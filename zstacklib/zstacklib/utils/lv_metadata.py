import ctypes
import errno as errno_mod
import json
import logging
import os
import re
import struct
import time

try:
    string_types = basestring
except NameError:
    string_types = str

from .lv_protocol import (
    ALIGNMENT, HEADER_BLOCK_SIZE,
    PENDING_NONE, PENDING_CONFIG_UPDATE, PENDING_STORAGE_CHANGE,
    SLOT_A, SLOT_HEADER_STRUCT_SIZE, SLOT_HEADER_FORMAT,
    CHECKSUM_SIZE, SLOT_OVERHEAD,
    OPTIMISTIC_READ_SIZE,
    MAX_LV_SIZE, INITIAL_LV_SIZE,
    IO_CHECK_PATTERN, IO_CHECK_PATTERN_LEN,
    LV_METADATA_SUFFIX, LV_METADATA_TAG,
    SlotLayout, ReadStatus, ReadResult,
    MetadataCapacityError, MetadataIOError,
    align_up, calculate_slot_layout,
    build_header, parse_header,
    build_slot, parse_slot,
)
from .vm_metadata_handler import VmMetadataHandler, VmMetadataScanEntry

logger = logging.getLogger(__name__)


# ###################################################################
# Handler
# ###################################################################

def _validate_metadata_lv_path(metadata_path):
    """Reject paths whose LV name is not ``<32-hex-UUID>_vmmeta``.

    This mirrors ``_validate_metadata_path`` in ``file_metadata_handler.py``
    to keep write/get/cleanup consistent with scan(), which only recognises
    LVs matching the ``<32hex>_vmmeta`` pattern.
    """
    if not metadata_path:
        raise ValueError("metadataPath must not be empty")
    lv_name = os.path.basename(metadata_path)
    if not lv_name.endswith(LV_METADATA_SUFFIX):
        raise ValueError("metadataPath LV name must end with %s: %s"
                         % (LV_METADATA_SUFFIX, metadata_path))
    vm_uuid = lv_name[:-len(LV_METADATA_SUFFIX)]
    if not re.match(r'^[0-9a-f]{32}$', vm_uuid):
        raise ValueError(
            "metadataPath LV name must be <32hex>%s: %s"
            % (LV_METADATA_SUFFIX, metadata_path))


