# -*- coding: utf-8 -*-
from __future__ import absolute_import

import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

from kvmagent import external_plugin_manifest
from kvmagent import external_plugin_registry
from zstacklib.utils import restart_fence
from zstacklib.utils.restart_fence import AgentRestartFence


VERSIONS = {
    "python": "3.11.9",
    "kvmAgent": "5.1.0",
    "zstacklib": "5.1.0",
    "qemu": "8.2.0",
    "libvirt": "9.0.0",
    "os": "centos7",
    "architectures": "x86_64",
}


COMPATIBILITY = dict((name, ">=1.0,<99.0") for name in (
    "python", "kvmAgent", "zstacklib", "qemu", "libvirt"))
COMPATIBILITY.update({
    "os": ["centos7", "kylin10", "helix8.4r"],
    "architectures": ["x86_64", "aarch64"],
})


class _Collector(object):
    def __init__(self, versions=None):
        self.versions = dict(versions or VERSIONS)
        self.collect_count = 0

    def collect(self):
        self.collect_count += 1
        return dict(self.versions)

    def collect_next_start(self):
        return dict(self.versions)


class _SplitCollector(_Collector):
    def __init__(self, live, next_start):
        super(_SplitCollector, self).__init__(live)
        self.next_start = dict(next_start)

    def collect_next_start(self):
        return dict(self.next_start)


class _UnavailableNextStartCollector(_Collector):
    def collect_next_start(self):
        raise external_plugin_registry.RuntimeQueryError(
            "next-start package query failed", reason="RUNTIME_QUERY_INVALID")


class _RecoveringCollector(_Collector):
    def __init__(self):
        super(_RecoveringCollector, self).__init__()
        self.available = False

    def collect(self):
        if not self.available:
            raise external_plugin_registry.RuntimeQueryError(
                "runtime query unavailable", reason="RUNTIME_QUERY_INVALID")
        return super(_RecoveringCollector, self).collect()


class _HttpServer(object):
    def __init__(self):
        self.sync = {}
        self.async_ = {}
        self.raw = {}

    def register_sync_uri(self, path, handler):
        self.sync[path] = handler

    def register_async_uri(self, path, handler):
        self.async_[path] = handler

    def register_raw_uri(self, path, handler):
        self.raw[path] = handler

    def register_uri_batch(self, routes):
        mappings = {"sync": self.sync, "async": self.async_}
        for kind, path, handler in routes:
            mappings[kind][path] = handler


def _write(path, content):
    with open(path, "w") as stream:
        stream.write(content)
    os.chmod(path, 0o644)


