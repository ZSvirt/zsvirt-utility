"""
NVMe-oF (fabrics) native multipath: keep disk_device_status/state alive (GapA)
and make the throughput/IOPS/latency *Wwid charts light up (GapB), via
IDENTIFICATION only -- no kvmagent metric backfill, no control-plane change.

GapA (crash that emptied disk_device_status/state):
  collect_nvme_disk_stat used to enumerate /sys/class/nvme/<ctrl>, which on a
  fabrics host lists the *hidden controller-path* namespaces (e.g. nvme2c2n1).
  Those carry a wwid file (so they passed the old filter) but have NO
  /sys/block/<name>/dev node. generate() then did
      linux.read_file("/sys/block/nvme2c2n1/dev").strip()
  read_file returned None -> .strip() -> AttributeError, which propagated out of
  collect_disk_stat and emptied BOTH disk_device_status and disk_device_state
  for the whole host (observed 5580 crashes/day on 172.25.15.138).
  A naive "report the head instead" is also wrong: the fabrics head nvme2n1 has
  a dev node but NO /sys/block/nvme2n1/device/state, and it IS an LVM PV, so a
  blank state would convert to 0 and raise a false PV-abnormal alarm. The real
  state lives on the sibling controller /sys/class/nvme/nvme2/state.

GapB (charts empty): the head's /proc/diskstats row is all-zero; the real I/O
  accounts on the hidden c-path. collectd collects the c-path once its Disk
  regex is widened, emitting collectd_disk_disk_octets/ops/io_time on the c-path
  disk label. The chart join
      irate(collectd_disk_disk_octets_*) * on(disk,hostUuid) group_left(wwid) node_disk_wwid
  then only lights if node_disk_wwid ALSO carries the c-path under the head wwid.

These tests pin both contracts:
  TestCollectNvmeDiskStatFabrics  - GapA: c-path presence must not crash; fabrics
      heads reported via controller state; c-paths never reported as a head.
  TestNvmeControllerPaths         - GapB: the pure head->c-path enumerator.
  TestCollectNodeDiskWwidCpath    - GapB: node_disk_wwid registers the c-path
      under the head's identical wwid (right table of the join).

Style follows test_prometheus_disk_state.py (import-or-skip + mock.patch.object).
"""
import unittest
import os
import re

try:
    from kvmagent.plugins import prometheus
except ImportError as e:
    raise unittest.SkipTest(
        "kvmagent package not importable (%s); "
        "run with PYTHONPATH=kvmagent:zstacklib" % e)

import mock

# convert_state_to_int contract: 1 for running/live, else 0.
RUNNING, NOT_RUNNING = 1, 0

# Real /sys/block top level on zsv-new (172.25.15.138): local PCIe head nvme0n1,
# fabrics subsystems 2 and 3. nvmeXcYnZ are the hidden controller-path devices.
SYS_BLOCK_ENTRIES = [
    "nvme0n1",
    "nvme2c2n1", "nvme2c2n2", "nvme2c2n3",
    "nvme2n1", "nvme2n2", "nvme2n3",
    "nvme3c3n1",
    "nvme3n1",
]

# /sys/block/<head>/device subsystem dir listing (the sibling controller is nvme2/nvme3).
SUBSYS_ENTRIES = {
    "nvme2n1": ["iopolicy", "model", "nvme2", "nvme2n1", "subsysnqn"],
    "nvme2n2": ["iopolicy", "model", "nvme2", "nvme2n2", "subsysnqn"],
    "nvme2n3": ["iopolicy", "model", "nvme2", "nvme2n3", "subsysnqn"],
    "nvme3n1": ["iopolicy", "model", "nvme3", "nvme3n1", "subsysnqn"],
}

# dev node: heads have one, hidden c-paths do not (None).
DEV_NODE = {
    "nvme0n1": "259:0",
    "nvme2n1": "259:53", "nvme2n2": "259:56", "nvme2n3": "259:58",
    "nvme3n1": "259:62",
    "nvme2c2n1": None, "nvme2c2n2": None, "nvme2c2n3": None, "nvme3c3n1": None,
}

# device/state: local PCIe head has it; fabrics heads do not (None).
DEVICE_STATE = {
    "nvme0n1": "live",
    "nvme2n1": None, "nvme2n2": None, "nvme2n3": None, "nvme3n1": None,
}