class SblkMetadataHandler(VmMetadataHandler):
    # VG names in SharedBlock are UUIDs or LVM safe names (alnum + hyphen + underscore + period)
    _SAFE_VG_RE = re.compile(r'^[a-zA-Z0-9_.\-]+$')

    def __init__(self, lvm_module, bash_module):
        self._lvm = lvm_module
        self._bash = bash_module

    def _ensure_metadata_lv(self, metadata_path):
        """Create the metadata LV if it doesn't exist (no I/O initialization here)."""
        if self._lvm.lv_exists(metadata_path):
            return

        try:
            self._lvm.create_lv_from_absolute_path(
                metadata_path,
                INITIAL_LV_SIZE,
                tag=LV_METADATA_TAG,
                lock=True,
                exact_size=True,
            )
            logger.info("created metadata LV %s", metadata_path)
        except Exception:
            if self._lvm.lv_exists(metadata_path):
                logger.debug("metadata LV %s created by concurrent operation", metadata_path)
            else:
                raise

    def _initialize_if_needed(self, metadata_path, lv_size):
        """Under exclusive lock, initialize only truly blank (all-zero header) LVs.

        If the header is corrupted but non-zero, skip initialization and let
        write_metadata() fall back to a fresh write, whereas
        initialize_metadata_lv() would unconditionally wipe slot data.
        """
        fd = open_lv(metadata_path, readonly=True)
        try:
            header_bytes = aligned_pread(fd, HEADER_BLOCK_SIZE, 0)
            header = parse_header(header_bytes)
        finally:
            os.close(fd)

        if header.valid:
            return

        # Only initialize when the header block is all zeros (brand-new LV).
        # A non-zero but invalid header means corruption -- leave it for
        # write_metadata to handle via the normal write path.
        if header_bytes == b'\x00' * HEADER_BLOCK_SIZE:
            initialize_metadata_lv(metadata_path, lv_size)
            logger.info("initialized blank metadata LV %s", metadata_path)
        else:
            logger.warn("metadata LV %s has invalid header but non-zero data, "
                        "skipping initialization to preserve existing slots",
                        metadata_path)

    def _lv_list_func(self, vg):
        if not self._SAFE_VG_RE.match(vg):
            raise Exception("invalid VG name: %s" % vg)
        r, o = self._bash.bash_ro(
            "lvs --nolocking -t %s --noheadings -o lv_name,lv_path,lv_size --units b --nosuffix --separator '|'" % vg)
        if r != 0:
            raise Exception("failed to list metadata LVs in VG %s: %s" % (vg, o.strip()))
        result = []
        for line in o.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != 3:
                continue
            lv_name = parts[0].strip()
            lv_path = parts[1].strip()
            try:
                lv_size = int(float(parts[2].strip()))
            except (ValueError, TypeError):
                logger.warn("_lv_list_func: skipping LV with unparsable size: %s" % line)
                continue
            result.append((lv_name, lv_path, lv_size))
        return result

    def _do_write(self, metadataPath, metadata, vmUuid, vmName, vmCategory, architecture, schemaVersion):
        _validate_metadata_lv_path(metadataPath)
        self._ensure_metadata_lv(metadataPath)

        lvm = self._lvm

        def _get_lv_size():
            return int(float(lvm.get_lv_size(metadataPath)))

        def _extend_lv(new_size):
            lvm.extend_lv(metadataPath, new_size)

        with lvm.OperateLv(metadataPath, shared=False):
            self._initialize_if_needed(metadataPath, _get_lv_size())
            write_metadata(
                lv_path=metadataPath,
                payload=metadata,
                lv_size_getter=_get_lv_size,
                lv_extend_func=_extend_lv,
                schema_version=schemaVersion or '',
                vm_uuid=vmUuid,
                vm_name=vmName,
                vm_category=vmCategory or '',
                architecture=architecture,
            )

        logger.debug("successfully wrote vm metadata to %s", metadataPath)
        return {}

    def _do_get(self, metadataPath):
        _validate_metadata_lv_path(metadataPath)
        lvm = self._lvm

        if not lvm.lv_exists(metadataPath):
            return {'metadata': None}

        try:
            with lvm.OperateLv(metadataPath, shared=True):
                lv_size = int(lvm.get_lv_size(metadataPath))
                read_result = read_metadata(metadataPath, lv_size)
            if read_result.is_usable():
                payload = read_result.payload.decode('utf-8') \
                    if isinstance(read_result.payload, bytes) else read_result.payload
                logger.debug("read vm metadata from %s (%d bytes)",
                             metadataPath, len(payload) if payload else 0)
                return {'metadata': payload}
            else:
                logger.warn("metadata LV %s is not usable: status=%s, error=%s",
                            metadataPath, read_result.status, read_result.error)
                return {'metadata': None}
        except Exception as e:
            # Only swallow errors for LVs that no longer exist (race with
            # concurrent cleanup).  Real I/O / lock / activation errors must
            # propagate so that callers do not confuse storage failures with
            # "metadata not present".
            if not lvm.lv_exists(metadataPath):
                logger.warn("metadata LV %s disappeared during read: %s", metadataPath, e)
                return {'metadata': None}
            raise

    def _do_scan(self, metadataDir):
        # metadataDir is /dev/{vgUuid} for shared block
        vg_uuid = os.path.basename(metadataDir)
        lvm = self._lvm
        bash = self._bash

        @bash.in_bash
        def _lv_list(vg):
            return self._lv_list_func(vg)

        metadata_lvs = scan_metadata_lvs(vg_uuid, _lv_list)

        entries = []
        for item in metadata_lvs:
            vm_uuid = item['vm_uuid']
            lv_path = item['lv_path']
            lv_size = item['lv_size']

            entry = VmMetadataScanEntry(
                vmUuid=vm_uuid,
                metadataPath=lv_path,
                sizeBytes=lv_size,
            )

            try:
                with lvm.OperateLv(lv_path, shared=True):
                    status = get_metadata_status(lv_path, lv_size)
                if status.get('valid'):
                    entry.schemaVersion = status.get('schema_version', '')
                    entry.lastUpdateTime = status.get('last_update_time', 0)
                    entry.vmName = status.get('vm_name', '')
                    entry.architecture = status.get('architecture', '')
                    entry.vmCategory = status.get('vm_category', '')
            except Exception as e:
                logger.warn("failed to read metadata status for %s: %s", lv_path, e)

            entries.append(entry)

        logger.debug("scan_vm_metadata on vg %s: found %d metadata LVs, returned %d entries",
                     vg_uuid, len(metadata_lvs), len(entries))
        return entries

    def _do_cleanup(self, metadataPath):
        _validate_metadata_lv_path(metadataPath)
        lvm = self._lvm

        try:
            if lvm.lv_exists(metadataPath):
                delete_metadata_lv(metadataPath, lvm.delete_lv)
            else:
                logger.debug("metadata LV %s does not exist, skip cleanup", metadataPath)
        except Exception:
            logger.error("failed to cleanup metadata LV %s", metadataPath, exc_info=True)
            raise

        logger.debug("cleanup_vm_metadata: cleaned %s", metadataPath)
        return {}


# ###################################################################
# SharedBlock qcow2 backing-file rebase with LVM lock protection
# ###################################################################

