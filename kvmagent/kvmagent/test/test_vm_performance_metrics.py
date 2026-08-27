#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import os
import sys
import types
import unittest

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

        self.assertEqual(6.0, result["cpu"]["readyTimeMs"])
        self.assertEqual(0.03, result["cpu"]["readinessPercent"])
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

        self.assertIn("zstack_vm_cpu_ready_time_milliseconds", names)
        self.assertIn("zstack_vm_disk_read_latency_milliseconds", names)
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


if __name__ == "__main__":
    unittest.main()
