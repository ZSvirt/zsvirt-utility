#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import os
import sys
import types
import unittest
import shutil
import tempfile

try:
    from prometheus_client.core import GaugeMetricFamily  # noqa: F401
except ImportError:
    prometheus_client = types.ModuleType("prometheus_client")
    prometheus_core = types.ModuleType("prometheus_client.core")

    class GaugeMetricFamily(object):
        def __init__(self, name, documentation, value=None, labels=None):
            self.name = name
            self.documentation = documentation
            self.labels = labels or []
            self.samples = []

        def add_metric(self, labels, value):
            self.samples.append((labels, value))

    prometheus_core.GaugeMetricFamily = GaugeMetricFamily
    prometheus_client.core = prometheus_core
    sys.modules["prometheus_client"] = prometheus_client
    sys.modules["prometheus_client.core"] = prometheus_core

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugins"))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import vm_performance_metrics as metrics


class VmPerformanceMetricsTest(unittest.TestCase):
    def test_host_component_last_good_survives_failure_and_recovers(self):
        collector = metrics.VmHostPerformanceCollector()

        def snapshot(at, delay, requests, duration, drops, valid=True):
            return {"capturedAt": at,
                    "cpu": {"version": 15, "runDelayNs": {"cpu0": delay},
                            "valid": valid},
                    "disks": {"sda": {"readRequests": requests,
                                       "readTimeMs": duration,
                                       "writeRequests": requests,
                                       "writeTimeMs": duration}} if valid else {},
                    "interfaces": {"eth0": {"rxDrop": drops,
                                              "txDrop": drops}} if valid else {},
                    "valid": dict((name, valid) for name in
                                  ("cpu", "disks", "interfaces"))}

        collector._update_host_metrics(snapshot(0, 0, 0, 0, 0))
        good = collector._update_host_metrics(snapshot(10, 1000000, 2, 4, 3))
        self.assertEqual(1.0, good["cpu"]["readyTimeMs"])
        self.assertEqual(2.0, good["disks"]["sda"]["readLatencyMs"])
        self.assertEqual(3, good["interfaces"]["eth0"]["rxDrop"])

        failed = collector._update_host_metrics(snapshot(20, 0, 0, 0, 0, False))
        self.assertIs(good, failed)
        self.assertEqual(1.0, failed["cpu"]["readyTimeMs"])

        recovered = collector._update_host_metrics(snapshot(30, 3000000, 6, 12, 8))
        self.assertEqual(2.0, recovered["cpu"]["readyTimeMs"])
        self.assertEqual(2.0, recovered["disks"]["sda"]["readLatencyMs"])
        self.assertEqual(5, recovered["interfaces"]["eth0"]["rxDrop"])

    def test_disk_topology_keeps_top_dm_and_unrelated_raw_disks(self):
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "sda2"))
            open(os.path.join(root, "sda2", "partition"), "w").close()
            for name in ("dm-0", "dm-1", "sda", "sdb", "nvme0n1", "sda2"):
                if name == "sda2":
                    continue
                os.makedirs(os.path.join(root, name, "holders"))
                os.makedirs(os.path.join(root, name, "slaves"))
            os.mkdir(os.path.join(root, "dm-0", "holders", "dm-1"))
            os.mkdir(os.path.join(root, "dm-0", "slaves", "sda2"))
            os.mkdir(os.path.join(root, "dm-1", "slaves", "dm-0"))
            disks = dict((name, {"marker": name}) for name in
                         ("dm-0", "dm-1", "sda", "sdb", "nvme0n1"))
            original_realpath = metrics.os.path.realpath
            metrics.os.path.realpath = lambda path: (
                os.path.join(root, "devices", "sda", "sda2")
                if path == os.path.join(root, "sda2") else original_realpath(path))
            try:
                selected = metrics._select_top_level_disks(disks, root)
            finally:
                metrics.os.path.realpath = original_realpath
            self.assertEqual(set(["dm-1", "sdb", "nvme0n1"]), set(selected))
        finally:
            shutil.rmtree(root)

    def test_host_read_failures_are_explicit(self):
        missing = os.path.join(tempfile.gettempdir(), "does-not-exist-zstack")
        self.assertFalse(metrics._collect_host_cpu(missing)["valid"])
        self.assertIsNone(metrics._collect_host_disks(missing, missing))
        self.assertIsNone(metrics._collect_host_network(missing))

    def test_calculates_vm_cpu_disk_and_network_metrics(self):
        previous = {
            "capturedAt": 10.0,
            "vms": {"vm-1": {
                "vcpuCount": 2,
                "delayNs": {0: 1000000, 1: 2000000},
                "disks": {"vda": {
                    "readRequests": 10, "readTimeNs": 10000000,
                    "writeRequests": 20, "writeTimeNs": 40000000}},
                "interfaces": {"vnet0": {"rxDrop": 2, "txDrop": 3}},
            }},
        }
        current = {
            "capturedAt": 20.0,
            "vms": {"vm-1": {
                "vcpuCount": 2,
                "delayNs": {0: 3000000, 1: 6000000},
                "disks": {"vda": {
                    "readRequests": 12, "readTimeNs": 16000000,
                    "writeRequests": 24, "writeTimeNs": 60000000}},
                "interfaces": {"vnet0": {"rxDrop": 5, "txDrop": 7}},
            }},
        }

        result = metrics.calculate_vm_metrics(previous, current)["vm-1"]

        self.assertEqual({0: 2.0, 1: 4.0}, result["cpu"]["perCpu"])
        self.assertNotIn("readyTimeMs", result["cpu"])
        self.assertNotIn("readinessPercent", result["cpu"])
        self.assertEqual(3.0, result["disks"]["vda"]["readLatencyMs"])
        self.assertEqual(5.0, result["disks"]["vda"]["writeLatencyMs"])
        self.assertEqual(3, result["interfaces"]["vnet0"]["rxDrop"])
        self.assertEqual(4, result["interfaces"]["vnet0"]["txDrop"])

    def test_counter_reset_suppresses_cpu_series(self):
        before = {"vcpuCount": 1, "delayNs": {0: 5000000}}
        now = {"vcpuCount": 1, "delayNs": {0: 1000000}}
        self.assertIsNone(metrics._calculate_cpu(before, now, 10000.0))

    def test_metric_catalog_excludes_memory_and_costop(self):
        families = metrics.build_prometheus_metrics(
            {}, {"cpu": None, "disks": {}, "interfaces": {}})
        names = set(family.name for family in families)
        joined = " ".join(names).lower()

        self.assertNotIn("zstack_vm_cpu_ready_time_milliseconds", names)
        self.assertNotIn("zstack_vm_cpu_readiness_percent", names)
        self.assertNotIn("zstack_vm_disk_read_latency_milliseconds", names)
        self.assertIn("zstack_vm_vcpu_ready_time_milliseconds", names)
        self.assertIn("zstack_vm_disk_read_time_milliseconds", names)
        self.assertIn("zstack_vm_disk_read_requests", names)
        self.assertIn("zstack_vm_network_receive_dropped_packets", names)
        self.assertNotIn("memory", joined)
        self.assertNotIn("balloon", joined)
        self.assertNotIn("costop", joined)
        self.assertNotIn("co_stop", joined)

    def test_host_metrics_are_independent_from_vm_metrics(self):
        previous = {
            "capturedAt": 10.0,
            "cpu": {"version": 15,
                    "runDelayNs": {"cpu0": 1000000, "cpu1": 3000000}},
            "disks": {"dm-0": {"readRequests": 10, "readTimeMs": 20,
                                  "writeRequests": 20, "writeTimeMs": 80}},
            "interfaces": {"eth0": {"rxDrop": 3, "txDrop": 4}},
        }
        current = {
            "capturedAt": 20.0,
            "cpu": {"version": 15,
                    "runDelayNs": {"cpu0": 3000000, "cpu1": 7000000}},
            "disks": {"dm-0": {"readRequests": 14, "readTimeMs": 32,
                                  "writeRequests": 25, "writeTimeMs": 100}},
            "interfaces": {"eth0": {"rxDrop": 5, "txDrop": 7}},
        }

        result = metrics.calculate_host_metrics(previous, current)

        self.assertEqual(6.0, result["cpu"]["readyTimeMs"])
        self.assertEqual(0.03, result["cpu"]["readinessPercent"])
        self.assertEqual(3.0, result["disks"]["dm-0"]["readLatencyMs"])
        self.assertEqual(4.0, result["disks"]["dm-0"]["writeLatencyMs"])
        self.assertEqual(2, result["interfaces"]["eth0"]["rxDrop"])
        self.assertEqual(3, result["interfaces"]["eth0"]["txDrop"])

    @staticmethod
    def samples(families, name):
        family = next(f for f in families if f.name == name)
        # Support both the real prometheus_client and the dependency-free stub.
        return [(s.labels, s.value) if hasattr(s, "labels") else s
                for s in family.samples]

    def test_uuid_normalization_and_batch_collection(self):
        class Domain(object):
            def UUIDString(self):
                return "7C193600-2180-4DED-A7AD-0AF850D84AC4"

        class Connection(object):
            calls = 0

            def getAllDomainStats(self, stats, flags):
                self.calls += 1
                return [(Domain(), {"vcpu.current": 1, "vcpu.0.delay": 0})]

        conn = Connection()
        snapshot = metrics.collect_vm_snapshot(conn)
        vm_uuid = "7c19360021804deda7ad0af850d84ac4"
        self.assertEqual([vm_uuid], list(snapshot["vms"]))
        self.assertEqual(1, conn.calls)
        snapshot["capturedAt"] = 10.0
        after = metrics.collect_vm_snapshot(conn)
        after["capturedAt"] = 20.0
        families = metrics.build_prometheus_metrics(
            metrics.calculate_vm_metrics(snapshot, after),
            {"cpu": None, "disks": {}, "interfaces": {}})
        labels, value = self.samples(
            families, "zstack_vm_vcpu_ready_time_milliseconds")[0]
        self.assertEqual(vm_uuid, labels["vm_uuid"] if isinstance(labels, dict) else labels[0])
        self.assertEqual(0, value)

    def test_vcpu_only_and_actual_interval(self):
        cpu = metrics._calculate_cpu(
            {"vcpuCount": 2, "delayNs": {0: 0, 1: 0}},
            {"vcpuCount": 2, "delayNs": {0: 550000000, 1: 1100000000}},
            55000.0)
        families = metrics.build_prometheus_metrics(
            {"vm": {"cpu": cpu, "disks": {}, "interfaces": {}}},
            {"cpu": None, "disks": {}, "interfaces": {}})
        self.assertEqual([1.0, 2.0], sorted(v for _, v in self.samples(
            families, "zstack_vm_vcpu_readiness_percent")))
        self.assertNotIn("zstack_vm_cpu_ready_time_milliseconds",
                         [f.name for f in families])

    def test_sparse_online_vcpu_ids_are_collected_across_two_samples(self):
        before_count, before_delays = metrics._collect_vcpus({
            "vcpu.current": 2,
            "vcpu.maximum": 3,
            "vcpu.0.delay": 1000000,
            "vcpu.2.wait": 2000000,
        })
        after_count, after_delays = metrics._collect_vcpus({
            "vcpu.current": 2,
            "vcpu.maximum": 3,
            "vcpu.0.delay": 3000000,
            "vcpu.2.wait": 6000000,
        })

        cpu = metrics._calculate_cpu(
            {"vcpuCount": before_count, "delayNs": before_delays},
            {"vcpuCount": after_count, "delayNs": after_delays},
            1000.0)
        families = metrics.build_prometheus_metrics(
            {"vm": {"cpu": cpu, "disks": {}, "interfaces": {}}},
            {"cpu": None, "disks": {}, "interfaces": {}})

        for metric_name in (
                "zstack_vm_vcpu_ready_time_milliseconds",
                "zstack_vm_vcpu_readiness_percent"):
            samples = self.samples(families, metric_name)
            ids = sorted(labels[1] if not isinstance(labels, dict)
                         else labels["vcpu_id"] for labels, _ in samples)
            self.assertEqual(["0", "2"], ids)

    def test_missing_vcpu_or_nonpositive_interval_suppresses_partial_average(self):
        previous = {"vcpuCount": 2, "delayNs": {0: 0, 1: 0}}
        self.assertIsNone(metrics._calculate_cpu(
            previous, {"vcpuCount": 2, "delayNs": {0: 1}}, 1000))
        for interval in (0, -1000):
            self.assertIsNone(metrics._calculate_cpu(previous, previous, interval))

    def test_disk_weight_components_and_no_aggregate_series(self):
        before = dict((name, {"readRequests": 0, "readTime": 0,
                              "writeRequests": 0, "writeTime": 0})
                      for name in ("vda", "vdb"))
        now = {
            "vda": {"readRequests": 1, "readTime": 100000000,
                    "writeRequests": 2, "writeTime": 4000000},
            "vdb": {"readRequests": 100, "readTime": 100000000,
                    "writeRequests": 8, "writeTime": 16000000},
        }
        disks = metrics._calculate_disk(before, now, 1000000.0)
        families = metrics.build_prometheus_metrics(
            {"vm": {"cpu": None, "disks": disks, "interfaces": {}}},
            {"cpu": None, "disks": {}, "interfaces": {}})
        durations = self.samples(families, "zstack_vm_disk_read_time_milliseconds")
        requests = self.samples(families, "zstack_vm_disk_read_requests")
        self.assertEqual(2, len(durations))
        self.assertEqual(2, len(requests))
        self.assertAlmostEqual(200.0 / 101, sum(v for _, v in durations) /
                               sum(v for _, v in requests))

    def test_disk_reset_and_missing_field_suppress_both_components(self):
        previous = {"vda": {"readRequests": 10, "readTime": 100,
                            "writeRequests": 10, "writeTime": 100}}
        for key in ("readRequests", "readTime"):
            for invalid in (None, 0):
                now = {"vda": dict(previous["vda"])}
                now["vda"][key] = invalid
                disk = metrics._calculate_disk(previous, now, 1.0)["vda"]
                self.assertIsNone(disk["readRequests"])
                self.assertIsNone(disk["readTimeMs"])

    def test_disk_idle_exports_zero_components_not_zero_latency(self):
        snapshot = {"vda": {"readRequests": 10, "readTime": 100,
                             "writeRequests": 10, "writeTime": 100}}
        disk = metrics._calculate_disk(snapshot, snapshot, 1.0)["vda"]
        self.assertEqual(0, disk["readRequests"])
        self.assertEqual(0, disk["readTimeMs"])
        self.assertIsNone(disk["readLatencyMs"])


if __name__ == "__main__":
    unittest.main()