def sblk_prefix_rebase_backing_files(file_paths, old_prefix, new_prefix, normalize_path, lvm_module):
    """Walk the backing chain of each LV-backed qcow2 file, rebasing paths
    that match *old_prefix* to *new_prefix*.

    Uses LVM locks (``OperateLv``) to serialise concurrent access to shared
    LVs.  Lock order is sorted alphabetically to prevent ABBA deadlock.

    Parameters
    ----------
    file_paths : list[str]
        LV install paths (may use ``sharedblock:/`` scheme).
    old_prefix, new_prefix : str
        Path prefixes to replace (may use ``sharedblock:/`` scheme).
    normalize_path : callable(str) -> str
        Converts a ``sharedblock:/`` install path to an absolute ``/dev/``
        path.  Pass-through for paths that are already absolute.
    lvm_module : module
        ``zstacklib.utils.lvm`` - supplies ``OperateLv`` context manager.

    Returns
    -------
    int
        Number of files successfully rebased.
    """
    # Import here to avoid circular dependency
    from zstacklib.utils.linux import qcow2_get_backing_file, qcow2_rebase_no_check

    if not old_prefix:
        raise Exception("oldPrefix must not be empty")
    if not new_prefix:
        raise Exception("newPrefix must not be empty")

    logger.info("[sblk_rebase] START: file_count=%d, old_prefix=%s, new_prefix=%s"
                % (len(file_paths), old_prefix, new_prefix))

    old_prefix_raw = old_prefix
    new_prefix_raw = new_prefix
    old_prefix = os.path.normpath(normalize_path(old_prefix)) + os.sep
    new_prefix = os.path.normpath(normalize_path(new_prefix)) + os.sep

    logger.info("[sblk_rebase] normalized: old_prefix=%s (raw=%s), new_prefix=%s (raw=%s)"
                % (old_prefix, old_prefix_raw, new_prefix, new_prefix_raw))

    # -- Phase 1: Discovery (read-only, per-LV shared locks) ------------------
    rebase_pairs = []  # [(current_path, old_backing, new_backing), ...]
    all_lv_paths = set()
    skipped_chains = []  # [(file_path, reason), ...]

    normalized_file_paths = [normalize_path(p) for p in file_paths]
    for idx, file_path in enumerate(normalized_file_paths):
        raw_path = file_paths[idx] if idx < len(file_paths) else file_path
        logger.info("[sblk_rebase] Phase1: walking chain for %s (raw=%s)" % (file_path, raw_path))
        all_lv_paths.add(file_path)
        chain_pairs = []
        chain_valid = True
        visited = set()
        current = file_path
        depth = 0

        while current and current not in visited:
            visited.add(current)
            logger.debug("[sblk_rebase]   depth=%d, reading backing of %s (shared lock)" % (depth, current))
            with lvm_module.OperateLv(current, shared=True):
                backing = qcow2_get_backing_file(current)

            if not backing:
                logger.debug("[sblk_rebase]   depth=%d, current=%s, backing=<none>, chain end"
                             % (depth, current))
                break

            raw_backing = backing
            if backing.startswith("sharedblock:/"):
                backing = normalize_path(backing)
                logger.debug("[sblk_rebase]   depth=%d, current=%s, backing=%s (normalized from %s)"
                             % (depth, current, backing, raw_backing))
            else:
                logger.debug("[sblk_rebase]   depth=%d, current=%s, backing=%s"
                             % (depth, current, backing))

            if backing.startswith(old_prefix):
                new_backing = new_prefix + backing[len(old_prefix):]
                if new_backing == backing:
                    logger.debug("[sblk_rebase]   backing already has new prefix, skip: %s" % backing)
                    all_lv_paths.add(backing)
                    current = backing
                    depth += 1
                    continue
                all_lv_paths.add(new_backing)
                logger.debug("[sblk_rebase]   checking new backing existence: %s" % new_backing)
                try:
                    with lvm_module.OperateLv(new_backing, shared=True):
                        exists = os.path.exists(new_backing)
                except Exception as e:
                    logger.warn("[sblk_rebase]   failed to check new backing %s: %s" % (new_backing, e))
                    exists = False

                if exists:
                    logger.info("[sblk_rebase]   needs rebase: %s -> %s (on %s)"
                                % (backing, new_backing, current))
                    chain_pairs.append((current, backing, new_backing))
                    current = new_backing
                else:
                    reason = "new backing %s not accessible" % new_backing
                    logger.warn("[sblk_rebase]   %s, skip entire chain for %s" % (reason, file_path))
                    skipped_chains.append((file_path, reason))
                    chain_valid = False
                    break
            else:
                logger.debug("[sblk_rebase]   backing %s does not match old_prefix, no rebase needed"
                             % backing)
                all_lv_paths.add(backing)
                current = backing

            depth += 1

        if chain_valid and chain_pairs:
            logger.info("[sblk_rebase]   chain result: %d pairs queued for %s" % (len(chain_pairs), file_path))
            rebase_pairs.extend(chain_pairs)
        elif chain_valid:
            logger.info("[sblk_rebase]   chain result: no rebase needed for %s" % file_path)

    logger.info("[sblk_rebase] Phase1 summary: total_pairs=%d, all_lv_paths=%d, skipped_chains=%d"
                % (len(rebase_pairs), len(all_lv_paths), len(skipped_chains)))
    if skipped_chains:
        for sp, reason in skipped_chains:
            logger.warn("[sblk_rebase]   skipped: %s, reason: %s" % (sp, reason))

    if not rebase_pairs:
        logger.info("[sblk_rebase] END: nothing to rebase, return 0")
        return 0

    for i, (cp, ob, nb) in enumerate(rebase_pairs):
        logger.info("[sblk_rebase] plan[%d]: rebase %s, old_backing=%s -> new_backing=%s" % (i, cp, ob, nb))

    logger.info("[sblk_rebase] all LV paths involved: %s" % sorted(all_lv_paths))

    # -- Phase 2 & 3: Acquire LVM locks, then execute rebases -----------------
    # Determine lock levels: current_path needs exclusive, backing shared.
    lv_lock_levels = {}  # path -> shared (True=shared, False=exclusive)
    for current_path, _, new_backing in rebase_pairs:
        lv_lock_levels[current_path] = False  # exclusive always
        if new_backing not in lv_lock_levels:
            lv_lock_levels[new_backing] = True  # shared, unless already exclusive

    sorted_lvs = sorted(lv_lock_levels.keys())
    logger.info("[sblk_rebase] Phase2: acquiring %d LVM locks (exclusive=%d, shared=%d)"
                % (len(sorted_lvs),
                   sum(1 for v in lv_lock_levels.values() if not v),
                   sum(1 for v in lv_lock_levels.values() if v)))
    for lv_path in sorted_lvs:
        logger.info("[sblk_rebase]   lock plan: %s (%s)"
                    % (lv_path, "shared" if lv_lock_levels[lv_path] else "exclusive"))

    acquired = []
    rebased_count = 0
    skipped_count = 0
    try:
        for lv_path in sorted_lvs:
            shared = lv_lock_levels[lv_path]
            logger.debug("[sblk_rebase]   acquiring %s lock on %s"
                         % ("shared" if shared else "exclusive", lv_path))
            op = lvm_module.OperateLv(lv_path, shared=shared)
            op.__enter__()
            acquired.append(op)
            logger.debug("[sblk_rebase]   lock acquired: %s" % lv_path)

        logger.info("[sblk_rebase] Phase2: all %d locks acquired" % len(acquired))

        # NOTE: execution order within a chain does not matter because
        # qcow2_rebase_no_check uses `qemu-img rebase -u` (unsafe mode),
        # which only rewrites the backing-file string in the qcow2 header
        # without reading or validating the backing content.  Each rebase
        # is an independent, atomic header update - no data dependency
        # between layers.
        logger.info("[sblk_rebase] Phase3: executing %d rebases" % len(rebase_pairs))
        for current_path, expected_old_backing, new_backing in rebase_pairs:
            if not os.path.exists(new_backing):
                logger.warn("[sblk_rebase]   SKIP: target backing %s disappeared since discovery, "
                            "skip rebase for %s" % (new_backing, current_path))
                skipped_count += 1
                continue
            actual_backing = qcow2_get_backing_file(current_path)
            raw_actual = actual_backing
            if actual_backing and actual_backing.startswith("sharedblock:/"):
                actual_backing = normalize_path(actual_backing)
            if actual_backing != expected_old_backing:
                logger.warn("[sblk_rebase]   SKIP: backing of %s changed since discovery "
                            "(expected=%s, actual=%s, raw_actual=%s)"
                            % (current_path, expected_old_backing, actual_backing, raw_actual))
                skipped_count += 1
                continue

            logger.info("[sblk_rebase]   rebasing: %s, old_backing=%s -> new_backing=%s"
                        % (current_path, expected_old_backing, new_backing))
            qcow2_rebase_no_check(new_backing, current_path)
            rebased_count += 1
            logger.info("[sblk_rebase]   rebased OK: %s" % current_path)
    finally:
        logger.info("[sblk_rebase] Phase4: releasing %d locks" % len(acquired))
        for op in reversed(acquired):
            try:
                op.__exit__(None, None, None)
                logger.debug("[sblk_rebase]   lock released: %s" % op.abs_path)
            except Exception as e:
                logger.warn("[sblk_rebase]   failed to release lock on %s: %s" % (op.abs_path, e))

    logger.info("[sblk_rebase] END: rebased=%d, skipped=%d, total_planned=%d"
                % (rebased_count, skipped_count, len(rebase_pairs)))
    return rebased_count


