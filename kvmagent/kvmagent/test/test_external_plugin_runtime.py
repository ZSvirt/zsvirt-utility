# -*- coding: utf-8 -*-
from __future__ import absolute_import

import time
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

from kvmagent import external_plugin_runtime as runtime


class _Collector(object):
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def collect(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ExternalPluginRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.versions = {
            "python": "3.11.9",
            "kvmAgent": "5.1.0",
            "zstacklib": "5.1.0",
            "qemu": "8.2.0",
            "libvirt": "9.0.0",
            "os": "centos7",
            "architectures": "x86_64",
        }
        self.ranges = dict((name, ">=1.0,<99.0") for name in (
            "python", "kvmAgent", "zstacklib", "qemu", "libvirt"))
        self.ranges.update({
            "os": ["centos7", "kylin10", "helix8.4r"],
            "architectures": ["x86_64", "aarch64"],
        })

    def test_all_runtime_ranges_are_hard_gates(self):
        runtime.validate_compatibility(self.ranges, self.versions)
        for dependency in ("python", "kvmAgent", "zstacklib", "qemu",
                           "libvirt"):
            ranges = dict(self.ranges)
            ranges[dependency] = ">=99.0"
            with self.assertRaises(runtime.CompatibilityError) as raised:
                runtime.validate_compatibility(ranges, self.versions)
            self.assertEqual("PLUGIN_RUNTIME_INCOMPATIBLE", raised.exception.code)
            self.assertEqual(dependency, raised.exception.dependency)

    def test_invalid_range_is_distinct_from_incompatible_runtime(self):
        ranges = dict(self.ranges)
        ranges["qemu"] = "latest"
        with self.assertRaises(runtime.CompatibilityError) as raised:
            runtime.validate_compatibility(ranges, self.versions)
        self.assertEqual("PLUGIN_COMPATIBILITY_INVALID", raised.exception.code)

    def test_dependency_startup_is_retried_without_transient_failed_state(self):
        collector = _Collector([
            runtime.RuntimeQueryError("socket absent", transient=True,
                                      reason="DEPENDENCY_NOT_READY"),
            self.versions,
        ])
        clock = [0.0]
        waits = []

        def sleep(seconds):
            clock[0] += seconds

        versions, retries = runtime.collect_with_startup_retry(
            collector, deadline_seconds=30, sleep=sleep,
            monotonic=lambda: clock[0],
            on_wait=lambda count, unused_deadline, unused_detail: waits.append(count))
        self.assertEqual(self.versions, versions)
        self.assertEqual(1, retries)
        self.assertEqual([1], waits)

    def test_dependency_startup_timeout_is_not_reported_as_incompatible(self):
        class NeverReady(object):
            def collect(self):
                raise runtime.RuntimeQueryError(
                    "socket absent", transient=True, reason="DEPENDENCY_NOT_READY")

        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with self.assertRaises(runtime.CompatibilityError) as raised:
            runtime.collect_with_startup_retry(
                NeverReady(), deadline_seconds=1, sleep=sleep,
                monotonic=lambda: clock[0])
        self.assertEqual("PLUGIN_RUNTIME_VERSION_UNAVAILABLE", raised.exception.code)
        self.assertEqual("DEPENDENCY_STARTUP_TIMEOUT", raised.exception.reason)

    def test_virsh_parser_uses_daemon_and_running_hypervisor_versions(self):
        output = (b"Compiled against library: libvirt 9.0.0\n"
                  b"Running against daemon: 9.1.0\n"
                  b"Running hypervisor: QEMU 8.2.1\n")
        libvirt, qemu = runtime._virsh_versions(lambda unused, stderr=None: output)
        self.assertEqual("9.1.0", libvirt)
        self.assertEqual("8.2.1", qemu)

    def test_equivalent_short_numeric_versions_compare_equal(self):
        self.assertTrue(runtime.version_in_range("5.1", "==5.1.0"))
        self.assertTrue(runtime.version_in_range("5.1.0", ">=5.1,<5.2"))

    def test_release_candidate_sorts_before_final_release(self):
        self.assertTrue(runtime.version_in_range("3.13.0rc1", "<3.13.0"))
        self.assertFalse(runtime.version_in_range("3.13.0rc1", ">=3.13.0"))

    def test_equivalent_prerelease_separators_compare_equal(self):
        self.assertTrue(runtime.version_in_range(
            "3.13.0rc.1", "==3.13.0rc1"))

    def test_collector_canonicalizes_actual_manifest_os_and_cpu_tokens(self):
        virsh = (b"Using library: libvirt 9.0.0\n"
                 b"Running against daemon: 9.1.0\n"
                 b"Running hypervisor: QEMU 8.2.1\n")
        os_cases = (
            ('ID="centos"\nVERSION_ID="7.9.2009"\n', "centos7"),
            ('ID=kylin\nVERSION_ID="V10 (Tercel)"\n', "kylin10"),
            ('ID=helix\nVERSION_ID="8.4R"\n', "helix8.4r"),
        )
        for os_release, expected in os_cases:
            collector = runtime.RuntimeVersionCollector(
                command_runner=lambda unused, stderr=None: virsh,
                distribution_version=lambda unused: "5.1.0",
                os_release_reader=lambda value=os_release: value,
                machine="amd64")
            versions = collector.collect()
            self.assertEqual(expected, versions["os"])
            self.assertEqual("x86_64", versions["architectures"])

    def test_next_start_collection_includes_host_os_and_normalized_arm(self):
        def command_runner(command, stderr=None):
            if command[0] == runtime.sys.executable:
                return (b'{"python":"3.11.9","kvmAgent":"5.1.0",'
                        b'"zstacklib":"5.1.0"}\n')
            if command[0] == "virsh":
                return (b"Using library: libvirt 9.0.0\n"
                        b"Running against daemon: 9.1.0\n"
                        b"Running hypervisor: QEMU 8.2.1\n")
            if command[0] == "qemu-test":
                return b"QEMU emulator version 8.2.0\n"
            raise AssertionError("unexpected command: %r" % (command,))

        original_find = runtime._find_executable
        runtime._find_executable = lambda unused: "qemu-test"
        try:
            collector = runtime.RuntimeVersionCollector(
                command_runner=command_runner,
                distribution_version=lambda unused: "5.1.0",
                os_release_reader=lambda: "ID=centos\nVERSION_ID=7\n",
                machine="arm64")
            versions = collector.collect_next_start()
        finally:
            runtime._find_executable = original_find

        self.assertEqual("centos7", versions["os"])
        self.assertEqual("aarch64", versions["architectures"])

    def test_collector_rejects_unavailable_or_malformed_host_facts(self):
        cases = (
            (lambda: "ID=centos\n", "x86_64"),
            (lambda: "ID=bad id\nVERSION_ID=7\n", "x86_64"),
            (lambda: "ID=centos\nVERSION_ID=7\n", ""),
            (lambda: "ID=centos\nVERSION_ID=7\n", "x86 64"),
        )
        for reader, machine in cases:
            collector = runtime.RuntimeVersionCollector(
                command_runner=lambda unused, stderr=None: b"",
                distribution_version=lambda unused: "5.1.0",
                os_release_reader=reader,
                machine=machine)
            with self.assertRaises(runtime.RuntimeQueryError):
                collector.collect()

    def test_default_subprocess_is_killed_and_reaped_at_probe_deadline(self):
        started = time.time()
        with self.assertRaises(runtime.RuntimeQueryError) as raised:
            runtime._command_output(
                [runtime.sys.executable, "-c",
                 "import time; time.sleep(0.4); print('late')"],
                timeout_seconds=0.05)
        elapsed = time.time() - started

        self.assertEqual("DEPENDENCY_NOT_READY", raised.exception.reason)
        self.assertTrue(raised.exception.transient)
        self.assertLess(elapsed, 0.25)

    def test_subprocess_deadlines_use_monotonic_clock(self):
        class HungProcess(object):
            def __init__(self):
                self.killed = False
                self.waited = False

            def poll(self):
                return -9 if self.killed else None

            def terminate(self):
                pass

            def kill(self):
                self.killed = True

            def wait(self):
                self.waited = True
                return -9

        process = HungProcess()
        monotonic_values = (0.0, 1.0, 1.0, 1.2)
        with mock.patch.object(runtime.subprocess, "Popen",
                               return_value=process), \
             mock.patch.object(runtime, "monotonic_time",
                               side_effect=monotonic_values,
                               create=True), \
             mock.patch.object(runtime.time, "time",
                               side_effect=AssertionError(
                                   "deadlines must not use wall clock")), \
             mock.patch.object(runtime.time, "sleep"):
            with self.assertRaises(runtime.RuntimeQueryError):
                runtime._popen_output(["blocked-command"],
                                      timeout_seconds=0.1)

        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    def test_collect_deadline_uses_project_monotonic_clock_by_default(self):
        observed_timeouts = []
        versions = dict(self.versions)

        class Collector(object):
            def collect(self, timeout_seconds=None):
                observed_timeouts.append(timeout_seconds)
                return versions

        with mock.patch.object(runtime, "monotonic_time",
                               return_value=10.0, create=True), \
             mock.patch.object(runtime.time, "monotonic",
                               side_effect=AssertionError(
                                   "project monotonic clock was bypassed"),
                               create=True):
            result = runtime.collect_with_deadline(
                Collector(), "collect", deadline=11.0)

        self.assertEqual(versions, result)
        self.assertEqual([1.0], observed_timeouts)

    def test_startup_retry_uses_project_monotonic_clock_by_default(self):
        with mock.patch.object(runtime, "monotonic_time",
                               side_effect=(10.0, 10.0), create=True), \
             mock.patch.object(runtime.time, "monotonic",
                               side_effect=AssertionError(
                                   "project monotonic clock was bypassed"),
                               create=True):
            versions, retries = runtime.collect_with_startup_retry(
                _Collector([self.versions]), deadline_seconds=1,
                sleep=lambda unused: None)

        self.assertEqual(self.versions, versions)
        self.assertEqual(0, retries)

    def test_blocking_injected_collector_is_bounded_by_overall_deadline(self):
        observed_timeouts = []
        versions = dict(self.versions)

        class BlockingCollector(object):
            def collect(self, timeout_seconds=None):
                observed_timeouts.append(timeout_seconds)
                time.sleep(0.4)
                return dict(versions)

        started = time.time()
        with self.assertRaises(runtime.CompatibilityError) as raised:
            runtime.collect_with_startup_retry(
                BlockingCollector(), deadline_seconds=0.05)
        elapsed = time.time() - started

        self.assertEqual("PLUGIN_RUNTIME_VERSION_UNAVAILABLE",
                         raised.exception.code)
        self.assertEqual("DEPENDENCY_STARTUP_TIMEOUT",
                         raised.exception.reason)
        self.assertEqual(1, len(observed_timeouts))
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 0.05)
        self.assertLess(elapsed, 0.25)

    def test_blocking_injected_command_runner_cannot_escape_probe_deadline(self):
        def blocking_runner(unused_command, stderr=None):
            time.sleep(0.4)
            return b"late"

        started = time.time()
        with self.assertRaises(runtime.RuntimeQueryError):
            runtime._command_output(
                ["blocked-command"], command_runner=blocking_runner,
                timeout_seconds=0.05)
        self.assertLess(time.time() - started, 0.25)

    def test_next_process_probe_preserves_runtime_query_error(self):
        expected = runtime.RuntimeQueryError(
            "package probe timed out", transient=True,
            reason="DEPENDENCY_NOT_READY")

        def command_runner(unused_command, stderr=None):
            raise expected

        with self.assertRaises(runtime.RuntimeQueryError) as raised:
            runtime._next_process_versions(command_runner=command_runner)

        self.assertIs(expected, raised.exception)
        self.assertEqual("DEPENDENCY_NOT_READY", raised.exception.reason)
        self.assertTrue(raised.exception.transient)

    def test_next_process_probe_rejects_non_object_json(self):
        for payload in (b"[]", b"null", b'"versions"'):
            def command_runner(unused_command, stderr=None,
                               response=payload):
                return response

            with self.assertRaises(runtime.RuntimeQueryError) as raised:
                runtime._next_process_versions(command_runner=command_runner)

            self.assertEqual("RUNTIME_QUERY_INVALID", raised.exception.reason)
            self.assertFalse(raised.exception.transient)

    def test_next_virsh_probe_preserves_runtime_query_error(self):
        expected = runtime.RuntimeQueryError(
            "virsh probe timed out", transient=True,
            reason="DEPENDENCY_NOT_READY")

        def command_runner(unused_command, stderr=None):
            raise expected

        with self.assertRaises(runtime.RuntimeQueryError) as raised:
            runtime._next_hypervisor_versions(command_runner=command_runner)

        self.assertIs(expected, raised.exception)
        self.assertEqual("DEPENDENCY_NOT_READY", raised.exception.reason)
        self.assertTrue(raised.exception.transient)

    def test_next_qemu_probe_preserves_runtime_query_error(self):
        expected = runtime.RuntimeQueryError(
            "qemu probe timed out", transient=True,
            reason="DEPENDENCY_NOT_READY")

        def command_runner(command, stderr=None):
            if command[0] == "virsh":
                return b"Using library: libvirt 9.0.0\n"
            raise expected

        original_find = runtime._find_executable
        runtime._find_executable = lambda unused: "qemu-test"
        try:
            with self.assertRaises(runtime.RuntimeQueryError) as raised:
                runtime._next_hypervisor_versions(
                    command_runner=command_runner)
        finally:
            runtime._find_executable = original_find

        self.assertIs(expected, raised.exception)
        self.assertEqual("DEPENDENCY_NOT_READY", raised.exception.reason)
        self.assertTrue(raised.exception.transient)

    def test_permanent_error_after_block_is_still_bounded_by_deadline(self):
        calls = []

        class PermanentlyBlockedCollector(object):
            def collect(self, timeout_seconds=None):
                calls.append(timeout_seconds)
                time.sleep(0.4)
                raise runtime.RuntimeQueryError(
                    "permission denied", reason="RUNTIME_QUERY_DENIED",
                    transient=False)

        started = time.time()
        with self.assertRaises(runtime.CompatibilityError) as raised:
            runtime.collect_with_startup_retry(
                PermanentlyBlockedCollector(), deadline_seconds=0.05)

        self.assertEqual("DEPENDENCY_STARTUP_TIMEOUT",
                         raised.exception.reason)
        self.assertEqual(1, len(calls))
        self.assertLess(time.time() - started, 0.25)


if __name__ == "__main__":
    unittest.main()