# controller state (sibling of the fabrics head under the subsystem dir).
CONTROLLER_STATE = {"nvme2": "live", "nvme3": "live"}


def _sample_pairs(metric_family):
    pairs = {}
    for s in metric_family.samples:
        labels = s.labels if hasattr(s, "labels") else s[1]
        value = s.value if hasattr(s, "value") else s[2]
        pairs[labels["disk"]] = value
    return pairs


class TestCollectNvmeDiskStatFabrics(unittest.TestCase):

    def _run(self):
        def fake_exists(path):
            if path == "/sys/class/nvme":
                return True
            if path == "/sys/block":
                return True
            if path == "/sys/class/scsi_disk":
                return False
            return False

        def fake_isdir(path):
            return path.startswith("/sys/block/") and path.endswith("/device")

        def fake_listdir(path):
            if path == "/sys/block":
                return list(SYS_BLOCK_ENTRIES)
            if path.startswith("/sys/block/") and path.endswith("/device"):
                head = path[len("/sys/block/"):-len("/device")]
                return list(SUBSYS_ENTRIES.get(head, []))
            return []

        def fake_read_file_strip(path):
            if path.startswith("/sys/block/") and path.endswith("/dev"):
                return DEV_NODE.get(path[len("/sys/block/"):-len("/dev")])
            if path.startswith("/sys/block/") and path.endswith("/device/state"):
                return DEVICE_STATE.get(path[len("/sys/block/"):-len("/device/state")])
            if path.startswith("/sys/class/nvme/") and path.endswith("/state"):
                return CONTROLLER_STATE.get(path[len("/sys/class/nvme/"):-len("/state")])
            return None

        with mock.patch.object(prometheus.os.path, "exists", side_effect=fake_exists), \
                mock.patch.object(prometheus.os.path, "isdir", side_effect=fake_isdir), \
                mock.patch.object(prometheus.os, "listdir", side_effect=fake_listdir), \
                mock.patch.object(prometheus.linux, "read_file_strip",
                                  side_effect=fake_read_file_strip), \
                mock.patch.object(prometheus, "bash_ro", return_value=(1, "")), \
                mock.patch.object(prometheus, "sblk_pv_vg", {}):
            families = prometheus.collect_disk_stat()

        by_name = {f.name: f for f in families}
        return _sample_pairs(by_name["disk_device_state"]), \
            _sample_pairs(by_name["disk_device_status"])

    def test_does_not_crash_with_cpath_devices_present(self):
        # WHY: the original bug let a hidden c-path (no /sys/block/<n>/dev) reach
        # generate() and raise AttributeError, emptying the whole collector.
        try:
            self._run()
        except AttributeError as e:
            self.fail("collect_disk_stat raised on NVMe-oF c-path layout: %s" % e)

    def test_fabrics_heads_reported_running_via_controller_state(self):
        # WHY: nvme2n1 has no device/state; its state must be read from the
        # sibling controller (/sys/class/nvme/nvme2/state=live) -> running(1),
        # not a blank that converts to 0 and fires a false PV-abnormal alarm.
        state, _ = self._run()
        for head in ("nvme2n1", "nvme2n2", "nvme2n3", "nvme3n1"):
            self.assertEqual(RUNNING, state.get(head),
                             "fabrics head %s must be running(1) via controller "
                             "state, got %s" % (head, state.get(head)))

    def test_any_live_controller_keeps_fabrics_head_running(self):
        # WHY: os.listdir() order is unstable. Native multipath may expose one
        # controller in connecting/deleting while another path is still live; the
        # head must stay healthy if any sibling controller is live/running.
        with mock.patch.dict(SUBSYS_ENTRIES, {
            "nvme2n1": ["nvme99", "nvme2", "nvme2n1", "subsysnqn"]
        }), mock.patch.dict(CONTROLLER_STATE, {"nvme99": "connecting"}):
            state, _ = self._run()

        self.assertEqual(RUNNING, state.get("nvme2n1"))

    def test_local_pcie_head_still_reported(self):
        # WHY: the rewrite must not regress local PCIe NVMe, whose state sits on
        # /sys/block/nvme0n1/device/state directly.
        state, status = self._run()
        self.assertEqual(RUNNING, state.get("nvme0n1"))
        self.assertEqual(1, status.get("nvme0n1"))

    def test_cpath_devices_never_reported(self):
        # WHY: hidden controller-path devices are an implementation detail of
        # native multipath; reporting them would double-count and reintroduce
        # the /sys/block/<c-path>/dev crash surface.
        state, status = self._run()
        for cpath in ("nvme2c2n1", "nvme2c2n2", "nvme2c2n3", "nvme3c3n1"):
            self.assertNotIn(cpath, state,
                             "c-path %s must not appear in disk_device_state" % cpath)
            self.assertNotIn(cpath, status,
                             "c-path %s must not appear in disk_device_status" % cpath)