# ###################################################################
# Aligned I/O
# ###################################################################

_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL('libc.so.6', use_errno=True)
        _libc.pwrite.restype = ctypes.c_ssize_t
        _libc.pwrite.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_longlong]
        _libc.pread.restype = ctypes.c_ssize_t
        _libc.pread.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_size_t, ctypes.c_longlong]
        _libc.posix_memalign.restype = ctypes.c_int
        _libc.posix_memalign.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                         ctypes.c_size_t, ctypes.c_size_t]
        _libc.free.restype = None
        _libc.free.argtypes = [ctypes.c_void_p]
    return _libc


class AlignedBuffer(object):
    """Page-aligned buffer for O_DIRECT I/O.  Use as a context manager."""

    def __init__(self, size, alignment=ALIGNMENT):
        self._alignment = alignment
        self._size = align_up(size, alignment)
        self._ptr = ctypes.c_void_p()
        ret = _get_libc().posix_memalign(
            ctypes.byref(self._ptr), alignment, self._size)
        if ret != 0:
            raise OSError(ret, "posix_memalign failed (size=%d, align=%d)" % (self._size, alignment))
        ctypes.memset(self._ptr, 0, self._size)

    @property
    def size(self):
        return self._size

    def fill(self, data, offset=0):
        n = len(data)
        if offset + n > self._size:
            raise ValueError("data (len=%d) at offset %d exceeds buffer size %d" % (n, offset, self._size))
        ctypes.memmove(self._ptr.value + offset, data, n)

    def read(self, length, offset=0):
        if offset + length > self._size:
            raise ValueError("read (len=%d) at offset %d exceeds buffer size %d" % (length, offset, self._size))
        return ctypes.string_at(self._ptr.value + offset, length)

    def pwrite(self, fd, file_offset):
        """Write entire aligned buffer via O_DIRECT.

        Only EINTR is retried.  Short writes are fatal because advancing
        the pointer would break O_DIRECT alignment requirements.
        """
        while True:
            ret = _get_libc().pwrite(
                fd, self._ptr, self._size,
                ctypes.c_longlong(file_offset))
            if ret < 0:
                err = ctypes.get_errno()
                if err == errno_mod.EINTR:
                    continue
                raise OSError(err, "pwrite failed at offset %d: %s" % (file_offset, os.strerror(err)))
            if ret != self._size:
                raise OSError(0, "pwrite short write at offset %d: wrote %d of %d bytes"
                              % (file_offset, ret, self._size))
            return ret

    def pread(self, fd, file_offset):
        """Read entire aligned buffer via O_DIRECT.

        Only EINTR is retried.  Short reads are fatal because advancing
        the pointer would break O_DIRECT alignment requirements.
        A return of 0 means EOF (caller decides if that is an error).
        """
        while True:
            ret = _get_libc().pread(
                fd, self._ptr, self._size,
                ctypes.c_longlong(file_offset))
            if ret < 0:
                err = ctypes.get_errno()
                if err == errno_mod.EINTR:
                    continue
                raise OSError(err, "pread failed at offset %d: %s" % (file_offset, os.strerror(err)))
            if ret == 0:
                return 0
            if ret != self._size:
                raise OSError(0, "pread short read at offset %d: read %d of %d bytes"
                              % (file_offset, ret, self._size))
            return ret

    def close(self):
        if self._ptr.value:
            _get_libc().free(self._ptr)
            self._ptr = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


