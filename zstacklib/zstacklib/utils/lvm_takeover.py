"""SharedBlock copied-VG takeover operations.

Only stage entrypoints use ``bash.in_bash`` so each stage emits one complete
command trace; decorating nested helpers would split that trace.
"""

import hashlib
import re

import simplejson

from zstacklib.utils import bash
from zstacklib.utils import linux
from zstacklib.utils import log
from zstacklib.utils import lvm


logger = log.get_logger(__name__)


def get_vg_for_takeover(tag, disk_uuids):
    """Match the complete selected LUN set, including copies hidden by LVM."""
    target = set(w.strip().lower() for w in disk_uuids if w and w.strip())
    records = lvm.get_all_vgs(tag)
    selected = [r for r in records if (r['disk'].get('wwid') or '').strip().lower() in target]
    names = set(r['vgUuid'] for r in selected)
    found = set(r['disk']['wwid'].strip().lower() for r in selected)
    if (not target or len(names) != 1 or found != target or len(selected) != len(target)
            or any(r['diskCount'] != len(target) for r in selected)):
        raise Exception("cannot find one complete VG for target WWIDs %s" % disk_uuids)

    vg_uuid = names.pop()
    duplicate = any(r['vgUuid'] == vg_uuid and
                    (r['disk'].get('wwid') or '').strip().lower() not in target
                    for r in records)
    return vg_uuid, duplicate


def import_vg_clone(new_vg_uuid, disk_paths):
    """Reset VG/PV identities on only the explicitly selected copy devices."""
    paths = sorted(set(p for p in (disk_paths or []) if p))
    if not paths:
        raise Exception("cannot import cloned VG without target disk paths")
    r, o, e = bash.bash_roe(
        "timeout -s SIGKILL %s vgimportclone --yes --nolocking --basevgname %s "
        "--devices %s %s" % (lvm.lvm_cmd_timeout_with_locking, linux.shellquote(new_vg_uuid),
                             linux.shellquote(','.join(paths)),
                             ' '.join(linux.shellquote(p) for p in paths)))
    if r != 0:
        raise Exception("failed to import cloned VG as %s from %s: stdout=%s, stderr=%s" %
                        (new_vg_uuid, paths, o, e))


def ensure_vg_sanlock(vg_uuid, disk_paths):
    """Rebuild leases only after import, never by rewriting an active lockspace."""
    lock_type = _takeover_vg_info(vg_uuid, disk_paths)['vg_lock_type'].strip()
    if lock_type == 'sanlock':
        return
    if lock_type not in ('', 'none'):
        raise Exception("unexpected lock type %s for cloned VG %s" % (lock_type, vg_uuid))

    lvs = _takeover_lvm("lvs -a --noheadings --nolocking -t -o lv_name,lv_active %s" %
                        linux.shellquote(vg_uuid), disk_paths)
    rows = [line.split() for line in lvs.splitlines() if line.strip()]
    if any(row[-1] == 'active' for row in rows):
        raise Exception("cannot rebuild cloned VG %s locks while LVs are active" % vg_uuid)
    if any(row[0].strip('[]') == 'lvmlock' for row in rows):
        _takeover_lvm("lvremove --yes --force %s" %
                      linux.shellquote('%s/lvmlock' % vg_uuid), disk_paths)
    # --nolocking also disables lvmlockd, so it cannot initialize sanlock.
    # The imported local VG is already isolated; only a missing GL is skipped.
    _takeover_lvm("vgchange --yes --lockopt skipgl --locktype sanlock %s" %
                  linux.shellquote(vg_uuid), disk_paths)
    if _takeover_vg_info(vg_uuid, disk_paths)['vg_lock_type'].strip() != 'sanlock':
        raise Exception("cloned VG %s lock type is not sanlock after conversion" % vg_uuid)


def _takeover_lvm(command, disk_paths):
    paths = sorted(set(disk_paths))
    if not paths:
        raise Exception("cannot operate on cloned VG without target disk paths")
    command = "timeout -s SIGKILL %s %s --devices %s" % (
        lvm.lvm_cmd_timeout_with_locking, command, linux.shellquote(','.join(paths)))
    r, o, e = bash.bash_roe(command)
    if r != 0:
        raise Exception("cloned VG operation failed: %s; stdout=%s, stderr=%s" % (command, o, e))
    return o