# --- GapB (throughput/IOPS/latency charts empty) -------------------------------
# Fix is IDENTIFICATION, not data generation. collectd already emits octets/ops/
# io_time for the c-path once its Disk regex is widened to match nvmeXcYnZ. The
# chart PromQL is irate(collectd_disk_disk_octets_*) * on(disk,hostUuid)
# group_left(wwid) node_disk_wwid. The c-path carries the real I/O on its OWN
# disk label (nvme2c2n1), so for the join to light, node_disk_wwid must publish a
# row for that c-path under the SAME wwid as its head. These tests pin that, plus
# the pure c-path enumerator the registration relies on.


class TestNvmeControllerPaths(unittest.TestCase):
    """_nvme_controller_paths(head) enumerates a fabrics head's hidden c-paths.

    Contract: for a head nvmeXnY, return every /sys/block entry matching
    ^nvmeXc\\d+nY$ (its native-multipath controller paths). Anything that is not a
    bare nvme head (partitions, sd*, a c-path itself) yields [] so we never widen
    the registration to non-heads.
    """

    def _call(self, head, sys_block):
        return prometheus._nvme_controller_paths(head, list(sys_block))

    def test_head_returns_only_its_own_cpath(self):
        # WHY: nvme2n1's I/O accounts on nvme2c2n1; that exact c-path (same
        # controller index AND same namespace) is what must inherit the wwid.
        got = self._call("nvme2n1", SYS_BLOCK_ENTRIES)
        self.assertEqual(["nvme2c2n1"], got)

    def test_namespace_must_match_not_just_controller(self):
        # WHY: nvme2c2n2/n3 share the controller but are OTHER LUNs. Matching by
        # controller alone would cross-label wwids between distinct namespaces.
        self.assertEqual(["nvme2c2n3"], self._call("nvme2n3", SYS_BLOCK_ENTRIES))

    def test_local_pcie_head_has_no_cpath(self):
        # WHY: nvme0n1 is single-path PCIe; no c-path exists, so collectd already
        # collects it directly and we must add nothing (no double registration).
        self.assertEqual([], self._call("nvme0n1", SYS_BLOCK_ENTRIES))

    def test_non_head_inputs_yield_empty(self):
        # WHY: the regex anchors ^(nvme\\d+)(n\\d+)$ so a partition, a c-path fed
        # back in, or a SCSI disk can never trigger c-path registration.
        for bogus in ("nvme2c2n1", "nvme2n1p1", "sda", "nvme2", "dm-0"):
            self.assertEqual([], self._call(bogus, SYS_BLOCK_ENTRIES),
                             "%s must not enumerate c-paths" % bogus)


class TestCollectdNvmeDiskRegex(unittest.TestCase):

    def test_head_and_cpath_regex_support_multi_digit_names(self):
        # WHY: collect_disk_stat and node_disk_wwid both accept nvme\d+n\d+ and
        # nvme\d+c\d+n\d+. collectd must use the same contract or devices such
        # as nvme10n1/nvme0n12 are filtered before the chart join.
        source_path = os.path.abspath(prometheus.__file__)
        if source_path.endswith(".pyc"):
            source_path = source_path[:-1]
        with open(source_path) as fd:
            source = fd.read()

        self.assertIsNotNone(re.search(r'Disk\s+"/\^nvme\[0-9\]\+n\[0-9\]\+\$/\"', source))
        self.assertIsNotNone(re.search(r'Disk\s+"/\^nvme\[0-9\]\+c\[0-9\]\+n\[0-9\]\+\$/\"', source))
        self.assertIsNone(re.search(r'Disk\s+"/\^nvme\[0-9\]\[a-z\]\[0-9\]\$/\"', source))


