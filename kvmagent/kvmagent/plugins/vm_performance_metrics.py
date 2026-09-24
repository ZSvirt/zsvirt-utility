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
CPU_READY_SUPPORTED = "SUPPORTED"
CPU_READY_PARTIAL = "PARTIAL"
CPU_READY_NO_DATA = "NO_DATA"
CPU_READY_UNSUPPORTED = "UNSUPPORTED"

# The standard cpu<N> prefix contains nine counters.  Vendor kernels may
# append private counters, so run_delay must be addressed from the beginning
# of the record rather than from the end.
SCHEDSTAT_RUN_DELAY_INDEX = {
    15: 8,
    16: 8,
    17: 8,
}


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
    cpu_records = []
    try:
        with open(path) as stream:
            for line in stream:
                fields = line.split()
                if not fields:
                    continue
                if fields[0] == "version" and len(fields) > 1:
                    version = int(fields[1])
                elif re.match(r"^cpu\d+$", fields[0]):
                    cpu_records.append(fields)
    except (IOError, OSError, ValueError):
        return {"version": version, "runDelayNs": {}, "valid": False,
                "status": CPU_READY_NO_DATA}

    run_delay_index = SCHEDSTAT_RUN_DELAY_INDEX.get(version)
    if run_delay_index is None:
        return {"version": version, "runDelayNs": {}, "valid": False,
                "status": CPU_READY_UNSUPPORTED}
    if not cpu_records:
        return {"version": version, "runDelayNs": {}, "valid": False,
                "status": CPU_READY_NO_DATA}

    invalid_records = 0
    for fields in cpu_records:
        # cpu name + the nine standard counters, including timeslices after
        # run_delay.  Trailing vendor fields are intentionally accepted.
        if len(fields) < 10:
            invalid_records += 1
            continue
        try:
            run_delay = int(fields[run_delay_index])
            timeslices = int(fields[9])
            if run_delay < 0 or timeslices < 0:
                raise ValueError("negative schedstat counter")
            delays[fields[0]] = run_delay
        except (TypeError, ValueError):
            invalid_records += 1

    if invalid_records:
        status = CPU_READY_PARTIAL if delays else CPU_READY_NO_DATA
        return {"version": version, "runDelayNs": delays, "valid": False,
                "status": status}
    return {"version": version, "runDelayNs": delays, "valid": bool(delays),
            "status": CPU_READY_SUPPORTED if delays else CPU_READY_NO_DATA}


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
        return None
    return _select_top_level_disks(disks, sys_block)


def _block_relations(name, relation, sys_block):
    path = os.path.join(sys_block, name, relation)
    try:
        return set(os.listdir(path))
    except (IOError, OSError):
        return set()


def _whole_disk_for_partition(name, sys_block):
    if not _is_partition(name, sys_block):
        return None
    real_path = os.path.realpath(os.path.join(sys_block, name))
    parent = os.path.basename(os.path.dirname(real_path))
    return parent if parent and parent != name else None


def _select_top_level_disks(disks, sys_block="/sys/class/block"):
    """Keep top-level dm devices and raw disks outside their backing graphs."""
    dm_names = set(name for name in disks if name.startswith("dm-"))
    top_dm = set(name for name in dm_names
                 if not (_block_relations(name, "holders", sys_block) & dm_names))
    backing = set()

    def add_slaves(name):
        for slave in _block_relations(name, "slaves", sys_block):
            if slave in backing:
                continue
            backing.add(slave)
            parent = _whole_disk_for_partition(slave, sys_block)
            if parent:
                backing.add(parent)
            add_slaves(slave)

    for name in top_dm:
        add_slaves(name)
    selected = top_dm | (set(disks) - dm_names - backing)
    return dict((name, disks[name]) for name in selected)


def _host_nic_names(sys_net="/sys/class/net"):
    try:
        names = os.listdir(sys_net)
    except (IOError, OSError):
        return None
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
    names = _host_nic_names(sys_net)
    if names is None:
        return None
    for name in names:
        stats = os.path.join(sys_net, name, "statistics")
        rx_drop = _read_int(os.path.join(stats, "rx_dropped"))
        tx_drop = _read_int(os.path.join(stats, "tx_dropped"))
        if rx_drop is None or tx_drop is None:
            return None
        interfaces[name] = {"rxDrop": rx_drop, "txDrop": tx_drop}
    return interfaces


def collect_host_snapshot():
    cpu = _collect_host_cpu()
    disks = _collect_host_disks()
    interfaces = _collect_host_network()
    return {
        "hostname": socket.gethostname(),
        "capturedAt": time.time(),
        "cpu": cpu,
        "disks": disks or {},
        "interfaces": interfaces or {},
        "valid": {"cpu": cpu["valid"], "disks": disks is not None,
                  "interfaces": interfaces is not None},
    }