def _takeover_vg_info(vg_uuid, disk_paths):
    output = _takeover_lvm(
        "vgs --nolocking --shared --foreign -t --reportformat json "
        "-o vg_name,vg_uuid,vg_lock_type,vg_tags %s" % linux.shellquote(vg_uuid), disk_paths)
    rows = simplejson.loads(output)['report'][0]['vg']
    if len(rows) != 1 or rows[0]['vg_name'].strip() != vg_uuid or not rows[0]['vg_uuid'].strip():
        raise Exception("cannot identify one cloned VG %s on selected devices" % vg_uuid)
    return rows[0]


def _takeover_lockspace_present(vg_uuid):
    # A failed query is not evidence that it is safe to remove a device map.
    r, output, error = bash.bash_roe("timeout -s SIGKILL 30 sanlock client status")
    if r != 0:
        raise Exception("cannot query sanlock before takeover of %s: %s" % (vg_uuid, error))
    if any(line.strip().startswith('s lvm_%s:' % vg_uuid) for line in output.splitlines()):
        return True
    r, output, error = bash.bash_roe("timeout -s SIGKILL 30 lvmlockctl -i")
    if r != 0:
        raise Exception("cannot query lvmlockd before takeover of %s: %s" % (vg_uuid, error))
    return bool(re.search(r'\b%s\b' % re.escape(vg_uuid), output))


def _takeover_cleanup_lock_map(vg_uuid):
    """Clear ZSV-12632's stale internal mapping after the VG name is unambiguous.

    The caller must exclude a same-named source VG. Kernel mappings are not
    scoped by LVM --devices. Never remove business LV maps or an open lock LV.
    """
    name = vg_uuid.replace('-', '--') + '-lvmlock'
    r, output, error = bash.bash_roe(
        "timeout -s SIGKILL 30 dmsetup info -c --noheadings --separator '|' -o name,open")
    if r != 0:
        raise Exception("cannot inspect device maps for %s: %s" % (vg_uuid, error))
    matches = [line.strip().split('|') for line in output.splitlines()
               if line.strip().split('|')[0].strip() == name]
    if not matches or _takeover_lockspace_present(vg_uuid):
        return
    if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1].strip() != '0':
        raise Exception("cannot remove lock map %s: mapping is in use or ambiguous" % name)
    lvm.log_vg_lock_diagnostics(vg_uuid, "before takeover stale lock map cleanup")
    r, output, error = bash.bash_roe(
        "timeout -s SIGKILL 30 dmsetup remove %s" % linux.shellquote(name))
    if r != 0:
        raise Exception("failed to remove stale lock map %s: %s; %s" % (name, output, error))
    logger.info("takeover removed stale lock map %s" % name)


