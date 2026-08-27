#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Prometheus collector for KVM VM/host scheduling and I/O metrics.

The collector intentionally does not expose guest memory/balloon metrics or
CPU co-stop.  Counter based metrics need two successful scrapes: the first
scrape establishes the baseline and the next scrape exports interval values.
"""

from __future__ import division

import os
import re
import socket
import threading
import time

from prometheus_client.core import GaugeMetricFamily

try:
    import libvirt
except ImportError:
    libvirt = None


NANOSECONDS_PER_MILLISECOND = 1000000.0


def _libvirt_flag(name):
    return getattr(libvirt, name, 0) if libvirt is not None else 0


def _counter_delta(previous, current):
    if previous is None or current is None or current < previous:
        return None
    return current - previous


def _read_int(path):
    try:
        with open(path) as stream:
            return int(stream.read().strip())
    except (IOError, OSError, ValueError):
        return None


def _collect_host_cpu(path="/proc/schedstat"):
    version = None
    delays = {}
    try:
        with open(path) as stream:
            for line in stream:
                fields = line.split()
                if not fields:
                    continue
                if fields[0] == "version" and len(fields) > 1:
                    version = int(fields[1])
                elif re.match(r"^cpu\d+$", fields[0]) and len(fields) >= 4:
                    # Supported schedstat CPU records end in:
                    # running_ns, run_delay_ns, timeslices.
                    delays[fields[0]] = int(fields[-2])
    except (IOError, OSError, ValueError):
        pass
    return {"version": version, "runDelayNs": delays}


def _is_partition(name, sys_block="/sys/class/block"):
    return os.path.exists(os.path.join(sys_block, name, "partition"))


def _collect_host_disks(path="/proc/diskstats",
                        sys_block="/sys/class/block"):
    disks = {}
    try:
        with open(path) as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 14:
                    continue
                name = fields[2]
                if (name.startswith(("loop", "ram", "nbd", "fd", "sr")) or
                        _is_partition(name, sys_block)):
                    continue
                disks[name] = {
                    "readRequests": int(fields[3]),
                    "readTimeMs": int(fields[6]),
                    "writeRequests": int(fields[7]),
                    "writeTimeMs": int(fields[10]),
                }
    except (IOError, OSError, ValueError):
        pass
    # Avoid double counting device-mapper IO and its backing devices.
    mapped = dict((k, v) for k, v in disks.items() if k.startswith("dm-"))
    return mapped if mapped else disks


def _host_nic_names(sys_net="/sys/class/net"):
    try:
        names = os.listdir(sys_net)
    except (IOError, OSError):
        return []
    ignored = ("vnet", "vnic", "tap", "tun", "virbr", "br", "docker",
               "veth", "vxlan", "genev", "outer", "ud_")
    result = []
    for name in names:
        base = os.path.join(sys_net, name)
        if name == "lo" or name.startswith(ignored):
            continue
        if _read_int(os.path.join(base, "type")) != 1:
            continue
        if not os.path.exists(os.path.join(base, "device")):
            continue
        result.append(name)
    return sorted(result)


def _collect_host_network(sys_net="/sys/class/net"):
    interfaces = {}
    for name in _host_nic_names(sys_net):
        stats = os.path.join(sys_net, name, "statistics")
        interfaces[name] = {
            "rxDrop": _read_int(os.path.join(stats, "rx_dropped")),
            "txDrop": _read_int(os.path.join(stats, "tx_dropped")),
        }
    return interfaces


def collect_host_snapshot():
    return {
        "hostname": socket.gethostname(),
        "capturedAt": time.time(),
        "cpu": _collect_host_cpu(),
        "disks": _collect_host_disks(),
        "interfaces": _collect_host_network(),
    }


def _collect_vcpus(values):
    count = int(values.get("vcpu.current", 0))
    delays = {}
    for vcpu_id in range(count):
        delay_key = "vcpu.%d.delay" % vcpu_id
        wait_key = "vcpu.%d.wait" % vcpu_id
        if delay_key in values:
            delays[vcpu_id] = int(values[delay_key])
        elif wait_key in values:
            delays[vcpu_id] = int(values[wait_key])
    return count, delays


def _collect_vm_disks(values):
    disks = {}
    for disk_id in range(int(values.get("block.count", 0))):
        prefix = "block.%d." % disk_id
        name = values.get(prefix + "name", str(disk_id))
        disks[name] = {
            "readRequests": _optional_int(values, prefix + "rd.reqs"),
            "readTimeNs": _optional_int(values, prefix + "rd.times"),
            "writeRequests": _optional_int(values, prefix + "wr.reqs"),
            "writeTimeNs": _optional_int(values, prefix + "wr.times"),
        }
    return disks


def _collect_vm_interfaces(values):
    interfaces = {}
    for nic_id in range(int(values.get("net.count", 0))):
        prefix = "net.%d." % nic_id
        name = values.get(prefix + "name", str(nic_id))
        interfaces[name] = {
            "rxDrop": _optional_int(values, prefix + "rx.drop"),
            "txDrop": _optional_int(values, prefix + "tx.drop"),
        }
    return interfaces


def _optional_int(values, key):
    return int(values[key]) if key in values else None


def collect_vm_snapshot(conn):
    """Collect all active VMs with one libvirt getAllDomainStats call."""
    stats = (_libvirt_flag("VIR_DOMAIN_STATS_VCPU") |
             _libvirt_flag("VIR_DOMAIN_STATS_BLOCK") |
             _libvirt_flag("VIR_DOMAIN_STATS_INTERFACE"))
    flags = _libvirt_flag("VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE")
    records = conn.getAllDomainStats(stats, flags)
    snapshot = {}
    for domain, values in records:
        count, delays = _collect_vcpus(values)
        snapshot[domain.UUIDString()] = {
            "vcpuCount": count,
            "delayNs": delays,
            "disks": _collect_vm_disks(values),
            "interfaces": _collect_vm_interfaces(values),
        }
    return {"capturedAt": time.time(), "vms": snapshot}


def _calculate_cpu(previous, current, interval_ms):
    per_cpu = {}
    total_ns = 0
    for cpu_id, current_delay in current["delayNs"].items():
        delta = _counter_delta(previous["delayNs"].get(cpu_id), current_delay)
        if delta is None:
            continue
        per_cpu[cpu_id] = delta / NANOSECONDS_PER_MILLISECOND
        total_ns += delta
    expected = current["vcpuCount"]
    if not per_cpu or len(per_cpu) != expected or interval_ms <= 0:
        return None
    ready_ms = total_ns / NANOSECONDS_PER_MILLISECOND
    return {
        "readyTimeMs": ready_ms,
        "readinessPercent": ready_ms / (interval_ms * len(per_cpu)) * 100.0,
        "intervalMs": interval_ms,
        "perCpu": per_cpu,
    }


def _calculate_disk(previous, current, time_scale):
    result = {}
    for name, now in current.items():
        before = previous.get(name)
        if before is None:
            continue
        rd_reqs = _counter_delta(before["readRequests"], now["readRequests"])
        rd_time = _counter_delta(before["readTime"], now["readTime"])
        wr_reqs = _counter_delta(before["writeRequests"], now["writeRequests"])
        wr_time = _counter_delta(before["writeTime"], now["writeTime"])
        result[name] = {
            "readLatencyMs": rd_time / float(rd_reqs) / time_scale
            if rd_reqs and rd_time is not None else None,
            "writeLatencyMs": wr_time / float(wr_reqs) / time_scale
            if wr_reqs and wr_time is not None else None,
        }
    return result


def _calculate_network(previous, current):
    result = {}
    for name, now in current.items():
        before = previous.get(name)
        if before is None:
            continue
        result[name] = {
            "rxDrop": _counter_delta(before["rxDrop"], now["rxDrop"]),
            "txDrop": _counter_delta(before["txDrop"], now["txDrop"]),
        }
    return result


def calculate_vm_metrics(previous, current):
    interval_ms = (current["capturedAt"] - previous["capturedAt"]) * 1000.0
    result = {}
    for vm_uuid, now in current["vms"].items():
        before = previous["vms"].get(vm_uuid)
        if before is None:
            continue
        vm_disks_before = dict((name, {
            "readRequests": value["readRequests"],
            "readTime": value["readTimeNs"],
            "writeRequests": value["writeRequests"],
            "writeTime": value["writeTimeNs"],
        }) for name, value in before["disks"].items())
        vm_disks_now = dict((name, {
            "readRequests": value["readRequests"],
            "readTime": value["readTimeNs"],
            "writeRequests": value["writeRequests"],
            "writeTime": value["writeTimeNs"],
        }) for name, value in now["disks"].items())
        result[vm_uuid] = {
            "cpu": _calculate_cpu(before, now, interval_ms),
            "disks": _calculate_disk(vm_disks_before, vm_disks_now,
                                     NANOSECONDS_PER_MILLISECOND),
            "interfaces": _calculate_network(before["interfaces"],
                                             now["interfaces"]),
        }
    return result


def calculate_host_metrics(previous, current):
    interval_ms = (current["capturedAt"] - previous["capturedAt"]) * 1000.0
    cpu_before = {"vcpuCount": len(previous["cpu"]["runDelayNs"]),
                  "delayNs": previous["cpu"]["runDelayNs"]}
    cpu_now = {"vcpuCount": len(current["cpu"]["runDelayNs"]),
               "delayNs": current["cpu"]["runDelayNs"]}
    cpu = None
    if current["cpu"]["version"] is not None and current["cpu"]["version"] >= 15:
        cpu = _calculate_cpu(cpu_before, cpu_now, interval_ms)
    disk_before = dict((name, {
        "readRequests": value["readRequests"], "readTime": value["readTimeMs"],
        "writeRequests": value["writeRequests"], "writeTime": value["writeTimeMs"],
    }) for name, value in previous["disks"].items())
    disk_now = dict((name, {
        "readRequests": value["readRequests"], "readTime": value["readTimeMs"],
        "writeRequests": value["writeRequests"], "writeTime": value["writeTimeMs"],
    }) for name, value in current["disks"].items())
    return {
        "cpu": cpu,
        "disks": _calculate_disk(disk_before, disk_now, 1.0),
        "interfaces": _calculate_network(previous["interfaces"],
                                         current["interfaces"]),
    }


def _metric_families():
    specs = (
        ("zstack_vm_cpu_ready_time_milliseconds", ["vm_uuid"]),
        ("zstack_vm_cpu_readiness_percent", ["vm_uuid"]),
        ("zstack_vm_vcpu_ready_time_milliseconds", ["vm_uuid", "vcpu_id"]),
        ("zstack_vm_vcpu_readiness_percent", ["vm_uuid", "vcpu_id"]),
        ("zstack_vm_disk_read_latency_milliseconds", ["vm_uuid", "disk_name"]),
        ("zstack_vm_disk_write_latency_milliseconds", ["vm_uuid", "disk_name"]),
        ("zstack_vm_network_receive_dropped_packets", ["vm_uuid", "nic_name"]),
        ("zstack_vm_network_transmit_dropped_packets", ["vm_uuid", "nic_name"]),
        ("zstack_host_cpu_ready_time_milliseconds", []),
        ("zstack_host_cpu_readiness_percent", []),
        ("zstack_host_cpu_ready_time_milliseconds_by_cpu", ["cpu_id"]),
        ("zstack_host_disk_read_latency_milliseconds", ["disk_name"]),
        ("zstack_host_disk_write_latency_milliseconds", ["disk_name"]),
        ("zstack_host_network_receive_dropped_packets", ["nic_name"]),
        ("zstack_host_network_transmit_dropped_packets", ["nic_name"]),
    )
    return dict((name, GaugeMetricFamily(name, name.replace("_", " "),
                                          None, labels))
                for name, labels in specs)


def build_prometheus_metrics(vm_metrics, host_metrics):
    metrics = _metric_families()
    for vm_uuid, values in vm_metrics.items():
        cpu = values["cpu"]
        if cpu is not None:
            metrics["zstack_vm_cpu_ready_time_milliseconds"].add_metric(
                [vm_uuid], cpu["readyTimeMs"])
            metrics["zstack_vm_cpu_readiness_percent"].add_metric(
                [vm_uuid], cpu["readinessPercent"])
            interval_ms = cpu["intervalMs"]
            for vcpu_id, ready_ms in cpu["perCpu"].items():
                labels = [vm_uuid, str(vcpu_id)]
                metrics["zstack_vm_vcpu_ready_time_milliseconds"].add_metric(
                    labels, ready_ms)
                if interval_ms:
                    metrics["zstack_vm_vcpu_readiness_percent"].add_metric(
                        labels, ready_ms / interval_ms * 100.0)
        for name, disk in values["disks"].items():
            labels = [vm_uuid, name]
            if disk["readLatencyMs"] is not None:
                metrics["zstack_vm_disk_read_latency_milliseconds"].add_metric(
                    labels, disk["readLatencyMs"])
            if disk["writeLatencyMs"] is not None:
                metrics["zstack_vm_disk_write_latency_milliseconds"].add_metric(
                    labels, disk["writeLatencyMs"])
        for name, nic in values["interfaces"].items():
            labels = [vm_uuid, name]
            if nic["rxDrop"] is not None:
                metrics["zstack_vm_network_receive_dropped_packets"].add_metric(
                    labels, nic["rxDrop"])
            if nic["txDrop"] is not None:
                metrics["zstack_vm_network_transmit_dropped_packets"].add_metric(
                    labels, nic["txDrop"])

    cpu = host_metrics["cpu"]
    if cpu is not None:
        metrics["zstack_host_cpu_ready_time_milliseconds"].add_metric(
            [], cpu["readyTimeMs"])
        metrics["zstack_host_cpu_readiness_percent"].add_metric(
            [], cpu["readinessPercent"])
        for cpu_id, ready_ms in cpu["perCpu"].items():
            metrics["zstack_host_cpu_ready_time_milliseconds_by_cpu"].add_metric(
                [str(cpu_id)], ready_ms)
    for name, disk in host_metrics["disks"].items():
        if disk["readLatencyMs"] is not None:
            metrics["zstack_host_disk_read_latency_milliseconds"].add_metric(
                [name], disk["readLatencyMs"])
        if disk["writeLatencyMs"] is not None:
            metrics["zstack_host_disk_write_latency_milliseconds"].add_metric(
                [name], disk["writeLatencyMs"])
    for name, nic in host_metrics["interfaces"].items():
        if nic["rxDrop"] is not None:
            metrics["zstack_host_network_receive_dropped_packets"].add_metric(
                [name], nic["rxDrop"])
        if nic["txDrop"] is not None:
            metrics["zstack_host_network_transmit_dropped_packets"].add_metric(
                [name], nic["txDrop"])
    return list(metrics.values())


class VmHostPerformanceCollector(object):
    def __init__(self, connection_factory=None, clock=None):
        self.connection_factory = connection_factory or self._open_libvirt
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.previous_vm = None
        self.previous_host = None

    @staticmethod
    def _open_libvirt():
        if libvirt is None:
            raise RuntimeError("libvirt-python is unavailable")
        conn = libvirt.openReadOnly("qemu:///system")
        if conn is None:
            raise RuntimeError("failed to open qemu:///system")
        return conn

    def collect(self):
        with self.lock:
            conn = self.connection_factory()
            try:
                current_vm = collect_vm_snapshot(conn)
            finally:
                conn.close()
            current_host = collect_host_snapshot()
            if self.previous_vm is None or self.previous_host is None:
                self.previous_vm = current_vm
                self.previous_host = current_host
                return list(_metric_families().values())
            vm_metrics = calculate_vm_metrics(self.previous_vm, current_vm)
            host_metrics = calculate_host_metrics(self.previous_host, current_host)
            self.previous_vm = current_vm
            self.previous_host = current_host
            return build_prometheus_metrics(vm_metrics, host_metrics)