def aligned_pwrite(fd, data, file_offset, alignment=ALIGNMENT):
    with AlignedBuffer(len(data), alignment) as buf:
        buf.fill(data)
        return buf.pwrite(fd, file_offset)


def aligned_pread(fd, size, file_offset, alignment=ALIGNMENT):
    with AlignedBuffer(size, alignment) as buf:
        read_len = buf.pread(fd, file_offset)
        return buf.read(min(size, read_len))


def open_lv(lv_path, readonly=False):
    flags = os.O_RDONLY if readonly else os.O_RDWR
    flags |= os.O_DIRECT | os.O_SYNC
    return os.open(lv_path, flags)


def current_epoch_ms():
    return int(time.time() * 1000)


# ---- Bulk zero-fill ----
_ZERO_FILL_CHUNK = 1024 * 1024  # 1 MB - balances I/O count vs memory


def _zero_fill_region(fd, offset, length):
    """Write zeros to [offset, offset+length) using aligned O_DIRECT I/O.

    Uses 1 MB chunks to avoid per-4KB overhead on large regions.
    ``length`` must be a multiple of ALIGNMENT.
    """
    end = offset + length
    while offset < end:
        chunk = min(_ZERO_FILL_CHUNK, end - offset)
        aligned_pwrite(fd, b'\x00' * chunk, offset)
        offset += chunk


# ###################################################################
# LV Manager
# ###################################################################

def calculate_extend_size(current_lv_size, min_required):
    """Stepped expansion: <8MB +2MB, <16MB +4MB, <32MB +8MB, else +16MB."""
    if min_required > MAX_LV_SIZE:
        raise MetadataCapacityError(
            "Required LV size %d exceeds maximum %d"
            % (min_required, MAX_LV_SIZE))

    MB = 1024 * 1024
    size = current_lv_size

    while size < min_required:
        if size < 8 * MB:
            step = 2 * MB
        elif size < 16 * MB:
            step = 4 * MB
        elif size < 32 * MB:
            step = 8 * MB
        else:
            step = 16 * MB

        size += step
        if size > MAX_LV_SIZE:
            size = MAX_LV_SIZE
            break

    return size


def initialize_metadata_lv(lv_path, lv_size):
    """Initialise a brand-new metadata LV: sanity check, empty Slot A, Header."""
    fd = open_lv(lv_path)
    try:
        _io_sanity_check(fd, lv_path)

        layout = calculate_slot_layout(lv_size)

        slot_a = build_slot(
            seq_num=1,
            slot_offset=layout.slot_a_offset,
            slot_capacity=layout.slot_a_capacity,
            payload=b'{}',
        )
        aligned_pwrite(fd, slot_a, layout.slot_a_offset)

        # Zero-fill entire Slot B region to avoid residual data on reused PEs.
        _zero_fill_region(fd, layout.slot_b_offset, layout.slot_b_capacity)

        header = build_header(
            active_slot=SLOT_A,
            pending_op=0,
            write_sequence=1,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=0,
            schema_version='',
        )
        aligned_pwrite(fd, header, 0)

        logger.info("Initialized metadata LV %s (size=%d, slot_cap=%d)",
                    lv_path, lv_size, layout.slot_a_capacity)
    finally:
        os.close(fd)


def _io_sanity_check(fd, lv_path):
    """Write + read-back at offset 0 to verify O_DIRECT I/O path works."""
    test_data = IO_CHECK_PATTERN + b'\x00' * (512 - IO_CHECK_PATTERN_LEN)

    with AlignedBuffer(512) as buf:
        buf.fill(test_data)
        buf.pwrite(fd, 0)

    with AlignedBuffer(512) as buf:
        buf.pread(fd, 0)
        read_back = buf.read(IO_CHECK_PATTERN_LEN)
        if read_back != IO_CHECK_PATTERN:
            raise MetadataIOError(
                "O_DIRECT sanity check failed on %s "
                "(wrote %r, read %r)"
                % (lv_path, IO_CHECK_PATTERN, read_back))


def delete_metadata_lv(lv_path, lv_delete_func):
    lv_delete_func(lv_path)
    logger.info("Deleted metadata LV %s", lv_path)


def scan_metadata_lvs(vg_uuid, lv_list_func):
    """Scan VG for metadata LVs (names ending with '_vmmeta')."""
    result = []
    for lv_name, lv_path, lv_size in lv_list_func(vg_uuid):
        if lv_name.endswith(LV_METADATA_SUFFIX):
            vm_uuid = lv_name[:-len(LV_METADATA_SUFFIX)]
            if not re.match(r'^[0-9a-f]{32}$', vm_uuid):
                continue
            result.append({
                'vm_uuid': vm_uuid,
                'lv_path': lv_path,
                'lv_size': lv_size,
            })
    return result


def get_metadata_status(lv_path, lv_size):
    """Read-only health check: parse Header and return status dict."""
    fd = open_lv(lv_path, readonly=True)
    try:
        header_bytes = aligned_pread(fd, HEADER_BLOCK_SIZE, 0)
        header = parse_header(header_bytes)
        return {
            'valid': header.valid,
            'header_version': header.header_version,
            'active_slot': header.active_slot,
            'pending_op': header.pending_op,
            'write_sequence': header.write_sequence,
            'slot_a_offset': header.slot_a_offset,
            'slot_a_capacity': header.slot_a_capacity,
            'slot_b_offset': header.slot_b_offset,
            'slot_b_capacity': header.slot_b_capacity,
            'last_update_time': header.last_update_time,
            'schema_version': header.schema_version,
            'vm_category': header.vm_category,
            'vm_uuid': header.vm_uuid,
            'vm_name': header.vm_name,
            'architecture': header.architecture,
        }
    finally:
        os.close(fd)