def _schedstats_wait_supported(
        path="/proc/sys/kernel/sched_schedstats"):
    """Return whether libvirt's legacy vcpu.N.wait source is usable.

    A number of older libvirt builds return a successful zero when scheduler
    wait accounting is disabled.  Treating that value as a counter creates a
    false zero CPU Ready series, so the fallback is enabled only after an
    explicit runtime capability check.
    """
    return _read_int(path) == 1


def _collect_vcpus(values, wait_supported):
    count = int(values.get("vcpu.current", 0))
    delay_values = {}
    wait_values = {}
    # vcpu.current is the number of online vCPUs, not an upper bound for
    # their IDs. CPU hotplug can therefore produce sparse IDs (for example
    # 0 and 2). Discover the IDs from libvirt's returned keys instead of
    # assuming that online IDs are in range(vcpu.current).
    for key, value in values.items():
        match = re.match(r"^vcpu\.(\d+)\.(delay|wait)$", key)
        if match is None:
            continue
        vcpu_id = int(match.group(1))
        field = match.group(2)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed < 0:
            continue
        if field == "delay":
            delay_values[vcpu_id] = parsed
        else:
            wait_values[vcpu_id] = parsed

    delays = {}
    for vcpu_id in set(delay_values) | set(wait_values):
        if vcpu_id in delay_values:
            delays[vcpu_id] = delay_values[vcpu_id]
        elif wait_supported:
            delays[vcpu_id] = wait_values[vcpu_id]

    if count <= 0:
        status = CPU_READY_NO_DATA
    elif len(delays) == count:
        status = CPU_READY_SUPPORTED
    elif delays:
        status = CPU_READY_PARTIAL
    else:
        status = CPU_READY_UNSUPPORTED
    return count, delays, status


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


def collect_vm_snapshot(conn, wait_supported=None):
    """Collect all active VMs with one libvirt getAllDomainStats call."""
    stats = (_libvirt_flag("VIR_DOMAIN_STATS_VCPU") |
             _libvirt_flag("VIR_DOMAIN_STATS_BLOCK") |
             _libvirt_flag("VIR_DOMAIN_STATS_INTERFACE"))
    flags = _libvirt_flag("VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE")
    records = conn.getAllDomainStats(stats, flags)
    if wait_supported is None:
        wait_supported = _schedstats_wait_supported()
    snapshot = {}
    for domain, values in records:
        count, delays, cpu_status = _collect_vcpus(values, wait_supported)
        # libvirt uses RFC-4122 UUIDs; ZStack resource/API labels use 32 hex digits.
        snapshot[domain.UUIDString().replace("-", "").lower()] = {
            "vcpuCount": count,
            "delayNs": delays,
            "cpuStatus": cpu_status,
            "disks": _collect_vm_disks(values),
            "interfaces": _collect_vm_interfaces(values),
        }
    return {"capturedAt": time.time(), "vms": snapshot}


def _calculate_cpu(previous, current, interval_ms):
    per_cpu = {}
    for cpu_id, current_delay in current["delayNs"].items():
        delta = _counter_delta(previous["delayNs"].get(cpu_id), current_delay)
        if delta is None:
            continue
        per_cpu[cpu_id] = delta / NANOSECONDS_PER_MILLISECOND
    expected = current["vcpuCount"]
    if not per_cpu or len(per_cpu) != expected or interval_ms <= 0:
        return None
    return {
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
            # Interval gauges, NOT cumulative counters. Export each pair only
            # when both deltas are valid so weighted aggregation stays aligned.
            "readRequests": rd_reqs if rd_time is not None else None,
            "readTimeMs": rd_time / time_scale
            if rd_time is not None and rd_reqs is not None else None,
            "writeRequests": wr_reqs if wr_time is not None else None,
            "writeTimeMs": wr_time / time_scale
            if wr_time is not None and wr_reqs is not None else None,
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
            "cpuStatus": now.get("cpuStatus", CPU_READY_NO_DATA),
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
    # Host keeps its existing independent aggregate contract. VM aggregates are
    # intentionally calculated only by ZWatch PromQL from per-vCPU series.
    if cpu is not None:
        cpu["readyTimeMs"] = sum(cpu["perCpu"].values())
        cpu["readinessPercent"] = (
            cpu["readyTimeMs"] / (interval_ms * len(cpu["perCpu"])) * 100.0)
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
        "cpuStatus": current["cpu"].get("status", CPU_READY_NO_DATA),
        "disks": _calculate_disk(disk_before, disk_now, 1.0),
        "interfaces": _calculate_network(previous["interfaces"],
                                         current["interfaces"]),
    }


