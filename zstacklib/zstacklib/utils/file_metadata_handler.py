import json
import logging
import os
import re
import threading

from zstacklib.utils import lock
from zstacklib.utils.vm_metadata_handler import VmMetadataHandler, VmMetadataScanEntry

logger = logging.getLogger(__name__)

# MN uses FILE_METADATA_SUFFIX = ".vmmeta"
# agent detects metadata files by this suffix; all paths are MN-issued
_METADATA_SUFFIX = '.vmmeta'
_UUID_HEX_RE = re.compile(r'^[0-9a-f]{32}$')


def _fsync_directory(file_path):
    """fsync the parent directory to ensure rename/unlink is durable."""
    dir_fd = os.open(os.path.dirname(file_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _ensure_bytes(data):
    """Encode unicode to UTF-8 bytes; pass through if already bytes."""
    if isinstance(data, bytes):
        return data
    return data.encode('utf-8')


class _RefCountedLock(object):
    """A lock with a reference count for safe eviction."""
    __slots__ = ('lock', 'refcount')

    def __init__(self):
        self.lock = threading.Lock()
        self.refcount = 0


class _PathLockContext(object):
    """Context manager that acquires the path lock and releases the refcount on exit."""
    __slots__ = ('_handler', '_path', '_entry')

    def __init__(self, handler, path, entry):
        self._handler = handler
        self._path = path
        self._entry = entry

    def __enter__(self):
        self._entry.lock.acquire()
        return self._entry.lock

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._entry.lock.release()
        with self._handler._lock_map_lock:
            self._entry.refcount -= 1
        return False


def _validate_metadata_path(metadata_path):
    """Reject paths that are not absolute, do not end with the expected suffix,
    or whose basename is not ``<32-hex-UUID>.vmmeta``.

    This keeps write/get/cleanup consistent with scan(), which only
    recognises files matching the ``<32hex>.vmmeta`` pattern.
    """
    if not metadata_path or not os.path.isabs(metadata_path):
        raise ValueError("metadataPath must be an absolute path: %s" % metadata_path)
    name = os.path.basename(metadata_path)
    if not name.endswith(_METADATA_SUFFIX):
        raise ValueError("metadataPath must end with %s: %s" % (_METADATA_SUFFIX, metadata_path))
    vm_uuid = name[:-len(_METADATA_SUFFIX)]
    if not _UUID_HEX_RE.match(vm_uuid):
        raise ValueError(
            "metadataPath basename must be <32hex>%s: %s"
            % (_METADATA_SUFFIX, metadata_path)
        )


class FileBasedMetadataHandler(VmMetadataHandler):
    _LOCK_MAP_HIGH_WATER = 2000
    _LOCK_MAP_LOW_WATER = 500

    def __init__(self):
        super(FileBasedMetadataHandler, self).__init__()
        self._lock_map = {}
        self._lock_map_lock = threading.Lock()

    def _get_path_lock(self, metadataPath):
        """Return a context manager that holds the per-path lock.

        Uses reference counting so that eviction never removes an entry
        that has been handed out but not yet acquired.
        """
        with self._lock_map_lock:
            entry = self._lock_map.get(metadataPath)
            if entry is None:
                entry = _RefCountedLock()
                self._lock_map[metadataPath] = entry
            # Increment refcount BEFORE eviction so the new entry
            # is not considered idle and accidentally removed.
            entry.refcount += 1
            if len(self._lock_map) > self._LOCK_MAP_HIGH_WATER:
                self._evict_unlocked_entries()
            return _PathLockContext(self, metadataPath, entry)

    def _evict_unlocked_entries(self):
        """Remove entries whose locks are idle (refcount == 0 and not held).
        Called with _lock_map_lock already held."""
        to_remove = []
        for path, entry in self._lock_map.items():
            if len(self._lock_map) - len(to_remove) <= self._LOCK_MAP_LOW_WATER:
                break
            if entry.refcount == 0 and not entry.lock.locked():
                to_remove.append(path)
        for path in to_remove:
            del self._lock_map[path]
        if to_remove:
            logger.debug("evicted %d idle path locks (remaining %d)",
                         len(to_remove), len(self._lock_map))

    def _do_write(self, metadataPath, metadata, vmUuid, vmName, vmCategory, architecture, schemaVersion):
        _validate_metadata_path(metadataPath)
        path_vm_uuid = os.path.basename(metadataPath)[:-len(_METADATA_SUFFIX)]
        if vmUuid and vmUuid != path_vm_uuid:
            raise ValueError("vmUuid[%s] does not match metadataPath[%s]" % (vmUuid, metadataPath))
        with self._get_path_lock(metadataPath):
            dir_path = os.path.dirname(metadataPath)
            created_dir = False
            if not os.path.isdir(dir_path):
                try:
                    os.makedirs(dir_path, 0o700)
                    os.chmod(dir_path, 0o700)
                    created_dir = True
                except OSError:
                    if not os.path.isdir(dir_path):
                        raise
            if created_dir:
                _fsync_directory(dir_path)

            metadata_tmp = metadataPath + ".tmp"
            fd = os.open(metadata_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(_ensure_bytes(metadata))
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                # fd is consumed by os.fdopen even on failure
                raise
            os.rename(metadata_tmp, metadataPath)
            _fsync_directory(metadataPath)

            summary_path = metadataPath + ".summary"
            summary_vm_uuid = vmUuid or path_vm_uuid
            if summary_vm_uuid:
                _write_summary_best_effort(
                    summary_path, summary_vm_uuid,
                    vm_name=vmName,
                    vm_category=vmCategory,
                    architecture=architecture,
                    schema_version=schemaVersion,
                )
            else:
                # No vmUuid: remove stale summary to avoid scan() returning outdated info
                try:
                    if os.path.exists(summary_path):
                        os.remove(summary_path)
                        _fsync_directory(summary_path)
                except Exception as e:
                    logger.warn("failed to remove stale summary %s: %s", summary_path, e)

            logger.debug("successfully wrote vm metadata to %s", metadataPath)
            return {}

    def _do_get(self, metadataPath):
        _validate_metadata_path(metadataPath)
        with self._get_path_lock(metadataPath):
            if not os.path.isfile(metadataPath):
                tmp_path = metadataPath + ".tmp"
                if os.path.isfile(tmp_path):
                    # .tmp without a corresponding final file means the previous write
                    # may have crashed before rename().  Do NOT promote it - the data
                    # could be truncated.  Return None so the caller triggers a re-write.
                    logger.warn("found orphan tmp file %s without final metadata; "
                                "not promoting (may be incomplete)", tmp_path)
                return {'metadata': None}

            try:
                with open(metadataPath, 'r') as f:
                    content = f.read()
                logger.debug("read vm metadata from %s (%d bytes)", metadataPath, len(content))
                return {'metadata': content}
            except (IOError, OSError) as e:
                import errno as errno_mod
                if e.errno == errno_mod.ENOENT:
                    logger.warn("metadata file %s disappeared during read: %s", metadataPath, e)
                    return {'metadata': None}
                raise

    def _do_scan(self, metadataDir):
        if not metadataDir or not os.path.isabs(metadataDir):
            logger.warn("scan: metadataDir must be an absolute path: %s", metadataDir)
            return []
        if not os.path.isdir(metadataDir):
            return []

        entries = []
        for fname in os.listdir(metadataDir):
            is_tmp = fname.endswith(_METADATA_SUFFIX + '.tmp')
            if not (fname.endswith(_METADATA_SUFFIX) or is_tmp):
                continue
            vm_uuid = fname[:-len(_METADATA_SUFFIX + '.tmp')] if is_tmp else fname[:-len(_METADATA_SUFFIX)]
            if not _UUID_HEX_RE.match(vm_uuid):
                continue

            fpath = os.path.join(metadataDir, fname)
            metadata_path = fpath[:-4] if is_tmp else fpath
            if is_tmp and os.path.isfile(metadata_path):
                continue
            if not os.path.isfile(fpath):
                continue

            try:
                stat = os.stat(fpath)
                entry = VmMetadataScanEntry(
                    vmUuid=vm_uuid,
                    metadataPath=metadata_path,
                    sizeBytes=stat.st_size,
                    lastUpdateTime=int(stat.st_mtime * 1000),
                    incomplete=is_tmp,
                )

                summary_path = metadata_path + '.summary'
                try:
                    if not is_tmp and os.path.isfile(summary_path):
                        with open(summary_path, 'r') as sf:
                            summary = json.loads(sf.read())
                        entry.vmName = summary.get('vmName', '')
                        entry.vmCategory = summary.get('vmCategory', '')
                        entry.architecture = summary.get('architecture', '')
                        entry.schemaVersion = summary.get('schemaVersion', '')
                except Exception as e:
                    logger.warn("failed to load metadata summary %s: %s", summary_path, e)

                entries.append(entry)
            except Exception as e:
                logger.warn("failed to stat metadata file %s: %s", fpath, e)

        logger.debug("scan_vm_metadata on %s: found %d entries", metadataDir, len(entries))
        return entries

    def _do_cleanup(self, metadataPath):
        _validate_metadata_path(metadataPath)
        with self._get_path_lock(metadataPath):
            removed_any = False
            for path in [metadataPath, metadataPath + '.tmp',
                         metadataPath + '.summary', metadataPath + '.summary.tmp']:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed_any = True
                except Exception as e:
                    if path == metadataPath:
                        raise Exception("failed to cleanup metadata file %s: %s" % (path, str(e)))
                    logger.warn("failed to cleanup %s: %s", path, str(e))

            if removed_any:
                _fsync_directory(metadataPath)

            logger.debug("cleanup_vm_metadata: cleaned %s", metadataPath)

        return {}


def _write_summary_best_effort(summary_path, vm_uuid, vm_name='', vm_category='', architecture='', schema_version=''):
    """Write a lightweight summary file next to the metadata file.

    Failures are logged but never propagated (best-effort).
    """
    summary_tmp = summary_path + '.tmp'
    summary_json = json.dumps({
        "vmUuid": vm_uuid,
        "vmName": vm_name,
        "vmCategory": vm_category,
        "architecture": architecture,
        "schemaVersion": schema_version,
    }, ensure_ascii=False, separators=(',', ':'))
    try:
        fd = os.open(summary_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(_ensure_bytes(summary_json))
            f.flush()
            os.fsync(f.fileno())
        os.rename(summary_tmp, summary_path)
        _fsync_directory(summary_path)
    except Exception as e:
        logger.warn("failed to write summary for VM %s: %s", vm_uuid, e)
        try:
            os.remove(summary_tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  qcow2 backing-file rebase with imagecache flock protection
# ---------------------------------------------------------------------------

IMAGECACHE_DIR_MARKER = '/imagecache/'
REBASE_LOCK_SUFFIX = '.vmmeta-lck'


def _is_imagecache_path(path):
    """Check if a path is under an imagecache directory."""
    return IMAGECACHE_DIR_MARKER in path


def _get_rebase_lock_path(image_path):
    """Return the .vmmeta-lck lock file path for an imagecache qcow2.

    Pattern from zstack-store GetImageLockFile():
    strip file extension, append lock suffix.
    e.g. .../imagecache/template/UUID/UUID.qcow2 -> .../imagecache/template/UUID/UUID.vmmeta-lck
    """
    base, _ = os.path.splitext(image_path)
    return base + REBASE_LOCK_SUFFIX


def qcow2_prefix_rebase_backing_files(file_paths, old_prefix, new_prefix):
    """Walk the backing chain of each qcow2 file, rebasing paths that match old_prefix to new_prefix.

    For Local/NFS storage, acquires file locks on shared imagecache
    backing files to prevent concurrent rebase races.

    Returns the number of files successfully rebased.
    """
    # Import here to avoid circular dependency (linux <-> file_metadata_handler)
    from zstacklib.utils.linux import qcow2_get_backing_file, qcow2_rebase_no_check

    if not old_prefix:
        raise Exception("old_prefix must not be empty")
    if not new_prefix:
        raise Exception("new_prefix must not be empty")

    logger.info("[qcow2_rebase] START: file_count=%d, old_prefix=%s, new_prefix=%s"
                % (len(file_paths), old_prefix, new_prefix))

    old_prefix = os.path.normpath(old_prefix) + os.sep
    new_prefix = os.path.normpath(new_prefix) + os.sep

    logger.info("[qcow2_rebase] normalized: old_prefix=%s, new_prefix=%s" % (old_prefix, new_prefix))

    # -- Phase 1: Discovery --------------------------------------------------
    all_rebase_pairs = []  # [(current_path, new_backing), ...]
    imagecache_paths = set()  # paths needing file locks
    skipped_chains = []  # [(file_path, reason), ...]

    for file_path in file_paths:
        logger.info("[qcow2_rebase] Phase1: walking chain for %s" % file_path)
        rebase_pairs = []
        visited = set()
        current = file_path
        chain_valid = True
        depth = 0

        while current and current not in visited:
            visited.add(current)
            backing = qcow2_get_backing_file(current)
            if not backing:
                logger.debug("[qcow2_rebase]   depth=%d, current=%s, backing=<none>, chain end"
                             % (depth, current))
                break

            if not os.path.isabs(backing) and ':' not in backing:
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(current), backing))
                logger.debug("[qcow2_rebase]   depth=%d, current=%s, backing=%s (relative, resolved to %s)"
                             % (depth, current, backing, resolved))
                backing = resolved
            else:
                logger.debug("[qcow2_rebase]   depth=%d, current=%s, backing=%s"
                             % (depth, current, backing))

            backing_norm = os.path.normpath(backing)

            if backing_norm.startswith(old_prefix):
                new_backing = new_prefix + backing_norm[len(old_prefix):]
                if new_backing == backing_norm:
                    logger.debug("[qcow2_rebase]   backing already has new prefix, skip: %s" % backing_norm)
                    current = backing
                    depth += 1
                    continue
                if not os.path.exists(new_backing):
                    reason = "new backing %s not exist" % new_backing
                    logger.warn("[qcow2_rebase]   %s, skip entire chain for %s" % (reason, file_path))
                    skipped_chains.append((file_path, reason))
                    chain_valid = False
                    break

                logger.info("[qcow2_rebase]   needs rebase: %s -> %s (on %s)"
                            % (backing_norm, new_backing, current))
                rebase_pairs.append((current, new_backing))
                # Track imagecache files for locking (use new_prefix path)
                if _is_imagecache_path(new_backing):
                    imagecache_paths.add(new_backing)
                current = new_backing
            else:
                logger.debug("[qcow2_rebase]   backing %s does not match old_prefix, no rebase needed"
                             % backing_norm)
                # Backing doesn't need rebasing, but track if it's imagecache
                if _is_imagecache_path(backing_norm):
                    imagecache_paths.add(backing_norm)
                current = backing

            depth += 1

        if chain_valid and rebase_pairs:
            logger.info("[qcow2_rebase]   chain result: %d pairs queued for %s" % (len(rebase_pairs), file_path))
            all_rebase_pairs.extend(rebase_pairs)
        elif chain_valid:
            logger.info("[qcow2_rebase]   chain result: no rebase needed for %s" % file_path)

    logger.info("[qcow2_rebase] Phase1 summary: total_pairs=%d, imagecache_locks=%d, skipped_chains=%d"
                % (len(all_rebase_pairs), len(imagecache_paths), len(skipped_chains)))
    if skipped_chains:
        for sp, reason in skipped_chains:
            logger.warn("[qcow2_rebase]   skipped: %s, reason: %s" % (sp, reason))

    if not all_rebase_pairs:
        logger.info("[qcow2_rebase] END: nothing to rebase, return 0")
        return 0

    for i, (cp, nb) in enumerate(all_rebase_pairs):
        logger.info("[qcow2_rebase] plan[%d]: rebase %s -> new_backing %s" % (i, cp, nb))

    # -- Phase 2: Acquire imagecache locks ------------------------------------
    # Lock imagecache paths involved in rebase (both backing and current_path).
    lock_targets = set(imagecache_paths)
    for current_path, _new_backing in all_rebase_pairs:
        if _is_imagecache_path(current_path):
            lock_targets.add(current_path)
    sorted_lock_targets = sorted(lock_targets)
    acquired_locks = []
    rebased_count = 0
    skipped_count = 0
    logger.info("[qcow2_rebase] Phase2: acquiring %d imagecache locks" % len(sorted_lock_targets))

    try:
        for img_path in sorted_lock_targets:
            lock_path = _get_rebase_lock_path(img_path)
            logger.info("[qcow2_rebase]   locking: %s (lock_file=%s)" % (img_path, lock_path))
            fl = lock.FileLock(lock_path, locker=lock.Flock())
            fl.lock()
            acquired_locks.append(fl)
            logger.debug("[qcow2_rebase]   lock acquired: %s" % lock_path)

        logger.info("[qcow2_rebase] Phase2: all %d locks acquired" % len(acquired_locks))

        # -- Phase 3: Execute rebases under lock ------------------------------
        # Re-verify backing before each rebase to guard against concurrent
        # changes that may have happened between discovery and lock acquisition.
        logger.info("[qcow2_rebase] Phase3: executing %d rebases" % len(all_rebase_pairs))
        for current_path, new_backing in all_rebase_pairs:
            actual_backing = qcow2_get_backing_file(current_path)
            expected_old = old_prefix + new_backing[len(new_prefix):]
            expected_old_norm = os.path.normpath(expected_old)
            if not actual_backing:
                logger.warn("[qcow2_rebase]   SKIP: backing of %s disappeared since discovery"
                            % current_path)
                skipped_count += 1
                continue
            # Resolve relative backing paths relative to current_path's directory
            if not os.path.isabs(actual_backing) and ':' not in actual_backing:
                actual_backing = os.path.normpath(
                    os.path.join(os.path.dirname(current_path), actual_backing))
            actual_norm = os.path.normpath(actual_backing)
            if actual_norm != expected_old_norm:
                logger.warn("[qcow2_rebase]   SKIP: backing of %s changed since discovery "
                            "(expected=%s, actual=%s)" % (current_path, expected_old_norm, actual_norm))
                skipped_count += 1
                continue
            if not os.path.exists(new_backing):
                logger.warn("[qcow2_rebase]   SKIP: target backing %s disappeared since discovery, "
                            "skip rebase for %s" % (new_backing, current_path))
                skipped_count += 1
                continue
            logger.info("[qcow2_rebase]   rebasing: %s, old_backing=%s -> new_backing=%s"
                        % (current_path, actual_norm, new_backing))
            qcow2_rebase_no_check(new_backing, current_path)
            rebased_count += 1
            logger.info("[qcow2_rebase]   rebased OK: %s" % current_path)

    finally:
        logger.info("[qcow2_rebase] Phase4: releasing %d locks" % len(acquired_locks))
        for fl in reversed(acquired_locks):
            try:
                fl.unlock()
                logger.debug("[qcow2_rebase]   lock released: %s" % fl)
            except Exception as e:
                logger.warn("[qcow2_rebase]   failed to release lock %s: %s" % (fl, e))

    logger.info("[qcow2_rebase] END: rebased=%d, skipped=%d, total_planned=%d"
                % (rebased_count, skipped_count, len(all_rebase_pairs)))
    return rebased_count