# ###################################################################
# Read / Write / Recovery
# ###################################################################

def write_metadata(lv_path, payload, lv_size_getter, lv_extend_func,
                   schema_version='',
                   vm_category='', vm_uuid='', vm_name='',
                   architecture=''):
    """Three-phase atomic write of metadata to an sblk LV.

    IMPORTANT: The caller MUST hold an exclusive LVM lock (e.g.
    ``OperateLv(lv_path, shared=False)``) for the entire duration of this
    call.  This function reads the current header on a read-only fd, closes
    it, then opens a read-write fd for the three-phase write.  Without an
    external lock, a concurrent writer could modify the header between the
    two opens, causing silent data corruption.
    """
    payload_bytes = _ensure_bytes(payload)
    lv_size = int(lv_size_getter())

    # Step 0: read current state
    fd = open_lv(lv_path, readonly=True)
    try:
        header_bytes = aligned_pread(fd, HEADER_BLOCK_SIZE, 0)
        header = parse_header(header_bytes)
        if header.valid:
            op_type = _determine_op_type(fd, header, payload_bytes)
        else:
            header = None
            op_type = PENDING_STORAGE_CHANGE
    finally:
        os.close(fd)

    # Fresh write (no valid header)
    if header is None:
        _write_fresh(lv_path, payload_bytes, lv_size,
                     lv_size_getter, lv_extend_func, schema_version,
                     vm_category=vm_category, vm_uuid=vm_uuid,
                     vm_name=vm_name, architecture=architecture)
        return

    # Prepare three-phase write
    target_slot = 1 - header.active_slot
    new_seq = header.write_sequence + 1

    current_layout = SlotLayout(
        slot_a_offset=header.slot_a_offset,
        slot_a_capacity=header.slot_a_capacity,
        slot_b_offset=header.slot_b_offset,
        slot_b_capacity=header.slot_b_capacity,
    )

    required = SLOT_OVERHEAD + len(payload_bytes)
    target_cap = (current_layout.slot_a_capacity if target_slot == SLOT_A
                  else current_layout.slot_b_capacity)

    if required > target_cap:
        new_layout, lv_size = _extend_for_payload(
            lv_path, lv_size, len(payload_bytes),
            lv_size_getter, lv_extend_func)
    else:
        new_layout = current_layout

    if target_slot == SLOT_A:
        tgt_offset = new_layout.slot_a_offset
        tgt_capacity = new_layout.slot_a_capacity
    else:
        tgt_offset = new_layout.slot_b_offset
        tgt_capacity = new_layout.slot_b_capacity

    # Phase 1 -> 2 -> 3
    #
    # Phase 1 records new_layout (not current_layout) so that recovery
    # flows B/C can locate the Phase-2 payload at the correct offset
    # after an extend.  When new_layout differs from current_layout
    # (i.e. an extend occurred), Phase 1 also records prev_slot_*
    # fields so that recovery can locate the active slot using the
    # pre-extend capacity - the active slot's on-disk slot header still
    # records the old capacity, and strict parse would reject the new
    # capacity.  Recovery flows B/C use prev_slot_* to retry the active
    # slot read on strict failure.
    layout_changed = (new_layout != current_layout)
    fd = open_lv(lv_path)
    try:
        # Phase 1: Mark Intent - use new_layout so recovery can find
        # the Phase-2 slot; keep OLD summary fields for abort consistency.
        phase1 = build_header(
            active_slot=header.active_slot,
            pending_op=op_type,
            write_sequence=new_seq,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=header.last_update_time,
            schema_version=header.schema_version,
            vm_category=header.vm_category, vm_uuid=header.vm_uuid,
            vm_name=header.vm_name, architecture=header.architecture,
            prev_slot_a_capacity=current_layout.slot_a_capacity if layout_changed else 0,
            prev_slot_b_offset=current_layout.slot_b_offset if layout_changed else 0,
            prev_slot_b_capacity=current_layout.slot_b_capacity if layout_changed else 0,
        )
        aligned_pwrite(fd, phase1, 0)

        # Phase 2: Write payload to inactive slot
        slot_data = build_slot(
            seq_num=new_seq,
            slot_offset=tgt_offset,
            slot_capacity=tgt_capacity,
            payload=payload_bytes,
        )
        aligned_pwrite(fd, slot_data, tgt_offset)

        # Phase 3: Commit
        phase3 = build_header(
            active_slot=target_slot,
            pending_op=PENDING_NONE,
            write_sequence=new_seq,
            slot_a_offset=new_layout.slot_a_offset,
            slot_a_capacity=new_layout.slot_a_capacity,
            slot_b_offset=new_layout.slot_b_offset,
            slot_b_capacity=new_layout.slot_b_capacity,
            last_update_time=current_epoch_ms(),
            schema_version=schema_version,
            vm_category=vm_category, vm_uuid=vm_uuid, vm_name=vm_name,
            architecture=architecture,
        )
        aligned_pwrite(fd, phase3, 0)
    finally:
        os.close(fd)


def read_metadata(lv_path, lv_size):
    """Read metadata with full recovery support. Returns ReadResult."""
    lv_size = int(lv_size)
    fd = open_lv(lv_path, readonly=True)
    try:
        return _read_metadata_fd(fd, lv_size)
    finally:
        os.close(fd)