def _metric_families():
    specs = (
        ("zstack_vm_vcpu_ready_time_milliseconds", ["vm_uuid", "vcpu_id"]),
        ("zstack_vm_vcpu_readiness_percent", ["vm_uuid", "vcpu_id"]),
        ("zstack_vm_cpu_ready_status", ["vm_uuid", "status"]),
        ("zstack_vm_disk_read_time_milliseconds", ["vm_uuid", "disk_name"]),
        ("zstack_vm_disk_read_requests", ["vm_uuid", "disk_name"]),
        ("zstack_vm_disk_write_time_milliseconds", ["vm_uuid", "disk_name"]),
        ("zstack_vm_disk_write_requests", ["vm_uuid", "disk_name"]),
        ("zstack_vm_network_receive_dropped_packets", ["vm_uuid", "nic_name"]),
        ("zstack_vm_network_transmit_dropped_packets", ["vm_uuid", "nic_name"]),
        ("zstack_host_cpu_ready_time_milliseconds", []),
        ("zstack_host_cpu_readiness_percent", []),
        ("zstack_host_cpu_ready_time_milliseconds_by_cpu", ["cpu_id"]),
        ("zstack_host_cpu_ready_status", ["status"]),
        ("zstack_host_disk_read_latency_milliseconds", ["disk_name"]),
        ("zstack_host_disk_write_latency_milliseconds", ["disk_name"]),
        ("zstack_host_network_receive_dropped_packets", ["nic_name"]),
        ("zstack_host_network_transmit_dropped_packets", ["nic_name"]),
    )
    return dict((name, GaugeMetricFamily(name, name.replace("_", " ") +
                                          " (last valid scrape interval)",
                                          None, labels))
                for name, labels in specs)


def build_prometheus_metrics(vm_metrics, host_metrics):
    metrics = _metric_families()
    for vm_uuid, values in vm_metrics.items():
        cpu_status = values.get("cpuStatus", CPU_READY_NO_DATA)
        metrics["zstack_vm_cpu_ready_status"].add_metric(
            [vm_uuid, cpu_status], 1)
        cpu = values["cpu"]
        if cpu is not None:
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
            for direction in ("read", "write"):
                requests = disk[direction + "Requests"]
                duration_ms = disk[direction + "TimeMs"]
                if requests is not None and duration_ms is not None:
                    metrics["zstack_vm_disk_%s_requests" % direction].add_metric(
                        labels, requests)
                    metrics["zstack_vm_disk_%s_time_milliseconds" % direction].add_metric(
                        labels, duration_ms)
        for name, nic in values["interfaces"].items():
            labels = [vm_uuid, name]
            if nic["rxDrop"] is not None:
                metrics["zstack_vm_network_receive_dropped_packets"].add_metric(
                    labels, nic["rxDrop"])
            if nic["txDrop"] is not None:
                metrics["zstack_vm_network_transmit_dropped_packets"].add_metric(
                    labels, nic["txDrop"])

    metrics["zstack_host_cpu_ready_status"].add_metric(
        [host_metrics.get("cpuStatus", CPU_READY_NO_DATA)], 1)
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
        self.last_host_metrics = {"cpu": None, "cpuStatus": CPU_READY_NO_DATA,
                                  "disks": {}, "interfaces": {}}

    @staticmethod
    def _component_valid(snapshot, component):
        return snapshot.get("valid", {}).get(component, True)

    def _update_host_metrics(self, current):
        components = ("cpu", "disks", "interfaces")
        self.last_host_metrics["cpuStatus"] = current.get(
            "cpu", {}).get("status", CPU_READY_NO_DATA)
        if self.previous_host is None:
            self.previous_host = {"componentCapturedAt": {}, "valid": {}}
        for component in components:
            if not self._component_valid(current, component):
                continue
            previous_time = self.previous_host["componentCapturedAt"].get(component)
            if previous_time is not None:
                before = {"capturedAt": previous_time,
                          "cpu": self.previous_host.get("cpu", {"version": None, "runDelayNs": {}}),
                          "disks": self.previous_host.get("disks", {}),
                          "interfaces": self.previous_host.get("interfaces", {})}
                now = {"capturedAt": current["capturedAt"],
                       "cpu": current.get("cpu", before["cpu"]),
                       "disks": current.get("disks", before["disks"]),
                       "interfaces": current.get("interfaces", before["interfaces"])}
                calculated = calculate_host_metrics(before, now)
                self.last_host_metrics[component] = calculated[component]
            self.previous_host[component] = current[component]
            self.previous_host["componentCapturedAt"][component] = current["capturedAt"]
            self.previous_host["valid"][component] = True
        return self.last_host_metrics

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
            if self.previous_vm is None:
                self.previous_vm = current_vm
                host_metrics = self._update_host_metrics(current_host)
                vm_metrics = dict((vm_uuid, {
                    "cpu": None,
                    "cpuStatus": values.get("cpuStatus", CPU_READY_NO_DATA),
                    "disks": {},
                    "interfaces": {},
                }) for vm_uuid, values in current_vm["vms"].items())
                return build_prometheus_metrics(vm_metrics, host_metrics)
            vm_metrics = calculate_vm_metrics(self.previous_vm, current_vm)
            host_metrics = self._update_host_metrics(current_host)
            self.previous_vm = current_vm
            return build_prometheus_metrics(vm_metrics, host_metrics)