class _FakeUdevDevice(object):
    def __init__(self, props):
        self._props = props

    def get(self, key, default=None):
        return self._props.get(key, default)


# pvs --nolocking output: the fabrics head nvme2n1 and the local PCIe nvme0n1 are
# both LVM PVs. nvme0n1 has no c-path; nvme2n1's real I/O lives on nvme2c2n1.
PVS_OUTPUT = "  /dev/nvme2n1 vgfabrics\n  /dev/nvme0n1 vglocal\n"

WWID_FABRICS = "nvme-uuid.6ebf4e69-0e47-45ae-ad91-879ada5cf37c"
WWID_LOCAL = "nvme-uuid.00000000-0000-0000-0000-0000000pcie0"

DEVLINKS = {
    "/dev/nvme2n1": "/dev/disk/by-id/%s /dev/disk/by-id/lvm-pv-uuid-AAAA" % WWID_FABRICS,
    "/dev/nvme0n1": "/dev/disk/by-id/%s /dev/disk/by-id/lvm-pv-uuid-BBBB" % WWID_LOCAL,
}


def _wwid_pairs(metric_family):
    return set((s.labels["disk"], s.labels["wwid"])
               if hasattr(s, "labels") else (s[1]["disk"], s[1]["wwid"])
               for s in metric_family.samples)


class TestCollectNodeDiskWwidCpath(unittest.TestCase):
    """node_disk_wwid must register a fabrics head's c-path under the head's wwid.

    This is the right table of the chart join. Before the fix it carried only the
    head (nvme2n1), whose collectd series is empty, so the join produced nothing.
    After the fix it also carries nvme2c2n1 with the identical wwid, so the live
    c-path collectd series joins through and the charts light.
    """

    def _run(self):
        def fake_from_device_file(ctx, devpath):
            return _FakeUdevDevice({"DM_UUID": "", "DEVLINKS": DEVLINKS[devpath]})

        def fake_listdir(path):
            self.assertEqual("/sys/block", path)
            return list(SYS_BLOCK_ENTRIES)

        with mock.patch.object(prometheus, "bash_o", return_value=PVS_OUTPUT), \
                mock.patch.object(prometheus.pyudev, "Context", return_value=object()), \
                mock.patch.object(prometheus.pyudev.Device, "from_device_file",
                                  side_effect=fake_from_device_file), \
                mock.patch.object(prometheus.os, "listdir", side_effect=fake_listdir), \
                mock.patch.object(prometheus, "collect_node_disk_wwid_last_time", None), \
                mock.patch.object(prometheus, "collect_node_disk_wwid_last_result", None):
            families = list(prometheus.collect_node_disk_wwid())

        by_name = {f.name: f for f in families}
        return _wwid_pairs(by_name["node_disk_wwid"])

    def test_cpath_registered_under_head_wwid(self):
        # WHY: the c-path is where collectd sees real I/O; it must appear in
        # node_disk_wwid with the head's wwid or the join key (disk) never matches.
        pairs = self._run()
        self.assertIn(("nvme2n1", WWID_FABRICS), pairs)
        self.assertIn(("nvme2c2n1", WWID_FABRICS), pairs,
                      "c-path nvme2c2n1 must inherit the head's wwid")

    def test_cpath_and_head_share_identical_wwid(self):
        # WHY: group_left(wwid) joins on disk; if the c-path's wwid differed by a
        # byte the reporter-side *Wwid aggregation would split into two series.
        pairs = self._run()
        head_wwid = dict(pairs).get("nvme2n1")
        cpath_wwid = dict(pairs).get("nvme2c2n1")
        self.assertEqual(head_wwid, cpath_wwid)

    def test_local_pcie_head_gets_no_cpath_row(self):
        # WHY: nvme0n1 has no c-path; only the head row may exist, else we would
        # publish a phantom disk label that matches no collectd series.
        pairs = self._run()
        self.assertIn(("nvme0n1", WWID_LOCAL), pairs)
        self.assertEqual([], [d for d, _ in pairs if d.startswith("nvme0c")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