# ---- Read dispatcher ----

def _read_metadata_fd(fd, lv_size):
    header_bytes = aligned_pread(fd, HEADER_BLOCK_SIZE, 0)
    header = parse_header(header_bytes)

    if header.valid:
        if header.pending_op == PENDING_NONE:
            return _read_flow_a(fd, header, lv_size)
        elif header.pending_op == PENDING_CONFIG_UPDATE:
            return _read_flow_b(fd, header, lv_size)
        elif header.pending_op == PENDING_STORAGE_CHANGE:
            return _read_flow_c(fd, header, lv_size)
        else:
            logger.warning("Unknown PendingOp %d, treating as STORAGE_CHANGE",
                           header.pending_op)
            return _read_flow_c(fd, header, lv_size)
    else:
        logger.error("Header corrupted on LV (lv_size=%d), "
                     "cannot read metadata", lv_size)
        return ReadResult(status=ReadStatus.CORRUPTED,
                          error="Header corrupted")


# ---- Flow A: PendingOp == 0, normal read ----

def _read_flow_a(fd, header, lv_size):
    active = _read_active_slot(fd, header)
    if active.valid:
        return ReadResult(status=ReadStatus.OK,
                          payload=active.payload, header=header)

    inactive = _read_inactive_slot(fd, header)
    if inactive.valid:
        logger.warning("Active slot corrupted; returning inactive payload "
                     "(SeqNum=%d) which may be stale", inactive.seq_num)
        return ReadResult(
            status=ReadStatus.OK, header=header,
            payload=inactive.payload,
            error=("Active slot corrupted; returning inactive payload "
                   "(SeqNum=%d) which may be stale" % inactive.seq_num))

    return ReadResult(status=ReadStatus.CORRUPTED, header=header,
                      error="Both slots corrupted")


# ---- Flow B: PendingOp == 1, CONFIG_UPDATE interrupted ----

def _read_flow_b(fd, header, lv_size):
    target = _read_slot_at(fd, header, 1 - header.active_slot)

    if target.valid and target.seq_num == header.write_sequence:
        # Phase 2 done, Phase 3 not - target slot has latest data
        logger.info("CONFIG_UPDATE interrupted after Phase 2; "
                    "using target slot (seq=%d)", target.seq_num)
        return ReadResult(
            status=ReadStatus.OK,
            payload=target.payload, header=header)

    active = _read_active_slot_with_prev(fd, header)
    if active.valid:
        logger.info("CONFIG_UPDATE interrupted before Phase 2; "
                    "using active slot (seq=%d)", active.seq_num)
        return ReadResult(
            status=ReadStatus.OK,
            payload=active.payload, header=header)

    return ReadResult(status=ReadStatus.CORRUPTED, header=header,
                      error="CONFIG_UPDATE pending, both slots unreadable")


# ---- Flow C: PendingOp == 2, STORAGE_CHANGE interrupted ----

def _read_flow_c(fd, header, lv_size):
    target = _read_slot_at(fd, header, 1 - header.active_slot)

    if target.valid and target.seq_num == header.write_sequence:
        logger.info("STORAGE_CHANGE interrupted after Phase 2; "
                    "using target slot (seq=%d)", target.seq_num)
        return ReadResult(
            status=ReadStatus.OK,
            payload=target.payload, header=header)

    # Target invalid -> stale data, DANGEROUS
    active = _read_active_slot_with_prev(fd, header)
    return ReadResult(
        status=ReadStatus.STORAGE_CHANGE_INCOMPLETE,
        payload=active.payload if active.valid else None,
        header=header,
        error=("Storage topology changed but metadata not updated. "
               "Active slot has stale data. Must execute full-refresh."))


# ---- Slot I/O helpers ----

def _read_active_slot(fd, header):
    return _read_slot_at(fd, header, header.active_slot)


def _read_inactive_slot(fd, header):
    return _read_slot_at(fd, header, 1 - header.active_slot)


def _read_slot_at(fd, header, slot_index):
    if slot_index == SLOT_A:
        offset = header.slot_a_offset
        capacity = header.slot_a_capacity
    else:
        offset = header.slot_b_offset
        capacity = header.slot_b_capacity
    return _read_and_parse_slot(fd, offset, capacity, strict=True)


def _read_active_slot_with_prev(fd, header):
    """Read active slot; on strict failure, retry with prev layout.

    After an LV extend, Phase 1 records new_layout in the header but
    the active slot's on-disk slot header still has the old capacity.
    Strict parse rejects the capacity mismatch.  If prev_slot_*
    fields are present (non-zero), retry the read with the pre-extend
    layout so the active slot's intact data can be recovered.
    """
    active = _read_active_slot(fd, header)
    if active.valid:
        return active

    if header.active_slot == SLOT_A:
        prev_cap = header.prev_slot_a_capacity
        if prev_cap > 0:
            return _read_and_parse_slot(
                fd, header.slot_a_offset, prev_cap, strict=True)
    else:
        prev_off = header.prev_slot_b_offset
        prev_cap = header.prev_slot_b_capacity
        if prev_cap > 0 and prev_off > 0:
            return _read_and_parse_slot(
                fd, prev_off, prev_cap, strict=True)

    return active