@bash.in_bash
def prepare_takeover_vg(vg_uuid, disk_uuids, disk_paths, init_tag):
    """Import the selected copied VG with independent VG/PV identities.

    The on-disk marker binds the target VG identity to its WWIDs, so a copied
    marker from another device set cannot skip import and retries do not
    regenerate PV UUIDs. It is not a user-facing UUID-mode tag.
    """
    current, duplicate = get_vg_for_takeover(init_tag, disk_uuids)
    if (current == vg_uuid and duplicate) or (current != vg_uuid and lvm.vg_exists(vg_uuid)):
        raise Exception("target VG name %s is already used by another device set" % vg_uuid)
    wwids = sorted(set(w.strip().lower() for w in disk_uuids))
    if len(set(disk_paths)) != len(wwids):
        raise Exception("selected WWIDs and clone device paths do not match")
    identity = hashlib.sha256('\n'.join(wwids).encode('utf-8')).hexdigest()
    temporary = 'zs-takeover-%s-%s' % (vg_uuid, identity[:12])
    marker_prefix = 'zs::takeover::'
    marker = marker_prefix + vg_uuid + '::' + identity
    info = _takeover_vg_info(current, disk_paths)
    tags = [t.strip() for t in info['vg_tags'].split(',') if t.strip()]
    if not duplicate:
        _takeover_cleanup_lock_map(current)

    if marker not in tags:
        if current != temporary:
            if lvm.vg_exists(temporary):
                raise Exception("temporary takeover VG %s already exists" % temporary)
            import_vg_clone(temporary, disk_paths)
            current, duplicate = get_vg_for_takeover(init_tag, disk_uuids)
            if current != temporary or duplicate:
                raise Exception("imported clone identity does not match selected devices")
            info = _takeover_vg_info(current, disk_paths)
            tags = [t.strip() for t in info['vg_tags'].split(',') if t.strip()]
        if info['vg_lock_type'].strip() not in ('', 'none'):
            raise Exception("unmarked temporary takeover VG %s is not a local VG" % current)
        _takeover_lvm("vgchange --addtag %s %s" %
                      (linux.shellquote(marker), linux.shellquote(current)), disk_paths)
        for tag in tags:
            if tag.startswith(marker_prefix):
                _takeover_lvm("vgchange --deltag %s %s" %
                              (linux.shellquote(tag), linux.shellquote(current)), disk_paths)
    elif current not in (temporary, vg_uuid):
        raise Exception("takeover marker does not match current VG name %s" % current)

    if current == temporary:
        if _takeover_vg_info(current, disk_paths)['vg_lock_type'].strip() not in ('', 'none'):
            raise Exception("cannot rename shared temporary takeover VG %s" % current)
        _takeover_lvm("vgrename --nolocking %s %s" %
                      (linux.shellquote(current), linux.shellquote(vg_uuid)), disk_paths)
    current, duplicate = get_vg_for_takeover(init_tag, disk_uuids)
    if current != vg_uuid or duplicate:
        raise Exception("final cloned VG name/WWIDs do not match registration")
    _takeover_cleanup_lock_map(vg_uuid)
    ensure_vg_sanlock(vg_uuid, disk_paths)


@bash.in_bash
def start_takeover_vg_lock(vg_uuid, disk_paths):
    """Start native, newly prepared leases without the normal recovery writers."""
    if not _takeover_lockspace_present(vg_uuid):
        _takeover_lvm("vgchange --lock-start %s" % linux.shellquote(vg_uuid), disk_paths)
    r, output, _ = bash.bash_roe("timeout -s SIGKILL 30 sanlock client status")
    lockspaces = [line.split() for line in output.splitlines()
                  if line.strip().startswith('s lvm_%s:' % vg_uuid)]
    if r != 0 or len(lockspaces) != 1 or any(flag in lockspaces[0] for flag in ('ADD', 'REM')):
        raise Exception("cloned VG %s lockspace is not ready" % vg_uuid)
    r, output, _ = bash.bash_roe("timeout -s SIGKILL 30 lvmlockctl -i")
    if r != 0 or not re.search(r'\b%s\b' % re.escape(vg_uuid), output):
        raise Exception("cloned VG %s lockspace is missing from lvmlockd" % vg_uuid)


@bash.in_bash
def update_imported_vg_tag(vg_uuid, old_tag_prefix, new_tag, disk_paths):
    """Update an imported VG tag using only its selected copy devices."""
    old_tags = [t.strip() for t in _takeover_vg_info(vg_uuid, disk_paths)['vg_tags'].split(",")
                if t.strip().startswith(old_tag_prefix)]

    _takeover_lvm("vgchange --addtag %s %s" %
                  (linux.shellquote(new_tag), linux.shellquote(vg_uuid)), disk_paths)
    for old_tag in old_tags:
        if old_tag == new_tag:
            continue
        try:
            _takeover_lvm("vgchange --deltag %s %s" %
                          (linux.shellquote(old_tag), linux.shellquote(vg_uuid)), disk_paths)
        except Exception as e:
            logger.warn("failed to remove old tag %s from VG %s: %s (non-fatal, new tag already set)"
                        % (old_tag, vg_uuid, e))
    if old_tags:
        logger.debug("updated VG tag for %s: %s -> %s" % (vg_uuid, old_tags, new_tag))
    else:
        logger.warn("no tag with prefix [%s] found on VG %s, added new tag directly" %
                    (old_tag_prefix, vg_uuid))
