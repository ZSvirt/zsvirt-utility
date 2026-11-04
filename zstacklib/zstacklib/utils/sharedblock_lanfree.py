import os
import re
from collections import Counter
from itertools import chain

import simplejson

LVM_REPORT_TIMEOUT_SECONDS = 60
_LVM_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9+_.-]+$")
try:
    _long = long
except NameError:
    _long = int
_INTEGER_TYPES = (int, _long)
try:
    _STRING_TYPES = (basestring,)
except NameError:
    _STRING_TYPES = (str,)


# LVM metadata validation and physical-range construction.
_PE_RANGE_PATTERN = re.compile(r"^(.+):(\d+)(?:-(\d+))?$")

VG_RANGE_REPORT_FIELDS = ("vg_name", "vg_uuid", "vg_attr", "vg_extent_size", "vg_seqno", "pv_count", "vg_missing_pv_count")
PV_RANGE_REPORT_FIELDS = ("vg_uuid", "pv_uuid", "pv_name", "pv_size", "dev_size", "pe_start", "pv_pe_count", "pv_attr", "pv_missing", "pv_duplicate")
PV_DUPLICATE_AUDIT_FIELDS = ("vg_uuid", "pv_uuid", "pv_name", "pv_duplicate")


def _value(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _text(value):
    if value is None:
        return None
    return str(value).strip()


def _normalize_paths(paths):
    return set(filter(None, (_text(path) for path in paths)))


def _index_paths(path_groups):
    by_path = {}
    for index, paths in enumerate(path_groups):
        for path in paths:
            by_path.setdefault(path, set()).add(index)
    return by_path


def _required_text(value, field):
    result = _text(value)
    if not result:
        raise ValueError("missing text field[%s]" % field)
    return result


def _report_rows(report, section):
    if not isinstance(report, dict):
        raise ValueError("invalid LVM report for section[%s]" % section)

    rows = []
    for part in report.get("report", []):
        rows.extend(part.get(section, []))
    return rows


def _number(value, field):
    if value is None:
        raise ValueError("missing numeric field[%s]" % field)
    try:
        return _long(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("invalid numeric field[%s:%s]" % (field, value))


def _error(code, message):
    raise ValueError("%s: %s" % (code, message))


def _flag_is_set(value):
    value = (_text(value) or "").lower()
    return value not in ("", "0", "false", "n", "no")


def _device_path(dev_name):
    dev_name = _required_text(dev_name, "block-device.dev_name")
    if dev_name.startswith("/dev/"):
        return dev_name
    return "/dev/%s" % dev_name


def _read_capacity(path, capacity_reader):
    try:
        capacity = _number(capacity_reader(path), "block-device.path-capacity.size")
    except Exception as error:
        _error("LVM_RANGE_DEVICE_CAPACITY_MISSING", "cannot read capacity for path[%s]: %s" % (path, error))
    if capacity <= 0:
        _error("LVM_RANGE_DEVICE_CAPACITY_MISSING", "path[%s] has no positive capacity" % path)
    return capacity


def build_block_device_evidence(candidate, paths, realpath, list_slaves, capacity_reader):
    try:
        wwid = _required_text(_value(candidate, "wwid"), "block-device.wwid")
    except ValueError as error:
        _error("LVM_RANGE_WWID_MISSING", str(error))
    device_type = _text(_value(candidate, "type"))
    multipath_name = _text(_value(candidate, "multipathPath"))
    normalized_paths = _normalize_paths(paths)

    if device_type == "mpath" or multipath_name:
        if not multipath_name:
            _error("LVM_RANGE_MULTIPATH_TOPOLOGY_INCOMPLETE", "multipath device[%s] has no map name" % wwid)
        map_path = "/dev/mapper/%s" % multipath_name
        canonical_path = realpath(map_path)
        try:
            slaves = list(list_slaves(canonical_path) or [])
        except Exception as error:
            _error("LVM_RANGE_MULTIPATH_TOPOLOGY_INCOMPLETE", "cannot enumerate slaves for map[%s]: %s" % (map_path, error))
        slave_paths = sorted(set(_device_path(slave) for slave in slaves))
        if not slave_paths:
            _error("LVM_RANGE_MULTIPATH_TOPOLOGY_INCOMPLETE", "multipath map[%s] has no slave devices" % map_path)

        capacity_paths = [map_path] + slave_paths
        path_capacities = [{"path": path, "size": _read_capacity(path, capacity_reader)} for path in capacity_paths]
        normalized_paths.update(capacity_paths)
        normalized_paths.add(canonical_path)
        topology = "mpath"
    elif device_type in ("disk", "lvm-pv"):
        disk_path = _device_path(_value(candidate, "dev_name"))
        canonical_path = realpath(disk_path)
        capacity_paths = [disk_path]
        path_capacities = [{"path": disk_path, "size": _read_capacity(disk_path, capacity_reader)}]
        normalized_paths.update([disk_path, canonical_path])
        topology = "disk"
    else:
        _error("LVM_RANGE_TOPOLOGY_UNSUPPORTED", "device[%s] has unsupported topology[%s]" % (wwid, device_type))

    return {
        "paths": sorted(normalized_paths),
        "canonicalPath": canonical_path,
        "wwid": wwid,
        "size": path_capacities[0]["size"],
        "topology": topology,
        "pathCapacities": path_capacities
    }


def resolve_pv_block_devices(pv_names, candidates, realpath, list_slaves, capacity_reader):
    if not pv_names:
        return []
    paths_by_candidate = [_normalize_paths(paths) for unused_candidate, paths in candidates]
    by_path = _index_paths(paths_by_candidate)
    by_realpath = _index_paths(set(realpath(path) for path in paths) for paths in paths_by_candidate)
    result = []
    for pv_name in pv_names:
        pv_name = _required_text(pv_name, "pv_name")
        real_pv_name = realpath(pv_name)
        matches = by_path.get(pv_name, set()) | by_realpath.get(real_pv_name, set())
        if not matches:
            _error("LVM_RANGE_PV_DEVICE_UNRESOLVED", "cannot resolve block device for PV[%s]" % pv_name)
        if len(matches) != 1:
            _error("LVM_RANGE_PV_DEVICE_AMBIGUOUS", "PV[%s] resolves to[%s] block-device candidates" % (pv_name, len(matches)))

        index = matches.pop()
        candidate = candidates[index][0]
        paths = list(paths_by_candidate[index]) + [pv_name, real_pv_name]
        result.append(build_block_device_evidence(candidate, paths, realpath, list_slaves, capacity_reader))

    target_wwids = set(device["wwid"] for device in result)
    candidate_count_by_wwid = Counter(_text(_value(candidate, "wwid")) for candidate, unused_paths in candidates)
    for wwid, count in candidate_count_by_wwid.items():
        if wwid in target_wwids and count > 1:
            _error("LVM_RANGE_WWID_AMBIGUOUS", "target WWID[%s] occurs on[%s] block-device candidates" % (wwid, count))
    return result


def _parse_pe_range(value):
    ranges = [item.strip() for item in str(value).split(",") if item.strip()]
    if len(ranges) != 1:
        raise ValueError("linear segment must contain exactly one PV range[%s]" % value)

    matched = _PE_RANGE_PATTERN.match(ranges[0])
    if not matched:
        raise ValueError("invalid seg_pe_ranges[%s]" % value)

    start = _number(matched.group(2), "pv extent start")
    end = _number(matched.group(3) or matched.group(2), "pv extent end")
    if end < start:
        raise ValueError("invalid descending PV range[%s]" % value)
    return matched.group(1).strip(), start, end - start + 1


def _device_paths(device):
    paths = list(_value(device, "paths", []) or [])
    path = _value(device, "path")
    if path:
        paths.append(path)
    multipath_path = _value(device, "multipathPath")
    if multipath_path:
        paths.append("/dev/mapper/%s" % multipath_path)
    dev_name = _value(device, "dev_name")
    if dev_name:
        paths.append("/dev/%s" % dev_name)

    normalized = _normalize_paths(paths)
    return normalized | set(os.path.realpath(path) for path in normalized)


def _target_lv(target):
    path = _value(target, "absoluteInstallPath") or _value(target, "installPath")
    path = _required_text(path, "absoluteInstallPath")
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "" or parts[1] != "dev" or not parts[2] or not parts[3]:
        raise ValueError("invalid absolute LV install path[%s]" % path)
    return path, parts[2], parts[3]


def _pv_rows_by_name(pv_report):
    result = {}
    for row in _report_rows(pv_report, "pv"):
        name = _required_text(row.get("pv_name"), "pv_name")
        if name in result:
            raise ValueError("PV report contains duplicate pv_name[%s]" % name)
        result[name] = row
    return result


def validate_pv_duplicate_audit(target_pv_report, audit_report):
    target_rows = _report_rows(target_pv_report, "pv")
    audit_rows = _report_rows(audit_report, "pv")
    audit_by_pvid = {}
    audit_by_name = {}
    for row in audit_rows:
        pvid = _text(row.get("pv_uuid"))
        name = _text(row.get("pv_name"))
        if pvid:
            audit_by_pvid.setdefault(pvid, []).append(row)
        if name:
            audit_by_name.setdefault(name, []).append(row)

    for target in target_rows:
        target_pvid = _required_text(target.get("pv_uuid"), "pv_uuid")
        target_name = _required_text(target.get("pv_name"), "pv_name")
        matches = audit_by_pvid.get(target_pvid, [])
        conflicting_rows = [row for row in audit_by_name.get(target_name, []) if _text(row.get("pv_uuid")) != target_pvid]
        if conflicting_rows:
            _error("LVM_RANGE_PVID_CONFLICT", "target PV[%s] maps to another PVID in the unfiltered PV audit" % target_name)
        if not matches:
            _error("LVM_RANGE_PVID_AUDIT_INCOMPLETE", "target PVID[%s] is absent from the unfiltered PV audit" % target_pvid)
        if not any(_text(row.get("pv_name")) == target_name for row in matches):
            _error("LVM_RANGE_PVID_CONFLICT", "target PV[%s] does not map uniquely to PVID[%s] in the unfiltered PV audit" % (target_name, target_pvid))

        if (len(matches) != 1 or any(_flag_is_set(row.get("pv_duplicate")) for row in matches)):
            paths = sorted(set(_text(row.get("pv_name")) or "<unknown>" for row in matches))
            _error("LVM_RANGE_PVID_DUPLICATE", "target PVID[%s] occurs on devices[%s] in the unfiltered PV audit" % (target_pvid, ",".join(paths)))


def _validate_vg(vg, pv_rows):
    try:
        vg_name = _required_text(vg.get("vg_name"), "vg_name")
        vg_uuid = _required_text(vg.get("vg_uuid"), "vg_uuid")
        vg_attr = _required_text(vg.get("vg_attr"), "vg_attr")
        missing_pv_count = _number(vg.get("vg_missing_pv_count"), "vg_missing_pv_count")
        expected_pv_count = _number(vg.get("pv_count"), "pv_count")
    except ValueError as error:
        _error("LVM_RANGE_VG_METADATA_INVALID", str(error))
    if len(vg_attr) < 4 or vg_attr[3] == "p":
        _error("LVM_RANGE_VG_PARTIAL", "VG[%s] is partial, attributes[%s]" % (vg_name, vg_attr))

    if missing_pv_count != 0:
        _error("LVM_RANGE_VG_PARTIAL", "VG[%s] has missing PV count[%s]" % (vg_name, missing_pv_count))

    if expected_pv_count <= 0 or expected_pv_count != len(pv_rows):
        _error("LVM_RANGE_PV_COUNT_MISMATCH", "VG[%s] expects[%s] PV rows but report contains[%s]" % (vg_name, expected_pv_count, len(pv_rows)))
    return vg_uuid


def _validate_pvs(pv_rows, expected_vg_uuid, extent_size):
    pvids = set()
    for pv_name, pv in pv_rows.items():
        if _required_text(pv.get("vg_uuid"), "pv.vg_uuid") != expected_vg_uuid:
            _error("LVM_RANGE_PV_VG_MISMATCH", "PV[%s] belongs to unexpected VG UUID[%s]" % (pv_name, pv.get("vg_uuid")))

        pvid = _text(pv.get("pv_uuid"))
        if not pvid:
            _error("LVM_RANGE_PVID_MISSING", "PV[%s] has no PVID" % pv_name)
        if pvid in pvids:
            _error("LVM_RANGE_PVID_DUPLICATE", "PVID[%s] occurs more than once in the target VG" % pvid)
        pvids.add(pvid)

        if _flag_is_set(pv.get("pv_missing")):
            _error("LVM_RANGE_PV_MISSING", "PV[%s] is reported missing" % pv_name)
        if _flag_is_set(pv.get("pv_duplicate")):
            _error("LVM_RANGE_PV_DUPLICATE", "PV[%s] is an unchosen duplicate" % pv_name)

        try:
            pe_start = _number(pv.get("pe_start"), "pe_start")
            pv_size = _number(pv.get("pv_size"), "pv_size")
            dev_size = _number(pv.get("dev_size"), "dev_size")
            pe_count = _number(pv.get("pv_pe_count"), "pv_pe_count")
        except ValueError as error:
            _error("LVM_RANGE_PV_METADATA_INVALID", "PV[%s]: %s" % (pv_name, error))
        data_size = pe_count * extent_size
        if (pe_start < 0 or pv_size <= 0 or dev_size <= 0 or pe_count <= 0 or data_size > pv_size or pv_size > dev_size or pe_start + data_size > dev_size):
            _error("LVM_RANGE_PV_BOUNDARY", "PV[%s] has invalid data-area boundary: pe_start[%s], pv_size[%s], dev_size[%s], pe_count[%s], extent_size[%s]" %
                   (pv_name, pe_start, pv_size, dev_size, pe_count, extent_size))


def validate_lvm_metadata(vg_report, pv_report):
    vg_rows = _report_rows(vg_report, "vg")
    if len(vg_rows) != 1:
        _error("LVM_RANGE_VG_METADATA_INVALID", "expected exactly one VG report row, got[%s]" % len(vg_rows))
    vg = vg_rows[0]
    extent_size = _number(vg.get("vg_extent_size"), "vg_extent_size")
    if extent_size <= 0:
        _error("LVM_RANGE_VG_METADATA_INVALID", "VG extent size must be positive")
    try:
        pv_rows = _pv_rows_by_name(pv_report)
    except ValueError as error:
        _error("LVM_RANGE_PV_METADATA_INVALID", str(error))
    vg_uuid = _validate_vg(vg, pv_rows)
    _validate_pvs(pv_rows, vg_uuid, extent_size)
    return vg, pv_rows, vg_uuid, extent_size


def vg_seqno(vg_report):
    rows = _report_rows(vg_report, "vg")
    if len(rows) != 1:
        _error("LVM_RANGE_VG_METADATA_INVALID", "cannot get a unique VG sequence number")
    try:
        return _number(rows[0].get("vg_seqno"), "vg_seqno")
    except ValueError as error:
        _error("LVM_RANGE_VG_METADATA_INVALID", str(error))


def _validate_block_device(pv_name, pv, device):
    topology = _required_text(_value(device, "topology"), "block-device.topology")
    if topology not in ("disk", "mpath"):
        _error("LVM_RANGE_TOPOLOGY_UNSUPPORTED", "PV[%s] resolves to unsupported topology[%s]" % (pv_name, topology))

    try:
        wwid = _required_text(_value(device, "wwid"), "block-device.wwid")
    except ValueError as error:
        _error("LVM_RANGE_WWID_MISSING", "PV[%s]: %s" % (pv_name, error))
    path_capacities = list(_value(device, "pathCapacities", []) or [])
    if not path_capacities:
        _error("LVM_RANGE_DEVICE_CAPACITY_MISSING", "no capacity evidence exists for PV[%s]" % pv_name)
    capacity_values = []
    for path_capacity in path_capacities:
        path = _required_text(_value(path_capacity, "path"), "block-device.path-capacity.path")
        path_size = _number(_value(path_capacity, "size"), "block-device.path-capacity.size")
        if path_size <= 0:
            _error("LVM_RANGE_DEVICE_CAPACITY_MISSING", "path[%s] for PV[%s] has no positive capacity" % (path, pv_name))
        capacity_values.append(path_size)
    if topology == "mpath" and (len(capacity_values) < 2 or len(set(capacity_values)) != 1):
        _error("LVM_RANGE_MULTIPATH_CAPACITY_MISMATCH", "multipath PV[%s] has inconsistent map/slave capacities[%s]" % (pv_name, capacity_values))

    capacity = _number(_value(device, "size"), "block device size")
    if capacity <= 0:
        _error("LVM_RANGE_DEVICE_CAPACITY_MISSING", "block device capacity must be positive for PV[%s]" % pv_name)
    pv_device_size = _number(pv.get("dev_size"), "pv.dev_size")
    if capacity != pv_device_size or capacity not in capacity_values:
        _error("LVM_RANGE_DEVICE_CAPACITY_MISMATCH", "LVM device size[%s] and canonical device capacity[%s] differ for PV[%s]" % (pv_device_size, capacity, pv_name))

    canonical_path = _required_text(_value(device, "canonicalPath"), "block-device.canonicalPath")
    return {"device": device, "wwid": wwid, "capacity": capacity, "identity": (_required_text(pv.get("pv_uuid"), "pv_uuid"), canonical_path)}


def _validate_block_devices(pv_rows, block_devices):
    by_path = _index_paths(_device_paths(device) for device in block_devices)
    result = {}
    by_wwid = {}
    for pv_name, pv in pv_rows.items():
        matches = by_path.get(pv_name, set()) | by_path.get(os.path.realpath(pv_name), set())
        if not matches:
            raise ValueError("cannot resolve block device for PV[%s]" % pv_name)
        if len(matches) != 1:
            raise ValueError("ambiguous block-device mapping for PV[%s]" % pv_name)
        device = block_devices[matches.pop()]
        validated = _validate_block_device(pv_name, pv, device)
        result[pv_name] = validated
        existing = by_wwid.get(validated["wwid"])
        if existing is None:
            by_wwid[validated["wwid"]] = validated
            continue
        if existing["capacity"] != validated["capacity"]:
            _error("LVM_RANGE_WWID_CAPACITY_MISMATCH", "WWID[%s] has conflicting capacities[%s,%s]" % (validated["wwid"], existing["capacity"], validated["capacity"]))
        if existing["identity"] != validated["identity"]:
            _error("LVM_RANGE_WWID_AMBIGUOUS", "WWID[%s] resolves to distinct PV/device identities" % validated["wwid"])
    return result


def _build_range(row, pv_rows, validated_devices, extent_size, lv_size, expected_vg_uuid):
    lv_name = _required_text(row.get("lv_name"), "lv_name")
    segtype = _required_text(row.get("segtype"), "segtype")
    if segtype != "linear":
        _error("LVM_RANGE_SEGMENT_TYPE_UNSUPPORTED", "segment type[%s] for LV[%s] is unsupported" % (segtype, lv_name))
    if _required_text(row.get("vg_uuid"), "vg_uuid") != expected_vg_uuid:
        raise ValueError("LV[%s] belongs to unexpected VG UUID[%s]" % (lv_name, row.get("vg_uuid")))

    pv_name, pv_extent_start, pv_extent_count = _parse_pe_range(row.get("seg_pe_ranges", ""))
    extent_count = _number(row.get("seg_size_pe"), "seg_size_pe")
    if extent_count <= 0:
        raise ValueError("segment extent count must be positive for LV[%s]" % lv_name)
    if pv_extent_count != extent_count:
        raise ValueError("segment extent count[%s] does not match PV range count[%s] for LV[%s]" % (extent_count, pv_extent_count, lv_name))

    pv = pv_rows.get(pv_name)
    if pv is None:
        raise ValueError("cannot find PV metadata for device[%s]" % pv_name)
    if _required_text(pv.get("vg_uuid"), "pv.vg_uuid") != expected_vg_uuid:
        raise ValueError("PV[%s] belongs to unexpected VG UUID[%s]" % (pv_name, pv.get("vg_uuid")))
    validated_device = validated_devices[pv_name]
    wwid = validated_device["wwid"]

    seg_start_pe = _number(row.get("seg_start_pe"), "seg_start_pe")
    if seg_start_pe < 0:
        raise ValueError("negative segment start for LV[%s]" % lv_name)
    lv_offset = seg_start_pe * extent_size
    if lv_offset >= lv_size:
        raise ValueError("segment starts beyond LV size for LV[%s]" % lv_name)
    length = min(extent_count * extent_size, lv_size - lv_offset)

    pv_data_offset = _number(pv.get("pe_start"), "pe_start")
    if pv_data_offset < 0:
        raise ValueError("negative PV data offset for PV[%s]" % pv_name)
    lun_offset = pv_data_offset + pv_extent_start * extent_size
    if length <= 0 or lun_offset < 0:
        raise ValueError("invalid physical range for LV[%s]" % lv_name)

    capacity = validated_device["capacity"]
    pv_pe_count = _number(pv.get("pv_pe_count"), "pv_pe_count")
    if pv_extent_start + extent_count > pv_pe_count:
        _error("LVM_RANGE_PV_EXTENT_OVERFLOW", "segment range[%s:%s] exceeds PV[%s] extent count[%s]" % (pv_extent_start, pv_extent_start + extent_count - 1, pv_name, pv_pe_count))
    if lun_offset + length > capacity:
        raise ValueError("physical range exceeds block device capacity for PV[%s]" % pv_name)

    return ({"wwid": wwid, "lvOffsetBytes": lv_offset, "lunOffsetBytes": lun_offset, "lengthBytes": length}, {"wwid": wwid, "capacityBytes": capacity})


def _validate_complete_ranges(lv_name, lv_size, ranges):
    expected_offset = 0
    for item in ranges:
        if item["lvOffsetBytes"] != expected_offset:
            raise ValueError("LV[%s] ranges contain a gap or overlap at offset[%s]" % (lv_name, expected_offset))
        expected_offset += item["lengthBytes"]
    if expected_offset != lv_size:
        raise ValueError("LV[%s] ranges cover[%s] bytes but LV size is[%s]" % (lv_name, expected_offset, lv_size))

    ranges_by_wwid = {}
    for item in ranges:
        ranges_by_wwid.setdefault(item["wwid"], []).append(item)
    for wwid, physical_ranges in ranges_by_wwid.items():
        physical_ranges = sorted(physical_ranges, key=lambda value: value["lunOffsetBytes"])
        previous_end = -1
        for item in physical_ranges:
            if item["lunOffsetBytes"] < previous_end:
                _error("LVM_RANGE_PHYSICAL_OVERLAP", "LV[%s] has overlapping physical ranges on LUN[%s]" % (lv_name, wwid))
            previous_end = item["lunOffsetBytes"] + item["lengthBytes"]


def build_lv_range_descriptors(vg_report, lv_report, pv_report, block_devices, targets):
    vg, pv_rows, vg_uuid, extent_size = validate_lvm_metadata(vg_report, pv_report)
    lv_rows = {}
    for row in _report_rows(lv_report, "seg"):
        name = _required_text(row.get("lv_name"), "lv_name")
        lv_rows.setdefault(name, []).append(row)
    vg_name = _required_text(vg.get("vg_name"), "vg_name")
    validated_devices = _validate_block_devices(pv_rows, block_devices)
    descriptors = []
    luns = []
    lun_by_wwid = {}
    seen_resource_uuids = set()

    for target in targets:
        resource_uuid = _required_text(_value(target, "resourceUuid"), "resourceUuid")
        if resource_uuid in seen_resource_uuids:
            raise ValueError("duplicate descriptor target resource UUID[%s]" % resource_uuid)
        seen_resource_uuids.add(resource_uuid)

        absolute_path, target_vg_name, lv_name = _target_lv(target)
        if target_vg_name != vg_name:
            raise ValueError("target LV[%s] belongs to VG[%s], expected[%s]" % (absolute_path, target_vg_name, vg_name))

        rows = lv_rows.get(lv_name, [])
        if not rows:
            raise ValueError("cannot find LV metadata for target[%s]" % absolute_path)

        lv_uuid = _required_text(rows[0].get("lv_uuid"), "lv_uuid")
        lv_size = _number(rows[0].get("lv_size"), "lv_size")
        if lv_size <= 0:
            raise ValueError("LV size must be positive for target[%s]" % absolute_path)
        for row in rows:
            if (_required_text(row.get("lv_uuid"), "lv_uuid") != lv_uuid or _number(row.get("lv_size"), "lv_size") != lv_size):
                raise ValueError("inconsistent LV segment metadata for target[%s]" % absolute_path)

        resolved = [_build_range(row, pv_rows, validated_devices, extent_size, lv_size, vg_uuid) for row in rows]
        ranges = [item[0] for item in resolved]
        ranges.sort(key=lambda value: value["lvOffsetBytes"])
        _validate_complete_ranges(lv_name, lv_size, ranges)

        for unused_range, lun in resolved:
            _append_lun(lun, luns, lun_by_wwid)

        descriptors.append({"resourceUuid": resource_uuid, "ranges": ranges})

    return {"luns": luns, "descriptors": descriptors}


# SharedBlock LAN-free report collection and layout assembly.
def _run_lvm_json_report(command):
    from zstacklib.utils import shell

    cmd = shell.ShellCmd("timeout %s %s" % (LVM_REPORT_TIMEOUT_SECONDS, command))
    cmd(is_exception=False)
    if cmd.return_code != 0:
        raise Exception("failed to query LVM range metadata, command[%s], error[%s]" % (command, cmd.stderr))
    try:
        return simplejson.loads(cmd.stdout)
    except Exception:
        raise Exception("failed to parse LVM JSON report from command[%s], output[%s]" % (command, cmd.stdout))


def _get_vg_range_report(vg_uuid):
    from zstacklib.utils import linux

    return _run_lvm_json_report("vgs --readonly --nolocking -t --units b --nosuffix --reportformat json -o %s %s" % (",".join(VG_RANGE_REPORT_FIELDS), linux.shellquote(vg_uuid)))


def _get_lv_range_report(absolute_install_paths):
    from zstacklib.utils import linux

    quoted_paths = " ".join([linux.shellquote(path) for path in absolute_install_paths])
    return _run_lvm_json_report("lvs --readonly --nolocking -t --segments --units b --nosuffix --reportformat json -o vg_uuid,lv_uuid,lv_name,lv_size,segtype,seg_start_pe,seg_size_pe,seg_pe_ranges %s" % quoted_paths)


def _get_pv_range_report(vg_uuid):
    from zstacklib.utils import linux

    return _run_lvm_json_report("pvs --readonly --nolocking -t --units b --nosuffix --reportformat json -o %s -S %s" % (",".join(PV_RANGE_REPORT_FIELDS), linux.shellquote("vg_name=%s" % vg_uuid)))


def _get_pv_duplicate_audit_report():
    return _run_lvm_json_report("pvs --readonly --nolocking -t --all --units b --nosuffix --reportformat json -o %s" % (",".join(PV_DUPLICATE_AUDIT_FIELDS)))


def _candidate_paths(candidate):
    paths = []
    dev_name = getattr(candidate, "dev_name", None)
    if dev_name:
        paths.append("/dev/%s" % dev_name)
    multipath_path = getattr(candidate, "multipathPath", None)
    if multipath_path:
        paths.append("/dev/mapper/%s" % multipath_path)
    by_path = getattr(candidate, "path", None)
    if by_path:
        paths.append("/dev/disk/by-path/%s" % by_path)

    wwid = getattr(candidate, "wwid", None)
    if wwid:
        paths.extend(["/dev/disk/by-id/scsi-%s" % wwid, "/dev/disk/by-id/dm-uuid-mpath-%s" % wwid, "/dev/mapper/%s" % wwid])

    paths.extend([os.path.realpath(path) for path in list(paths) if os.path.exists(path)])
    return list(set(paths))


def _get_block_device_capacity(path):
    from zstacklib.utils import linux, shell

    return shell.call("blockdev --getsize64 %s" % linux.shellquote(path))


def _list_block_device_slaves(canonical_path):
    device_name = os.path.basename(canonical_path)
    slave_path = "/sys/class/block/%s/slaves" % device_name
    try:
        return os.listdir(slave_path)
    except OSError as error:
        raise Exception("LVM_RANGE_MULTIPATH_TOPOLOGY_INCOMPLETE: cannot enumerate slaves for device[%s]: %s" % (canonical_path, error))


def _get_lv_range_block_devices(pv_report):
    from zstacklib.utils import lvm

    pv_names = [_required_text(row.get("pv_name"), "pv_name") for row in _report_rows(pv_report, "pv") if not _flag_is_set(row.get("pv_missing"))]
    candidates = [(candidate, _candidate_paths(candidate)) for candidate in lvm.get_block_devices()]
    return resolve_pv_block_devices(pv_names, candidates, os.path.realpath, _list_block_device_slaves, _get_block_device_capacity)


def get_lv_range_descriptors(vg_uuid, targets):
    from zstacklib.utils import log

    if not targets:
        raise Exception("at least one LV range descriptor target is required")

    absolute_install_paths = []
    for target in targets:
        absolute_path = _value(target, "absoluteInstallPath")
        if not absolute_path:
            raise Exception("target[%s] does not contain an absolute LV install path" % _value(target, "resourceUuid"))
        absolute_install_paths.append(absolute_path)

    logger = log.get_logger(__name__)
    for attempt in range(3):
        vg_report_before = _get_vg_range_report(vg_uuid)
        lv_report = _get_lv_range_report(absolute_install_paths)
        pv_report = _get_pv_range_report(vg_uuid)
        vg_report_metadata_after = _get_vg_range_report(vg_uuid)
        before_seqno = vg_seqno(vg_report_before)
        metadata_after_seqno = vg_seqno(vg_report_metadata_after)
        if before_seqno != metadata_after_seqno:
            logger.warning("VG[%s] metadata changed from seqno[%s] to [%s] while collecting LVM reports, retry[%s/3]" %
                           (vg_uuid, before_seqno, metadata_after_seqno, attempt + 1))
            continue

        validate_lvm_metadata(vg_report_metadata_after, pv_report)
        pv_audit_report = _get_pv_duplicate_audit_report()
        validate_pv_duplicate_audit(pv_report, pv_audit_report)
        block_devices = _get_lv_range_block_devices(pv_report)
        vg_report_device_after = _get_vg_range_report(vg_uuid)
        device_after_seqno = vg_seqno(vg_report_device_after)
        if metadata_after_seqno != device_after_seqno:
            logger.warning("VG[%s] metadata changed from seqno[%s] to [%s] while collecting block devices, retry[%s/3]" %
                           (vg_uuid, metadata_after_seqno, device_after_seqno, attempt + 1))
            continue

        return build_lv_range_descriptors(vg_report_device_after, lv_report, pv_report, block_devices, targets)

    _error("LVM_RANGE_METADATA_CHANGED", "VG[%s] metadata changed while collecting LV range descriptors" % vg_uuid)


def _validate_lvm_identifier(value, name):
    if (not value or value in (".", "..") or _LVM_IDENTIFIER_PATTERN.match(value) is None):
        raise ValueError("invalid SharedBlock %s[%s]" % (name, value))
    return value


def absolute_install_path(vg_uuid, install_path, identity, resource_kind="resource"):
    _validate_lvm_identifier(vg_uuid, "VG name")
    if resource_kind not in ("resource", "snapshot", "volume"):
        raise ValueError("invalid LAN-free resource kind[%s]" % resource_kind)
    prefix = "sharedblock://%s/" % vg_uuid
    if not install_path or not install_path.startswith(prefix):
        raise ValueError("%s[%s] install path[%s] does not belong to SharedBlock VG[%s]" % (resource_kind, identity, install_path, vg_uuid))
    lv_name = install_path[len(prefix):]
    try:
        _validate_lvm_identifier(lv_name, "LV name")
    except ValueError:
        raise ValueError("%s[%s] has invalid SharedBlock LV install path[%s]" % (resource_kind, identity, install_path))
    return "/dev/%s/%s" % (vg_uuid, lv_name)


def _validate_chain_paths(vg_uuid, chain):
    _validate_lvm_identifier(vg_uuid, "VG name")
    if not chain:
        raise ValueError("qcow2 backing chain is empty")
    if any(not path for path in chain):
        raise ValueError("qcow2 backing chain contains an empty path")
    if len(set(chain)) != len(chain):
        raise ValueError("qcow2 backing chain contains a cycle or duplicate path")
    prefix = "/dev/%s/" % vg_uuid
    for path in chain:
        if not path or not path.startswith(prefix):
            raise ValueError("backing path[%s] does not belong to SharedBlock VG[%s]" % (path, vg_uuid))
        lv_name = path[len(prefix):]
        try:
            _validate_lvm_identifier(lv_name, "LV name")
        except ValueError:
            raise ValueError("invalid SharedBlock backing LV path[%s]" % path)


def install_path_from_absolute(vg_uuid, absolute_path):
    _validate_lvm_identifier(vg_uuid, "VG name")
    prefix = "/dev/%s/" % vg_uuid
    if not absolute_path.startswith(prefix):
        raise ValueError("absolute LV path[%s] does not belong to SharedBlock VG[%s]" % (absolute_path, vg_uuid))
    lv_name = absolute_path[len(prefix):]
    _validate_lvm_identifier(lv_name, "LV name")
    return "sharedblock://%s/%s" % (vg_uuid, lv_name)


def build_source_plan(vg_uuid, target, chain):
    snapshot_uuid = _value(target, "volumeSnapshotUuid")
    if not snapshot_uuid or not snapshot_uuid.strip():
        raise ValueError("volumeSnapshotUuid must be non-empty")
    snapshot_path = absolute_install_path(vg_uuid, _value(target, "volumeSnapshotInstallPath"), snapshot_uuid, "snapshot")

    paths = list(chain or [])
    _validate_chain_paths(vg_uuid, paths)
    if paths[0] != snapshot_path:
        raise ValueError("volume snapshot path[%s] is not the backing chain head[%s]" % (snapshot_path, paths[0]))

    range_targets = [{"resourceUuid": "%s:%s" % (snapshot_uuid, index), "absoluteInstallPath": path} for index, path in enumerate(paths)]

    return {"vgUuid": vg_uuid, "paths": paths, "rangeTargets": range_targets}


def build_source_layout(target, plan, range_result, formats, lv_sizes, virtual_size):
    expected_ids = [_value(item, "resourceUuid") for item in plan["rangeTargets"]]
    ranges_by_id = {}
    for descriptor in range_result.get("descriptors", []):
        resource_uuid = descriptor.get("resourceUuid")
        if resource_uuid in ranges_by_id:
            raise ValueError("duplicate range descriptor[%s]" % resource_uuid)
        ranges_by_id[resource_uuid] = descriptor.get("ranges")
    if set(ranges_by_id.keys()) != set(expected_ids):
        raise ValueError("mismatched range descriptors, expected[%s], actual[%s]" % (expected_ids, list(ranges_by_id.keys())))

    layers = []
    layer_count = len(plan["paths"])
    for index, path in enumerate(plan["paths"]):
        identity = expected_ids[index]
        layers.append({
            "layerIndex": index,
            "parentLayerIndex": index + 1 if index + 1 < layer_count else None,
            "sourceInstallPath": install_path_from_absolute(plan["vgUuid"], path),
            "format": formats[path],
            "lvSizeBytes": lv_sizes[path],
            "ranges": ranges_by_id[identity]
        })

    return {"volumeSnapshotUuid": _value(target, "volumeSnapshotUuid"), "volumeUuid": _value(target, "volumeUuid"), "virtualSizeBytes": virtual_size, "layers": layers}


def build_volume_layout(target, plan, range_result, image_format, lv_size, virtual_size):
    volume_uuid = _value(target, "volumeUuid")
    if (image_format not in ("qcow2", "raw") or not _positive_integer(lv_size) or not _positive_integer(virtual_size)):
        raise ValueError("invalid volume layout metadata")
    descriptors = range_result.get("descriptors", [])
    if (len(descriptors) != 1 or descriptors[0].get("resourceUuid") != volume_uuid):
        raise ValueError("mismatched range descriptors for volume[%s]" % volume_uuid)
    ranges = descriptors[0].get("ranges")
    luns = {}
    for lun in range_result.get("luns", []):
        wwid = _value(lun, "wwid")
        capacity = _value(lun, "capacityBytes")
        if not wwid or not _positive_integer(capacity) or wwid in luns:
            raise ValueError("invalid volume LUN descriptor")
        luns[wwid] = capacity
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("invalid volume ranges")
    expected_offset = 0
    for item in ranges:
        wwid = _value(item, "wwid")
        offset = _value(item, "lvOffsetBytes")
        lun_offset = _value(item, "lunOffsetBytes")
        length = _value(item, "lengthBytes")
        if wwid not in luns:
            raise ValueError("volume range references unknown LUN[%s]" % wwid)
        if (offset != expected_offset or not _nonnegative_integer(lun_offset) or not _positive_integer(length)
                or expected_offset + length > lv_size or lun_offset + length > luns[wwid]):
            raise ValueError("invalid volume range at offset[%s]" % expected_offset)
        expected_offset += length
    if expected_offset != lv_size:
        raise ValueError("volume ranges do not cover LV")
    return {
        "volumeUuid": volume_uuid,
        "volumeInstallPath": install_path_from_absolute(plan["vgUuid"], plan["absoluteInstallPath"]),
        "format": image_format,
        "virtualSizeBytes": virtual_size,
        "lvSizeBytes": lv_size,
        "ranges": ranges
    }


def _nonnegative_integer(value):
    return (not isinstance(value, bool) and isinstance(value, _INTEGER_TYPES) and value >= 0)


def _positive_integer(value):
    return _nonnegative_integer(value) and value > 0


def _lv_size_bytes(value):
    # lvm.get_lv_size returns decimal text for both ordinary and thin LVs.
    if isinstance(value, _STRING_TYPES):
        if not re.match(r"^[0-9]+$", value):
            raise ValueError("invalid LV size")
        value = int(value)
    if not _positive_integer(value):
        raise ValueError("invalid LV size")
    return value


def collect_volume_layouts(vg_uuid, targets, operate_lv, get_format, get_lv_size, get_virtual_size):
    if not vg_uuid or not targets:
        raise ValueError("vgUuid and at least one Volume LAN-free target are required")
    layouts = []
    lun_groups = []
    for target in targets:
        volume_uuid = _value(target, "volumeUuid")
        if not volume_uuid or not volume_uuid.strip():
            raise ValueError("volumeUuid must be non-empty")
        absolute_path = absolute_install_path(vg_uuid, _value(target, "volumeInstallPath"), volume_uuid, "volume")
        plan = {"vgUuid": vg_uuid, "absoluteInstallPath": absolute_path, "rangeTargets": [{"resourceUuid": volume_uuid, "absoluteInstallPath": absolute_path}]}
        with operate_lv(absolute_path, shared=True, delete_when_exception=False):
            range_result = get_lv_range_descriptors(vg_uuid, plan["rangeTargets"])
            image_format = get_format(absolute_path)
            lv_size = _lv_size_bytes(get_lv_size(absolute_path))
            virtual_size = (lv_size if image_format == "raw" else get_virtual_size(absolute_path))
            layout = build_volume_layout(target, plan, range_result, image_format, lv_size, virtual_size)
        layouts.append(layout)
        lun_groups.append(range_result["luns"])
    return {"luns": merge_luns(lun_groups), "layouts": layouts}


def _append_lun(lun, result, by_wwid):
    wwid = _value(lun, "wwid")
    capacity = _value(lun, "capacityBytes")
    if not wwid or capacity is None:
        raise ValueError("invalid LUN descriptor")
    existing = by_wwid.get(wwid)
    if existing is None:
        by_wwid[wwid] = lun
        result.append(lun)
        return
    if _value(existing, "capacityBytes") != capacity:
        raise ValueError("conflicting capacities for LUN[%s]" % wwid)


def merge_luns(lun_groups):
    result = []
    by_wwid = {}
    for lun in chain.from_iterable(lun_groups):
        _append_lun(lun, result, by_wwid)
    return result