def _read_and_parse_slot(fd, offset, capacity, strict=True):
    """Optimistic 1-MB-first read; re-reads if payload is larger."""
    read_size = min(capacity, OPTIMISTIC_READ_SIZE) if capacity > 0 \
        else OPTIMISTIC_READ_SIZE
    data = aligned_pread(fd, read_size, offset)

    slot = parse_slot(data, expected_offset=offset,
                      expected_capacity=capacity if strict else None,
                      strict=strict)

    if not slot.valid and read_size < capacity:
        if len(data) >= SLOT_HEADER_STRUCT_SIZE:
            try:
                _, _, _, _, payload_len = struct.unpack(
                    SLOT_HEADER_FORMAT, data[:SLOT_HEADER_STRUCT_SIZE])
                total_needed = (SLOT_HEADER_STRUCT_SIZE
                                + payload_len + CHECKSUM_SIZE)
                if read_size < total_needed <= capacity:
                    data = aligned_pread(fd, total_needed, offset)
                    slot = parse_slot(
                        data, expected_offset=offset,
                        expected_capacity=capacity if strict else None,
                        strict=strict)
            except struct.error:
                pass

    return slot


# ---- Op type determination ----

def _determine_op_type(fd, header, new_payload):
    """CONFIG_UPDATE vs STORAGE_CHANGE based on volume/snapshot topology diff."""
    try:
        active_slot = _read_active_slot(fd, header)
        if active_slot.valid and active_slot.payload:
            if _storage_topology_changed(active_slot.payload, new_payload):
                return PENDING_STORAGE_CHANGE
            else:
                return PENDING_CONFIG_UPDATE
    except Exception as e:
        logger.debug("failed to determine op type, defaulting to STORAGE_CHANGE: %s", e)
    return PENDING_STORAGE_CHANGE


def _storage_topology_changed(old_payload, new_payload):
    """Compare volume UUIDs/installPaths and snapshot UUIDs."""
    try:
        old_str = (old_payload.decode('utf-8')
                   if isinstance(old_payload, bytes) else old_payload)
        new_str = (new_payload.decode('utf-8')
                   if isinstance(new_payload, bytes) else new_payload)
        old = json.loads(old_str)
        new = json.loads(new_str)
    except (ValueError, TypeError):
        return True

    def _extract_topology(d):
        vols = {}
        for rm in d.get('volumes', []):
            vo_str = rm.get('vo', '{}')
            try:
                vo = json.loads(vo_str) if isinstance(vo_str, string_types) else vo_str
                vols[vo.get('uuid', '')] = vo.get('installPath', '')
            except (ValueError, TypeError):
                pass
        snaps = {}
        # snapshots is a flat list of JSON-serialized VolumeSnapshotVO strings
        snap_list = d.get('snapshots', [])
        if isinstance(snap_list, list):
            for s_json in snap_list:
                try:
                    s = (json.loads(s_json) if isinstance(s_json, string_types)
                         else s_json)
                    snaps[s.get('uuid', '')] = s.get(
                        'primaryStorageInstallPath', '')
                except (ValueError, TypeError):
                    pass
        return vols, snaps

    try:
        old_vols, old_snaps = _extract_topology(old)
        new_vols, new_snaps = _extract_topology(new)
        return old_vols != new_vols or old_snaps != new_snaps
    except Exception as e:
        logger.debug("failed to extract topology, assuming changed: %s", e)
        return True


# ---- Fresh write (no valid header) ----

def _write_fresh(lv_path, payload_bytes, lv_size,
                 lv_size_getter, lv_extend_func, schema_version,
                 vm_category='', vm_uuid='', vm_name='',
                 architecture=''):
    layout = calculate_slot_layout(lv_size)
    required = SLOT_OVERHEAD + len(payload_bytes)

    if required > layout.slot_a_capacity:
        layout, lv_size = _extend_for_payload(
            lv_path, lv_size, len(payload_bytes),
            lv_size_getter, lv_extend_func)

    fd = open_lv(lv_path)
    try:
        # Clear entire Slot B region to prevent stale data on reused PEs.
        _zero_fill_region(fd, layout.slot_b_offset, layout.slot_b_capacity)

        slot_data = build_slot(
            seq_num=1,
            slot_offset=layout.slot_a_offset,
            slot_capacity=layout.slot_a_capacity,
            payload=payload_bytes)
        aligned_pwrite(fd, slot_data, layout.slot_a_offset)

        h = build_header(
            active_slot=SLOT_A,
            pending_op=PENDING_NONE,
            write_sequence=1,
            slot_a_offset=layout.slot_a_offset,
            slot_a_capacity=layout.slot_a_capacity,
            slot_b_offset=layout.slot_b_offset,
            slot_b_capacity=layout.slot_b_capacity,
            last_update_time=current_epoch_ms(),
            schema_version=schema_version,
            vm_category=vm_category, vm_uuid=vm_uuid, vm_name=vm_name,
            architecture=architecture)
        aligned_pwrite(fd, h, 0)
    finally:
        os.close(fd)


def _extend_for_payload(lv_path, current_lv_size, payload_len,
                        lv_size_getter, lv_extend_func):
    required = SLOT_OVERHEAD + payload_len
    min_lv = ALIGNMENT + 2 * align_up(required, ALIGNMENT)
    if min_lv > MAX_LV_SIZE:
        raise MetadataCapacityError(
            "Required LV size %d exceeds maximum %d "
            "(payload = %d bytes)" % (min_lv, MAX_LV_SIZE, payload_len))

    new_lv_target = calculate_extend_size(current_lv_size, min_lv)
    lv_extend_func(new_lv_target)
    actual_size = int(lv_size_getter())
    if actual_size < min_lv:
        raise MetadataCapacityError(
            "LV %s extended to %d but need at least %d "
            "(VG may be out of space)" % (lv_path, actual_size, min_lv))
    return calculate_slot_layout(actual_size), actual_size


def _ensure_bytes(data):
    if isinstance(data, bytes):
        return data
    return data.encode('utf-8')