class ExternalPluginRegistryTest(unittest.TestCase):
    def setUp(self):
        AgentRestartFence.reset_for_test()
        self.root = tempfile.mkdtemp()
        self.registry_root = os.path.join(self.root, "registry")
        self.managed_root = os.path.join(self.root, "managed")
        os.makedirs(self.registry_root)
        os.makedirs(self.managed_root)
        self.http = _HttpServer()

    def tearDown(self):
        AgentRestartFence.reset_for_test()
        for name in list(sys.modules):
            if name == "sample_plugin" or name.startswith("sample_plugin."):
                sys.modules.pop(name, None)
            if name == "duplicate_plugin" or name.startswith("duplicate_plugin."):
                sys.modules.pop(name, None)
            if name.startswith("loader_"):
                sys.modules.pop(name, None)
        shutil.rmtree(self.root)

    def _release(self, plugin_id="sample-plugin", namespace="sample_plugin",
                 version="2.0.0", marker=None, plugin_api=1):
        release = os.path.join(self.managed_root, plugin_id + "-" + version)
        package = os.path.join(release, namespace)
        os.makedirs(package)
        _write(os.path.join(package, "__init__.py"), "")
        marker_statement = ""
        if marker:
            marker_statement = "open(%r, 'w').write('started')\n" % marker
        _write(os.path.join(package, "plugin.py"),
               "class Entry(object):\n"
               "    def configure(self, config):\n"
               "        self.context = config['externalPluginContext']\n"
               "    def start(self):\n"
               "        %s"
               "        self.context.register_sync_uri('/sample/status', lambda req: '{}', mutable=False)\n"
               "    def stop(self):\n"
               "        pass\n" % marker_statement)
        _write(os.path.join(release, "NOTICE"), "notice\n")
        digest = external_plugin_manifest._canonical_content_sha256(release)
        manifest = {
            "schemaVersion": "1",
            "identity": {
                "pluginId": plugin_id,
                "version": version,
                "contentSha256": digest,
                "sourceCommit": "0" * 40,
                "buildTime": "2026-08-20T00:00:00Z",
            },
            "loading": {
                "entryModule": namespace + ".plugin",
                "entryClass": "Entry",
                "pluginApi": plugin_api,
            },
            "compatibility": dict(COMPATIBILITY),
            "interfaces": {"exampleApiVersion": 1, "errorCodeVersion": 1},
            "capabilities": {},
            "security": {"dynamicDependencies": False},
        }
        _write(os.path.join(release, "manifest.yaml"), json.dumps(manifest))
        return release

    def _register(self, name, plugin_id, release, namespace, plugin_api=1):
        path = os.path.join(self.registry_root, name + ".ini")
        _write(path,
               "[external-plugin]\n"
               "id = %s\n"
               "release_root = %s\n"
               "entry_module = %s.plugin\n"
               "entry_class = Entry\n"
               "manifest = %s\n"
               "plugin_api = %s\n"
               "enabled = true\n" %
               (plugin_id, release, namespace,
                os.path.join(release, "manifest.yaml"), plugin_api))

    def _replace_plugin_source(self, release, namespace, source):
        _write(os.path.join(release, namespace, "plugin.py"), source)
        manifest_path = os.path.join(release, "manifest.yaml")
        with open(manifest_path, "r") as stream:
            manifest = json.load(stream)
        manifest["identity"]["contentSha256"] = (
            external_plugin_manifest._canonical_content_sha256(release))
        _write(manifest_path, json.dumps(manifest))

    def _mutate_manifest(self, release, mutate):
        manifest_path = os.path.join(release, "manifest.yaml")
        with open(manifest_path, "r") as stream:
            manifest = json.load(stream)
        mutate(manifest)
        _write(manifest_path, json.dumps(manifest))

    def _registry(self, **kwargs):
        return external_plugin_registry.ExternalPluginRegistry(
            self.http, registry_root=self.registry_root,
            managed_root=self.managed_root, expected_uid=None,
            runtime_collector=kwargs.get("runtime_collector", _Collector()),
            protected_namespaces=kwargs.get("protected_namespaces", set()),
            dependency_ready_deadline=kwargs.get(
                "dependency_ready_deadline", 30),
            sleep=kwargs.get("sleep"), monotonic=kwargs.get("monotonic"),
            stop_timeout_seconds=kwargs.get("stop_timeout_seconds", 1),
            utcnow=kwargs.get("utcnow"))

    def test_deployment_verification_markers_are_outside_content_digest(self):
        release = self._release()
        manifest = external_plugin_manifest.load_manifest(
            os.path.join(release, "manifest.yaml"))

        _write(os.path.join(release, ".artifact-sha256"), "1" * 64 + "\n")
        _write(os.path.join(release, ".content-sha256"),
               manifest.content_sha256 + "\n")
        hardened = []
        try:
            for root, unused_dirs, files in os.walk(release):
                for name in files:
                    if name in ("manifest.yaml", ".artifact-sha256",
                                ".content-sha256"):
                        continue
                    path = os.path.join(root, name)
                    mode = stat.S_IMODE(os.lstat(path).st_mode)
                    hardened.append((path, mode))
                    os.chmod(path, 0o555 if mode & 0o111 else 0o444)

            self.assertEqual(
                manifest.content_sha256,
                external_plugin_manifest._canonical_content_sha256(release))
            manifest.verify_content(release)
        finally:
            for path, mode in hardened:
                os.chmod(path, mode)

    def test_discovery_rejects_release_changed_during_content_verification(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        original_verify = (
            external_plugin_manifest.ExternalPluginManifest.verify_content)

        def verify_then_mutate(manifest, release_root):
            result = original_verify(manifest, release_root)
            _write(os.path.join(release_root, "sample_plugin", "plugin.py"),
                   "# changed while validation was in progress\n")
            return result

        external_plugin_manifest.ExternalPluginManifest.verify_content = (
            verify_then_mutate)
        try:
            record = self._registry().discover()[0]
        finally:
            external_plugin_manifest.ExternalPluginManifest.verify_content = (
                original_verify)

        self.assertEqual("FAILED", record.state)
        self.assertEqual("PRE_IMPORT_VALIDATION", record.failure["stage"])
        self.assertEqual("PLUGIN_MANIFEST_MISMATCH", record.failure["code"])

    def test_discovery_rejects_registry_replaced_during_manifest_load(self):
        first = self._release(
            "sample-plugin", "sample_plugin", "2.0.0")
        second = self._release(
            "sample-plugin", "sample_plugin_next", "3.0.0")
        self._register(
            "sample", "sample-plugin", first, "sample_plugin")
        original_load = external_plugin_registry.load_manifest

        def load_then_replace(path):
            manifest = original_load(path)
            self._register(
                "sample", "sample-plugin", second, "sample_plugin_next")
            return manifest

        external_plugin_registry.load_manifest = load_then_replace
        try:
            record = self._registry().discover()[0]
        finally:
            external_plugin_registry.load_manifest = original_load

        self.assertEqual("FAILED", record.state)
        self.assertEqual("PRE_IMPORT_VALIDATION", record.failure["stage"])
        self.assertEqual("PLUGIN_MANIFEST_MISMATCH", record.failure["code"])

    def test_discovery_rejects_release_pointer_changed_before_snapshot(self):
        first = self._release(
            "sample-plugin", "sample_plugin", "2.0.0")
        second = self._release(
            "sample-plugin", "sample_plugin_next", "3.0.0")
        pointer = os.path.join(self.managed_root, "sample-plugin-current")

        def point_to(target):
            if os.path.lexists(pointer):
                os.unlink(pointer)
            if os.name == "nt":
                os.symlink(target, pointer, target_is_directory=True)
            else:
                os.symlink(target, pointer)

        try:
            point_to(first)
        except (AttributeError, OSError) as error:
            self.skipTest("directory symlinks unavailable: %s" % error)
        self._register(
            "sample", "sample-plugin", pointer, "sample_plugin")
        registry = self._registry()
        original_fingerprint = registry._disk_metadata_fingerprint
        switched = [False]

        def fingerprint_after_switch(registry_path, release_root,
                                     resolved_release=None):
            if not switched[0]:
                switched[0] = True
                point_to(second)
            return original_fingerprint(
                registry_path, release_root, resolved_release)

        registry._disk_metadata_fingerprint = fingerprint_after_switch
        record = registry.discover()[0]

        self.assertEqual("FAILED", record.state)
        self.assertEqual("PRE_IMPORT_VALIDATION", record.failure["stage"])
        self.assertEqual("PLUGIN_MANIFEST_MISMATCH", record.failure["code"])

    def test_blocked_probe_is_bounded_and_does_not_stall_follow_on_plugin(self):
        release_wait = threading.Event()
        entered = threading.Event()

        class FirstProbeBlocks(object):
            def __init__(self):
                self.calls = 0

            def collect(self, timeout_seconds=None):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release_wait.wait(0.4)
                return dict(VERSIONS)

        first = self._release("first-plugin", "loader_first", "2.0.0")
        second = self._release("second-plugin", "loader_second", "2.0.0")
        self._register("first", "first-plugin", first, "loader_first")
        self._register("second", "second-plugin", second, "loader_second")
        registry = self._registry(
            runtime_collector=FirstProbeBlocks(),
            dependency_ready_deadline=0.05)
        registry.discover()

        started = time.time()
        try:
            registry.load_and_start()
            elapsed = time.time() - started
        finally:
            release_wait.set()

        status = dict((item["id"], item) for item in
                      registry.status_response(False)["plugins"])
        self.assertTrue(entered.is_set())
        self.assertLess(elapsed, 0.25)
        self.assertEqual("DEPENDENCY_STARTUP_TIMEOUT",
                         status["first-plugin"]["failure"]["reason"])
        self.assertEqual("STARTED", status["second-plugin"]["state"])

    def test_wait_deadline_is_fixed_across_dependency_retries(self):
        monotonic = [10.0]
        wall_clock = [datetime.datetime(2026, 8, 23, 0, 0, 0)]
        registry = self._registry(
            dependency_ready_deadline=30,
            monotonic=lambda: monotonic[0],
            utcnow=lambda: wall_clock[0])
        record = external_plugin_registry.ExternalPluginRecord("sample")

        registry._on_wait(record, 1, 40.0, "first")
        first_deadline = record.dependency["waitDeadline"]
        monotonic[0] = 20.0
        wall_clock[0] = datetime.datetime(2026, 8, 23, 0, 0, 10)
        registry._on_wait(record, 2, 40.0, "second")

        self.assertEqual("2026-08-23T00:00:30Z", first_deadline)
        self.assertEqual(first_deadline,
                         record.dependency["waitDeadline"])

    def test_loads_plugin_and_keeps_release_root_out_of_sys_path(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.register_status_endpoint()
        registry.load_and_start()
        status = registry.status_response(reconcile=False)["plugins"][0]
        self.assertEqual("STARTED", status["state"])
        self.assertEqual("COMPATIBLE", status["compatibilityState"])
        self.assertNotIn(release, sys.path)
        self.assertFalse(os.path.exists(os.path.join(release, "sample_plugin",
                                                     "__pycache__")))
        self.assertIn("/sample/status", status["registeredRoutes"])
        self.assertIn("/kvmagent/plugins/status", self.http.sync)
        self.assertIn("/kvmagent/operations/status", self.http.sync)
        self.assertIn("/kvmagent/operations/restart-fence", self.http.raw)

    def test_load_rejects_release_changed_after_discovery_before_import(self):
        marker = os.path.join(self.root, "marker")
        release = self._release(marker=marker)
        self._register("sample", "sample-plugin", release, "sample_plugin")
        source_path = os.path.join(release, "sample_plugin", "plugin.py")
        with open(source_path, "r") as stream:
            source = stream.read()

        class MutatingCollector(_Collector):
            def collect(self):
                _write(source_path, source + "\n# changed before import\n")
                return super(MutatingCollector, self).collect()

        registry = self._registry(runtime_collector=MutatingCollector())
        registry.discover()
        registry.load_and_start()
        status = registry.status_response(reconcile=False)["plugins"][0]

        self.assertEqual("FAILED", status["state"])
        self.assertEqual("PRE_IMPORT_VALIDATION", status["failure"]["stage"])
        self.assertEqual("PLUGIN_MANIFEST_MISMATCH",
                         status["failure"]["code"])
        self.assertFalse(os.path.exists(marker))

    def test_registry_scan_failure_does_not_abort_base_agent(self):
        registry = self._registry()
        original_listdir = external_plugin_registry.os.listdir

        def fail_to_scan(_path):
            raise OSError("registry permission denied")

        external_plugin_registry.os.listdir = fail_to_scan
        try:
            records = registry.discover()
        finally:
            external_plugin_registry.os.listdir = original_listdir

        self.assertEqual([], records)
        self.assertEqual([], registry.status_response(False)["plugins"])

    def test_namespace_validation_uses_stable_module_snapshot(self):
        class ConcurrentModules(dict):
            def __iter__(self):
                raise RuntimeError("dictionary changed size during iteration")

        class SysProxy(object):
            modules = ConcurrentModules({"existing.module": object()})

        registry = self._registry()
        original_sys = external_plugin_registry.sys
        external_plugin_registry.sys = SysProxy()
        try:
            registry._validate_namespaces()
        finally:
            external_plugin_registry.sys = original_sys

    def test_discovery_exception_leaves_explicit_failed_state(self):
        registry = self._registry()
        registry._discovery_completed = True

        def fail_discovery():
            raise RuntimeError("unexpected discovery failure")

        registry._discover_records = fail_discovery
        with self.assertRaises(RuntimeError):
            registry.discover()

        self.assertEqual("FAILED", registry.initialization_state)
        self.assertFalse(registry._discovery_completed)

    def test_empty_completed_discovery_is_not_repeated_during_load(self):
        registry = self._registry()
        scans = []
        original_listdir = external_plugin_registry.os.listdir

        def counted_listdir(path):
            if path == self.registry_root:
                scans.append(path)
            return original_listdir(path)

        external_plugin_registry.os.listdir = counted_listdir
        try:
            registry.discover()
            registry.load_and_start()
        finally:
            external_plugin_registry.os.listdir = original_listdir

        self.assertEqual(1, len(scans))
        self.assertEqual("READY", registry.initialization_state)

    def test_incompatible_runtime_prevents_any_plugin_code_execution(self):
        marker = os.path.join(self.root, "marker")
        release = self._release(marker=marker)
        self._register("sample", "sample-plugin", release, "sample_plugin")
        versions = dict(VERSIONS)
        versions["qemu"] = "100.0.0"
        registry = self._registry(runtime_collector=_Collector(versions))
        registry.discover()
        registry.load_and_start()
        status = registry.status_response(reconcile=False)["plugins"][0]
        self.assertEqual("FAILED", status["state"])
        self.assertEqual("PLUGIN_RUNTIME_INCOMPATIBLE", status["failure"]["code"])
        self.assertFalse(os.path.exists(marker))

    def test_namespace_conflict_fails_all_records_before_import(self):
        first = self._release("first-plugin", "duplicate_plugin", "2.0.0")
        second = self._release("second-plugin", "duplicate_plugin", "2.0.1")
        self._register("first", "first-plugin", first, "duplicate_plugin")
        self._register("second", "second-plugin", second, "duplicate_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        statuses = registry.status_response(reconcile=False)["plugins"]
        self.assertEqual(2, len(statuses))
        self.assertTrue(all(item["state"] == "FAILED" for item in statuses))
        self.assertTrue(all(item["failure"]["code"] == "PLUGIN_NAMESPACE_CONFLICT"
                            for item in statuses))

    def test_status_detects_next_start_drift_without_reloading_plugin(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        next_start = dict(VERSIONS)
        next_start["qemu"] = "100.0.0"
        collector = _SplitCollector(VERSIONS, next_start)
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.load_and_start()
        instance = registry.records[0].instance
        status = registry.status_response(reconcile=True)["plugins"][0]
        self.assertEqual("STARTED", status["state"])
        self.assertEqual("DRIFTED_NEXT_START", status["compatibilityState"])
        self.assertTrue(status["restartRequired"])
        self.assertIs(instance, registry.records[0].instance)

    def test_every_manifest_only_change_requires_restart_without_reimport(self):
        mutations = (
            ("version", lambda document: document["identity"].update(
                {"version": "2.0.1"})),
            ("compatibility", lambda document: document["compatibility"].update(
                {"python": ">=1.0,<98.0"})),
            ("interfaces", lambda document: document["interfaces"].update(
                {"exampleApiVersion": 2})),
            ("capabilities", lambda document: document["capabilities"].update(
                {"manifestOnlyFlag": True})),
            ("security", lambda document: document["security"].update(
                {"auditPolicy": "strict"})),
        )
        for index, (label, mutate) in enumerate(mutations):
            plugin_id = "manifest-%s" % label
            namespace = "loader_%s" % label
            release = self._release(plugin_id, namespace, "2.0.%s" % index)
            self._register(label, plugin_id, release, namespace)
            registry = self._registry()
            registry.discover()
            record = registry._by_id[plugin_id]
            registry.load_and_start()
            instance = record.instance
            loaded_digest = record.loaded_manifest_digest

            self._mutate_manifest(release, mutate)
            status = record.status()
            self.assertEqual("STARTED", status["state"])
            statuses = dict((item["id"], item) for item in
                            registry.status_response(reconcile=True)["plugins"])
            status = statuses[plugin_id]

            self.assertEqual("DRIFTED_NEXT_START",
                             status["compatibilityState"], label)
            self.assertTrue(status["restartRequired"], label)
            self.assertIs(instance, record.instance, label)
            self.assertEqual(loaded_digest, record.loaded_manifest_digest,
                             label)
            registry.stop()
            os.remove(os.path.join(self.registry_root, label + ".ini"))
            for name in list(sys.modules):
                if name == namespace or name.startswith(namespace + "."):
                    sys.modules.pop(name, None)

    def test_status_does_not_reconcile_a_transitioning_plugin(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        collector = _Collector()
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.records[0].transitioning = True

        status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual(0, collector.collect_count)
        self.assertTrue(status["transitioning"])

    def test_status_reports_background_initialization_lifecycle(self):
        entered = threading.Event()
        release_load = threading.Event()

        class BlockingCollector(_Collector):
            def collect(self):
                entered.set()
                release_load.wait(2)
                return super(BlockingCollector, self).collect()

        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(runtime_collector=BlockingCollector())

        self.assertEqual(
            "PENDING",
            registry.status_response(reconcile=False)["initializationState"])
        registry.discover()
        self.assertEqual(
            "LOADING",
            registry.status_response(reconcile=False)["initializationState"])

        worker = threading.Thread(target=registry.load_and_start)
        worker.start()
        self.assertTrue(entered.wait(1))
        loading = registry.status_response(reconcile=False)
        self.assertEqual("LOADING", loading["initializationState"])
        self.assertTrue(loading["plugins"][0]["transitioning"])

        release_load.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            "READY",
            registry.status_response(reconcile=False)["initializationState"])

    def test_status_does_not_reconcile_before_initialization_is_ready(self):
        collector = _Collector()
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(runtime_collector=collector)
        registry.discover()

        status = registry.status_response(reconcile=True)

        self.assertEqual("LOADING", status["initializationState"])
        self.assertEqual(0, collector.collect_count)

    def test_status_endpoint_reuses_reconciliation_until_interval_expires(self):
        clock = [100.0]
        collector = _Collector()
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(
            runtime_collector=collector, monotonic=lambda: clock[0])
        registry.status_reconcile_interval = 5
        registry.discover()
        registry.load_and_start()
        registry.register_status_endpoint()
        collector.collect_count = 0

        first = json.loads(
            self.http.sync[external_plugin_registry.STATUS_PATH]({}))
        second = json.loads(
            self.http.sync[external_plugin_registry.STATUS_PATH]({}))

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(1, collector.collect_count)

        clock[0] += 5
        json.loads(self.http.sync[external_plugin_registry.STATUS_PATH]({}))
        self.assertEqual(2, collector.collect_count)

    def test_status_reuses_content_verification_when_metadata_is_unchanged(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        original_digest = external_plugin_manifest._canonical_content_sha256
        digest_calls = []

        def counted_digest(release_root):
            digest_calls.append(release_root)
            return original_digest(release_root)

        external_plugin_manifest._canonical_content_sha256 = counted_digest
        try:
            first = registry.status_response(reconcile=True)["plugins"][0]
            second = registry.status_response(reconcile=True)["plugins"][0]
        finally:
            external_plugin_manifest._canonical_content_sha256 = original_digest

        self.assertEqual("COMPATIBLE", first["compatibilityState"])
        self.assertEqual("COMPATIBLE", second["compatibilityState"])
        self.assertEqual([], digest_calls)

    def test_status_revalidates_changed_release_once_and_caches_drift(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        _write(os.path.join(release, "sample_plugin", "plugin.py"),
               "# changed release content\n")
        original_digest = external_plugin_manifest._canonical_content_sha256
        digest_calls = []

        def counted_digest(release_root):
            digest_calls.append(release_root)
            return original_digest(release_root)

        external_plugin_manifest._canonical_content_sha256 = counted_digest
        try:
            first = registry.status_response(reconcile=True)["plugins"][0]
            second = registry.status_response(reconcile=True)["plugins"][0]
        finally:
            external_plugin_manifest._canonical_content_sha256 = original_digest

        self.assertEqual("DRIFTED_NEXT_START", first["compatibilityState"])
        self.assertEqual("DRIFTED_NEXT_START", second["compatibilityState"])
        self.assertEqual(1, len(digest_calls))

    def test_status_revalidates_changed_registry_once(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        registry_path = os.path.join(self.registry_root, "sample.ini")
        with open(registry_path, "a") as stream:
            stream.write("\n")
        parse_calls = []
        original_parse = registry._parse_registry

        def counted_parse(path):
            parse_calls.append(path)
            return original_parse(path)

        registry._parse_registry = counted_parse
        first = registry.status_response(reconcile=True)["plugins"][0]
        second = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual("COMPATIBLE", first["compatibilityState"])
        self.assertEqual("COMPATIBLE", second["compatibilityState"])
        self.assertEqual(1, len(parse_calls))

    def test_status_poll_python2_fallback_ignores_backward_wall_clock_jump(self):
        registry = self._registry()
        registry.status_reconcile_interval = 5
        reconciliations = []
        registry.status_response = lambda reconcile=True: (
            reconciliations.append(reconcile) or {})
        original_monotonic = getattr(time, "monotonic", None)

        try:
            if original_monotonic is not None:
                delattr(time, "monotonic")
            with mock.patch.object(time, "time", side_effect=(100.0, 90.0)), \
                 mock.patch.object(
                     restart_fence.os, "times",
                     side_effect=((0, 0, 0, 0, 100.0),
                                  (0, 0, 0, 0, 105.0))):
                registry._status_response_for_poll()
                registry._status_response_for_poll()
        finally:
            if original_monotonic is not None:
                time.monotonic = original_monotonic

        self.assertEqual([True, True], reconciliations)

    def test_status_reconciliation_uses_restart_fence_monotonic_clock(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")

        with mock.patch.object(
                external_plugin_registry, "monotonic_time",
                return_value=100.0) as clock:
            registry = self._registry()
            registry.discover()
            registry.load_and_start()
            status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertIs(clock, registry.monotonic)
        self.assertEqual("COMPATIBLE", status["compatibilityState"])
        self.assertFalse(status["stale"])

    def test_concurrent_status_polls_share_one_reconciliation(self):
        class BlockingCollector(_Collector):
            def __init__(self):
                super(BlockingCollector, self).__init__()
                self.block = False
                self.first_probe_entered = threading.Event()
                self.second_probe_entered = threading.Event()
                self.release_probe = threading.Event()

            def collect(self):
                self.collect_count += 1
                if self.block:
                    if self.collect_count == 1:
                        self.first_probe_entered.set()
                    elif self.collect_count == 2:
                        self.second_probe_entered.set()
                    self.release_probe.wait(1)
                return dict(self.versions)

        collector = BlockingCollector()
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.load_and_start()
        registry.register_status_endpoint()
        collector.collect_count = 0
        collector.block = True
        responses = []
        status_handler = self.http.sync[external_plugin_registry.STATUS_PATH]
        first = threading.Thread(target=lambda: responses.append(
            json.loads(status_handler({}))))
        second = threading.Thread(target=lambda: responses.append(
            json.loads(status_handler({}))))

        first.start()
        self.assertTrue(collector.first_probe_entered.wait(1))
        second.start()
        try:
            self.assertFalse(collector.second_probe_entered.wait(0.05))
            self.assertEqual(1, collector.collect_count)
        finally:
            collector.release_probe.set()
            first.join(1)
            second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(2, len(responses))
        self.assertTrue(all(response["success"] for response in responses))

    def test_runtime_reconciliation_fences_mutations_but_keeps_read_only_route(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        collector = _Collector()
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.load_and_start()
        context = registry.records[0].instance.context
        context.register_async_uri("/sample/mutate", lambda req: "changed", mutable=True)
        self.assertEqual("changed", self.http.async_["/sample/mutate"]({}))
        collector.versions["qemu"] = "100.0.0"
        status = registry.status_response(reconcile=True)["plugins"][0]
        self.assertEqual("INCOMPATIBLE_RUNTIME", status["compatibilityState"])
        fenced = json.loads(self.http.async_["/sample/mutate"]({}))
        self.assertFalse(fenced["success"])
        self.assertEqual("PLUGIN_RUNTIME_INCOMPATIBLE", fenced["errorCode"])
        self.assertEqual("{}", self.http.sync["/sample/status"]({}))

    def test_restart_fence_rejects_new_mutable_sync_route_request(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        context = registry.records[0].instance.context
        context.register_sync_uri(
            "/sample/mutate-sync", lambda req: "changed", mutable=True)
        acquired, unused_snapshot = AgentRestartFence.acquire(0.1, 1)
        self.assertTrue(acquired)

        raw_response = self.http.sync["/sample/mutate-sync"]({})
        self.assertNotEqual("changed", raw_response)
        response = json.loads(raw_response)

        self.assertFalse(response["success"])
        self.assertEqual("HOST_OPERATION_IN_PROGRESS", response["errorCode"])

    def test_restart_fence_drains_inflight_mutable_sync_route_request(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        context = registry.records[0].instance.context
        entered = threading.Event()
        release_handler = threading.Event()

        def mutate(unused_request):
            entered.set()
            release_handler.wait(1)
            return "changed"

        context.register_sync_uri("/sample/mutate-sync", mutate, mutable=True)
        handler_worker = threading.Thread(
            target=lambda: self.http.sync["/sample/mutate-sync"]({}))
        handler_worker.start()
        self.assertTrue(entered.wait(1))
        acquire_result = []
        acquire_worker = threading.Thread(
            target=lambda: acquire_result.append(
                AgentRestartFence.acquire(0.5, 1)))
        acquire_worker.start()
        try:
            deadline = time.time() + 1
            while AgentRestartFence.snapshot()["state"] != "FENCED":
                self.assertLess(time.time(), deadline)
                time.sleep(0.01)

            self.assertEqual(
                1, AgentRestartFence.snapshot()["activeRequestCount"])
            self.assertTrue(acquire_worker.is_alive())
        finally:
            release_handler.set()
            handler_worker.join(1)
            acquire_worker.join(1)

        self.assertFalse(handler_worker.is_alive())
        self.assertFalse(acquire_worker.is_alive())
        self.assertTrue(acquire_result[0][0])

    def test_mutable_async_route_leaves_restart_counting_to_http_layer(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        context = registry.records[0].instance.context
        context.register_async_uri(
            "/sample/mutate-async", lambda req: "changed", mutable=True)

        with mock.patch.object(
                AgentRestartFence, "enter_request",
                wraps=AgentRestartFence.enter_request) as enter_request:
            response = self.http.async_["/sample/mutate-async"]({})

        self.assertEqual("changed", response)
        enter_request.assert_not_called()
        self.assertEqual(
            0, AgentRestartFence.snapshot()["activeRequestCount"])

    def test_read_only_sync_route_is_not_counted_or_blocked_by_restart_fence(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        acquired, unused_snapshot = AgentRestartFence.acquire(0.1, 1)
        self.assertTrue(acquired)

        response = self.http.sync["/sample/status"]({})

        self.assertEqual("{}", response)
        self.assertEqual(
            0, AgentRestartFence.snapshot()["activeRequestCount"])

    def test_next_start_query_failure_cannot_mask_live_incompatibility(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        collector = _UnavailableNextStartCollector()
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.load_and_start()
        collector.versions["qemu"] = "100.0.0"

        status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual("FAILED", status["state"])
        self.assertEqual("INCOMPATIBLE_RUNTIME", status["compatibilityState"])
        self.assertEqual("PLUGIN_RUNTIME_INCOMPATIBLE",
                         status["failure"]["code"])

    def test_recovered_dependency_requires_controlled_restart(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        record = registry.records[0]
        record.fail("COMPATIBILITY_CHECK", "PLUGIN_RUNTIME_VERSION_UNAVAILABLE",
                    reason="DEPENDENCY_STARTUP_TIMEOUT")

        status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual("FAILED", status["state"])
        self.assertEqual("COMPATIBLE", status["compatibilityState"])
        self.assertTrue(status["restartRequired"])

    def test_initial_runtime_failure_recovers_to_restart_required(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        collector = _RecoveringCollector()
        registry = self._registry(runtime_collector=collector)
        registry.discover()
        registry.load_and_start()
        collector.available = True

        status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual("FAILED", status["state"])
        self.assertEqual("COMPATIBLE", status["compatibilityState"])
        self.assertEqual("READY", status["dependency"]["state"])
        self.assertTrue(status["restartRequired"])

    def test_group_writable_registry_is_rejected(self):
        if os.name == "nt":
            self.skipTest("Windows does not expose POSIX group-write mode bits")
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        path = os.path.join(self.registry_root, "sample.ini")
        os.chmod(path, 0o664)
        registry = self._registry()
        registry.discover()
        status = registry.status_response(reconcile=False)["plugins"][0]
        self.assertEqual("PLUGIN_REGISTRY_INVALID", status["failure"]["code"])

    def test_status_detects_registry_drift_without_reloading(self):
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        path = os.path.join(self.registry_root, "sample.ini")
        with open(path, "a") as stream:
            stream.write("unknown_option = changed\n")

        status = registry.status_response(reconcile=True)["plugins"][0]

        self.assertEqual("STARTED", status["state"])
        self.assertEqual("DRIFTED_NEXT_START", status["compatibilityState"])
        self.assertTrue(status["restartRequired"])

    def test_duplicate_route_isolated_to_later_plugin(self):
        first = self._release("first-plugin", "sample_plugin", "2.0.0")
        self._register("first", "first-plugin", first, "sample_plugin")
        second = self._release("second-plugin", "duplicate_plugin", "2.0.1")
        self._register("second", "second-plugin", second, "duplicate_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()

        statuses = dict((item["id"], item)
                        for item in registry.status_response(False)["plugins"])
        self.assertEqual("STARTED", statuses["first-plugin"]["state"])
        self.assertEqual("FAILED", statuses["second-plugin"]["state"])
        self.assertEqual("PLUGIN_START_FAILED",
                         statuses["second-plugin"]["failure"]["code"])

    def test_concurrent_dynamic_route_has_one_owner_and_reuses_after_stop(self):
        route = "/dynamic/shared"
        stops = []
        first = self._release("first-plugin", "loader_first", "2.0.0")
        second = self._release("second-plugin", "loader_second", "2.0.1")
        for release, namespace, plugin_id in (
                (first, "loader_first", "first-plugin"),
                (second, "loader_second", "second-plugin")):
            self._replace_plugin_source(
                release, namespace,
                "class Entry(object):\n"
                "    def configure(self, config):\n"
                "        self.context = config['externalPluginContext']\n"
                "        self.stops = config['stops']\n"
                "    def start(self): pass\n"
                "    def stop(self): self.stops.append(%r)\n" % plugin_id)
            self._register(plugin_id, plugin_id, release, namespace)
        registry = self._registry()
        registry.discover()
        registry.load_and_start({"stops": stops})
        records = dict((record.plugin_id, record) for record in registry.records)
        self.assertTrue(all(record.state == "STARTED"
                            for record in records.values()))

        second_contains = threading.Event()

        class DelayedFinalContains(dict):
            def __init__(self, initial):
                dict.__init__(self, initial)
                self.route_contains = 0
                self.guard = threading.Lock()

            def __contains__(self, key):
                value = dict.__contains__(self, key)
                if key != route + "/":
                    return value
                with self.guard:
                    self.route_contains += 1
                    current_contains = self.route_contains
                    if current_contains == 2:
                        second_contains.set()
                if current_contains == 1:
                    second_contains.wait(0.25)
                return value

        self.http.raw = DelayedFinalContains(self.http.raw)
        start = threading.Event()
        outcomes = []
        outcome_lock = threading.Lock()
        handlers = {}

        def register(plugin_id):
            handler = lambda unused_request, value=plugin_id: value
            handlers[plugin_id] = handler
            start.wait(1)
            try:
                records[plugin_id].context.register_sync_uri(
                    route, handler, mutable=False)
                outcome = ("success", plugin_id, None)
            except Exception as error:
                outcome = ("error", plugin_id, error)
            with outcome_lock:
                outcomes.append(outcome)

        workers = [threading.Thread(target=register, args=(plugin_id,))
                   for plugin_id in ("first-plugin", "second-plugin")]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(1)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

        successes = [item for item in outcomes if item[0] == "success"]
        errors = [item for item in outcomes if item[0] == "error"]
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0][2], ValueError)
        winner = successes[0][1]
        loser = errors[0][1]
        self.assertEqual(winner, registry._route_owners[route])
        self.assertIs(handlers[winner], self.http.sync[route])
        self.assertEqual([route], records[winner].registered_routes)
        self.assertEqual([], records[loser].registered_routes)

        registry.release_route(loser, route)
        self.assertEqual(winner, registry._route_owners[route])
        registry._rollback_record(records[winner], records[winner].context)
        self.assertNotIn(route, registry._route_owners)
        self.assertNotIn(route, self.http.sync)
        self.assertIsNone(records[winner].instance)

        original_register = self.http.register_sync_uri

        def fail_publish(unused_path, unused_handler):
            raise RuntimeError("dynamic publish failed")

        self.http.register_sync_uri = fail_publish
        try:
            with self.assertRaises(RuntimeError):
                records[loser].context.register_sync_uri(
                    route, handlers[loser], mutable=False)
        finally:
            self.http.register_sync_uri = original_register
        self.assertNotIn(route, registry._route_owners)
        self.assertNotIn(route, self.http.sync)

        reuse_handler = lambda unused_request: "reused"
        records[loser].context.register_sync_uri(
            route, reuse_handler, mutable=False)
        self.assertEqual(loser, registry._route_owners[route])
        self.assertIs(reuse_handler, self.http.sync[route])
        self.assertEqual([route], records[loser].registered_routes)
        registry.stop()
        self.assertNotIn(route, registry._route_owners)
        self.assertNotIn(route, self.http.sync)
        self.assertEqual(sorted(("first-plugin", "second-plugin")),
                         sorted(stops))

    def test_trailing_slash_alias_cannot_claim_a_second_owner(self):
        registry = self._registry()
        registry.claim_route("first-plugin", "/shared")

        with self.assertRaises(ValueError):
            registry.claim_route("second-plugin", "/shared/")

    def test_repeated_root_slashes_cannot_create_an_empty_owner_key(self):
        registry = self._registry()
        registry.claim_route("first-plugin", "/")

        with self.assertRaises(ValueError):
            registry.claim_route("second-plugin", "////")

    def test_unsupported_plugin_api_is_rejected_before_import(self):
        marker = os.path.join(self.root, "unsupported-api-imported")
        release = self._release(marker=marker, plugin_api=999)
        self._register(
            "sample", "sample-plugin", release, "sample_plugin",
            plugin_api=999)
        registry = self._registry()
        registry.discover()
        registry.load_and_start()

        status = registry.status_response(False)["plugins"][0]
        self.assertEqual("FAILED", status["state"])
        self.assertEqual("PRE_IMPORT_VALIDATION", status["failure"]["stage"])
        self.assertEqual("PLUGIN_API_UNSUPPORTED", status["failure"]["code"])
        self.assertFalse(os.path.exists(marker))

    def test_import_failure_does_not_execute_or_block_another_plugin(self):
        broken = self._release("broken-plugin", "sample_plugin", "2.0.0")
        self._replace_plugin_source(broken, "sample_plugin", "def broken(:\n")
        self._register("broken", "broken-plugin", broken, "sample_plugin")
        healthy = self._release("healthy-plugin", "duplicate_plugin", "2.0.1")
        self._replace_plugin_source(
            healthy, "duplicate_plugin",
            "class Entry(object):\n"
            "    def configure(self, config): self.context = config['externalPluginContext']\n"
            "    def start(self): self.context.register_sync_uri('/healthy/status', lambda req: '{}', mutable=False)\n"
            "    def stop(self): pass\n")
        self._register("healthy", "healthy-plugin", healthy, "duplicate_plugin")

        registry = self._registry()
        registry.discover()
        registry.load_and_start()
        statuses = dict((item["id"], item)
                        for item in registry.status_response(False)["plugins"])
        self.assertEqual("FAILED", statuses["broken-plugin"]["state"])
        self.assertEqual("PLUGIN_IMPORT_FAILED",
                         statuses["broken-plugin"]["failure"]["code"])
        self.assertEqual("STARTED", statuses["healthy-plugin"]["state"])

    def test_partial_routes_are_removed_when_start_fails(self):
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config): self.context = config['externalPluginContext']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/partial', lambda req: '{}', mutable=False)\n"
            "        raise RuntimeError('start failed')\n"
            "    def stop(self): pass\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()

        status = registry.status_response(False)["plugins"][0]
        self.assertEqual("FAILED", status["state"])
        self.assertEqual("PLUGIN_START_FAILED", status["failure"]["code"])
        self.assertNotIn("/partial", self.http.sync)
        self.assertEqual([], status["registeredRoutes"])

    def test_routes_are_published_only_after_start_succeeds(self):
        registered = threading.Event()
        release_start = threading.Event()
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.registered = config['registered']\n"
            "        self.release_start = config['release_start']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/staged', lambda req: '{}', mutable=False)\n"
            "        self.registered.set()\n"
            "        self.release_start.wait(2)\n"
            "    def stop(self): pass\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        worker = threading.Thread(target=lambda: registry.load_and_start({
            "registered": registered,
            "release_start": release_start,
        }))
        worker.start()
        self.assertTrue(registered.wait(1))

        self.assertNotIn("/staged", self.http.sync)
        self.assertTrue(registry.records[0].transitioning)

        release_start.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIn("/staged", self.http.sync)
        self.assertFalse(registry.records[0].transitioning)

    def test_stop_during_start_prevents_late_route_activation(self):
        entered_start = threading.Event()
        release_start = threading.Event()
        stopped = threading.Event()
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.entered_start = config['entered_start']\n"
            "        self.release_start = config['release_start']\n"
            "        self.stopped = config['stopped']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/late', lambda req: '{}', mutable=False)\n"
            "        self.entered_start.set()\n"
            "        self.release_start.wait(2)\n"
            "    def stop(self): self.stopped.set()\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        worker = threading.Thread(target=lambda: registry.load_and_start({
            "entered_start": entered_start,
            "release_start": release_start,
            "stopped": stopped,
        }))
        worker.start()
        self.assertTrue(entered_start.wait(1))

        registry.request_stop()
        registry.stop()
        release_start.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.wait(1))
        self.assertNotIn("/late", self.http.sync)
        self.assertIsNone(registry.records[0].instance)
        self.assertEqual("PLUGIN_START_CANCELLED",
                         registry.records[0].failure["code"])

    def test_stop_owns_post_activate_transition_without_retaining_instance(self):
        activated = threading.Event()
        release_activate = threading.Event()
        stop_returned = threading.Event()
        stops = []
        first = self._release("first-plugin", "loader_first", "2.0.0")
        self._replace_plugin_source(
            first, "loader_first",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.stops = config['stops']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/post-activate', lambda req: '{}', mutable=False)\n"
            "    def stop(self): self.stops.append('stop')\n")
        self._register("first", "first-plugin", first, "loader_first")
        registry = self._registry(stop_timeout_seconds=0.05)
        registry.discover()
        original_activate = external_plugin_registry.ExternalPluginContext.activate

        def activate_with_barrier(context):
            original_activate(context)
            activated.set()
            release_activate.wait(2)

        external_plugin_registry.ExternalPluginContext.activate = (
            activate_with_barrier)
        load_worker = threading.Thread(
            target=lambda: registry.load_and_start({"stops": stops}))
        stop_worker = threading.Thread(
            target=lambda: (registry.stop(), stop_returned.set()))
        try:
            load_worker.start()
            self.assertTrue(activated.wait(1))
            stop_worker.start()
            returned_while_transition_blocked = stop_returned.wait(0.1)
            retained_when_stop_returned = registry.records[0].instance
            state_when_stop_returned = registry.records[0].state
        finally:
            release_activate.set()
            external_plugin_registry.ExternalPluginContext.activate = (
                original_activate)
        load_worker.join(1)
        stop_worker.join(1)

        self.assertFalse(load_worker.is_alive())
        self.assertFalse(stop_worker.is_alive())
        self.assertTrue(returned_while_transition_blocked)
        self.assertIsNone(retained_when_stop_returned)
        self.assertNotEqual("STARTED", state_when_stop_returned)
        record = registry.records[0]
        self.assertIsNone(record.instance)
        self.assertIsNone(record.context)
        self.assertEqual([], record.registered_routes)
        self.assertNotIn("/post-activate", self.http.sync)
        self.assertEqual(["stop"], stops)

        second = self._release("second-plugin", "loader_second", "2.0.0")
        self._register("second", "second-plugin", second, "loader_second")
        follow_on = self._registry()
        follow_on.discover()
        follow_on.load_and_start()
        self.assertEqual("STARTED", follow_on._by_id["second-plugin"].state)

    def test_stop_during_configure_skips_plugin_start(self):
        entered_configure = threading.Event()
        release_configure = threading.Event()
        started = threading.Event()
        stopped = threading.Event()
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.entered_configure = config['entered_configure']\n"
            "        self.release_configure = config['release_configure']\n"
            "        self.started = config['started']\n"
            "        self.stopped = config['stopped']\n"
            "        self.entered_configure.set()\n"
            "        self.release_configure.wait(2)\n"
            "    def start(self): self.started.set()\n"
            "    def stop(self): self.stopped.set()\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        worker = threading.Thread(target=lambda: registry.load_and_start({
            "entered_configure": entered_configure,
            "release_configure": release_configure,
            "started": started,
            "stopped": stopped,
        }))
        worker.start()
        self.assertTrue(entered_configure.wait(1))

        registry.request_stop()
        registry.stop()
        release_configure.set()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertFalse(started.is_set())
        self.assertTrue(stopped.wait(1))
        self.assertIsNone(registry.records[0].instance)
        self.assertEqual("PLUGIN_START_CANCELLED",
                         registry.records[0].failure["code"])

    def test_stop_during_import_skips_plugin_configure(self):
        import_entered = os.path.join(self.root, "import-entered")
        release_import = os.path.join(self.root, "release-import")
        configured = os.path.join(self.root, "configured")
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "import os\n"
            "import time\n"
            "def mark(path, value):\n"
            "    with open(path, 'w') as stream: stream.write(value)\n"
            "mark(%r, 'entered')\n"
            "while not os.path.exists(%r): time.sleep(0.01)\n"
            "class Entry(object):\n"
            "    def configure(self, config): mark(%r, 'configured')\n"
            "    def start(self): pass\n"
            "    def stop(self): pass\n" %
            (import_entered, release_import, configured))
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        worker = threading.Thread(target=registry.load_and_start)
        worker.start()
        deadline = time.time() + 1
        while not os.path.exists(import_entered):
            self.assertLess(time.time(), deadline)
            time.sleep(0.01)

        registry.request_stop()
        registry.stop()
        _write(release_import, "continue")
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertFalse(os.path.exists(configured))
        self.assertIsNone(registry.records[0].instance)
        self.assertEqual("PLUGIN_START_CANCELLED",
                         registry.records[0].failure["code"])

    def test_hung_plugin_stop_is_bounded_and_routes_are_removed(self):
        stop_entered = threading.Event()
        release_stop = threading.Event()
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.stop_entered = config['stop_entered']\n"
            "        self.release_stop = config['release_stop']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/started', lambda req: '{}', mutable=False)\n"
            "    def stop(self):\n"
            "        self.stop_entered.set()\n"
            "        self.release_stop.wait(2)\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(stop_timeout_seconds=0.05)
        registry.discover()
        registry.load_and_start({
            "stop_entered": stop_entered,
            "release_stop": release_stop,
        })
        started_at = time.time()
        try:
            registry.stop()
            elapsed = time.time() - started_at
            self.assertTrue(stop_entered.wait(1))
            self.assertLess(elapsed, 0.5)
            self.assertNotIn("/started", self.http.sync)
        finally:
            release_stop.set()

    def test_plugin_stop_timeout_uses_monotonic_clock_when_wall_clock_moves_backward(self):
        stop_entered = threading.Event()
        release_stop = threading.Event()

        class BlockingPlugin(object):
            def stop(self):
                stop_entered.set()
                release_stop.wait(0.4)

        monotonic_values = iter((10.0, 10.01))
        registry = self._registry(
            stop_timeout_seconds=0.05,
            monotonic=lambda: next(monotonic_values))

        started_at = time.time()
        try:
            with mock.patch.object(
                    external_plugin_registry.time, "time",
                    side_effect=(100.0, 99.0)):
                failures = registry._stop_instances_bounded(
                    [BlockingPlugin()])
            elapsed = time.time() - started_at
        finally:
            release_stop.set()

        self.assertTrue(stop_entered.wait(1))
        self.assertLess(elapsed, 0.25)
        self.assertEqual("PLUGIN_STOP_TIMEOUT", failures[0]["code"])

    def test_partial_routes_are_removed_when_configure_fails(self):
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.context.register_sync_uri('/partial-configure', lambda req: '{}', mutable=False)\n"
            "        raise RuntimeError('configure failed')\n"
            "    def start(self): pass\n"
            "    def stop(self): pass\n")
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()

        status = registry.status_response(False)["plugins"][0]
        self.assertEqual("FAILED", status["state"])
        self.assertEqual("PLUGIN_CONFIGURE_FAILED", status["failure"]["code"])
        self.assertNotIn("/partial-configure", self.http.sync)
        self.assertEqual([], status["registeredRoutes"])

    def test_configure_failure_stops_partial_instance(self):
        stopped = os.path.join(self.root, "configure-stopped")
        release = self._release()
        self._replace_plugin_source(
            release, "sample_plugin",
            "class Entry(object):\n"
            "    def configure(self, config): raise RuntimeError('configure failed')\n"
            "    def start(self): pass\n"
            "    def stop(self):\n"
            "        with open(%r, 'w') as stream:\n"
            "            stream.write('stopped')\n" % stopped)
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry()
        registry.discover()
        registry.load_and_start()

        self.assertTrue(os.path.exists(stopped))
        self.assertIsNone(registry.records[0].instance)

    def test_hung_rollback_stop_is_bounded_and_follow_on_plugin_loads(self):
        stop_entered = threading.Event()
        release_stop = threading.Event()
        first = self._release("first-plugin", "loader_first", "2.0.0")
        self._replace_plugin_source(
            first, "loader_first",
            "class Entry(object):\n"
            "    def configure(self, config):\n"
            "        self.context = config['externalPluginContext']\n"
            "        self.stop_entered = config['stop_entered']\n"
            "        self.release_stop = config['release_stop']\n"
            "    def start(self):\n"
            "        self.context.register_sync_uri('/rollback-route', lambda req: '{}', mutable=False)\n"
            "        raise RuntimeError('start failed')\n"
            "    def stop(self):\n"
            "        self.stop_entered.set()\n"
            "        self.release_stop.wait(0.4)\n")
        second = self._release("second-plugin", "loader_second", "2.0.0")
        self._register("first", "first-plugin", first, "loader_first")
        self._register("second", "second-plugin", second, "loader_second")
        registry = self._registry(stop_timeout_seconds=0.05)
        registry.discover()

        started = time.time()
        try:
            registry.load_and_start({
                "stop_entered": stop_entered,
                "release_stop": release_stop,
            })
            elapsed = time.time() - started
        finally:
            release_stop.set()

        records = dict((record.plugin_id, record) for record in registry.records)
        first_status = records["first-plugin"].status()
        self.assertTrue(stop_entered.is_set())
        self.assertLess(elapsed, 0.25)
        self.assertEqual("PLUGIN_START_FAILED", first_status["failure"]["code"])
        self.assertEqual("PLUGIN_STOP_TIMEOUT",
                         first_status["failure"]["cleanupCode"])
        self.assertEqual([], first_status["registeredRoutes"])
        self.assertNotIn("/rollback-route", self.http.sync)
        self.assertIsNone(records["first-plugin"].instance)
        self.assertEqual("STARTED", records["second-plugin"].state)

    def test_status_collect_timeout_is_bounded_and_next_status_recovers(self):
        release_collect = threading.Event()

        class BlockingCollect(_Collector):
            def __init__(self):
                super(BlockingCollect, self).__init__()
                self.calls = 0
                self.block_once = False

            def collect(self, timeout_seconds=None):
                self.calls += 1
                if self.block_once:
                    self.block_once = False
                    release_collect.wait(0.4)
                return super(BlockingCollect, self).collect()

        collector = BlockingCollect()
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(runtime_collector=collector)
        registry.status_reconcile_deadline = 0.05
        registry.discover()
        registry.load_and_start()
        collector.block_once = True

        started = time.time()
        first = registry.status_response(reconcile=True)["plugins"][0]
        first_elapsed = time.time() - started
        try:
            self.assertLess(first_elapsed, 0.25)
            self.assertEqual("UNKNOWN", first["compatibilityState"])
            self.assertTrue(first["stale"])

            started = time.time()
            second = registry.status_response(reconcile=True)["plugins"][0]
            self.assertLess(time.time() - started, 0.25)
            self.assertEqual("COMPATIBLE", second["compatibilityState"])
            self.assertFalse(second["stale"])
        finally:
            release_collect.set()

    def test_status_next_start_uses_remaining_shared_deadline(self):
        release_next_start = threading.Event()

        class BlockingNextStart(_Collector):
            def __init__(self):
                super(BlockingNextStart, self).__init__()
                self.block = False
                self.current_budgets = []
                self.next_budgets = []

            def collect(self, timeout_seconds=None):
                if self.block:
                    self.current_budgets.append(timeout_seconds)
                    time.sleep(0.03)
                return super(BlockingNextStart, self).collect()

            def collect_next_start(self, timeout_seconds=None):
                if self.block:
                    self.next_budgets.append(timeout_seconds)
                    self.block = False
                    release_next_start.wait(0.4)
                return dict(self.versions)

        collector = BlockingNextStart()
        release = self._release()
        self._register("sample", "sample-plugin", release, "sample_plugin")
        registry = self._registry(runtime_collector=collector)
        registry.status_reconcile_deadline = 0.05
        registry.discover()
        registry.load_and_start()
        collector.block = True

        started = time.time()
        first = registry.status_response(reconcile=True)["plugins"][0]
        first_elapsed = time.time() - started
        try:
            self.assertLess(first_elapsed, 0.25)
            self.assertEqual("UNKNOWN", first["compatibilityState"])
            self.assertTrue(first["stale"])
            self.assertEqual(1, len(collector.current_budgets))
            self.assertEqual(1, len(collector.next_budgets))
            self.assertGreater(collector.current_budgets[0], 0)
            self.assertLess(collector.next_budgets[0],
                            collector.current_budgets[0])
        finally:
            release_next_start.set()

        started = time.time()
        second = registry.status_response(reconcile=True)["plugins"][0]
        self.assertLess(time.time() - started, 0.25)
        self.assertEqual("COMPATIBLE", second["compatibilityState"])


if __name__ == "__main__":
    unittest.main()
