# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import shutil
import sys
import tempfile
import unittest

from kvmagent.external_plugin_registry import ExternalPluginRecord, ExternalPluginRegistry


VERSIONS = {
    "python": "3.11.9", "kvmAgent": "5.1.0", "zstacklib": "5.1.0",
    "qemu": "8.2.0", "libvirt": "9.0.0",
    "os": "centos7", "architectures": "x86_64",
}


class _Manifest(object):
    namespace = "loader_sample"
    entry_module = "loader_sample.plugin"
    entry_class = "Entry"
    compatibility = dict((name, ">=1.0,<99.0") for name in (
        "python", "kvmAgent", "zstacklib", "qemu", "libvirt"))
    compatibility.update({
        "os": ["centos7", "kylin10", "helix8.4r"],
        "architectures": ["x86_64", "aarch64"],
    })

    def metadata(self):
        return {"version": "2.0.0", "sha256": "a" * 64, "pluginApi": 1}


class _Collector(object):
    def __init__(self, versions):
        self.versions = versions

    def collect(self):
        return dict(self.versions)


class _HttpServer(object):
    def register_sync_uri(self, unused_path, unused_handler):
        pass


class ExternalPluginLoaderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        package = os.path.join(self.root, "loader_sample")
        os.makedirs(package)
        with open(os.path.join(package, "__init__.py"), "w") as stream:
            stream.write("")
        self.marker = os.path.join(self.root, "imported")
        with open(os.path.join(package, "plugin.py"), "w") as stream:
            stream.write(
                "with open(%r, 'w') as marker:\n"
                "    marker.write('yes')\n"
                "class Entry(object):\n"
                "    def configure(self, config): pass\n"
                "    def start(self): pass\n"
                "    def stop(self): pass\n" % self.marker)

    def tearDown(self):
        for name in list(sys.modules):
            if name == "loader_sample" or name.startswith("loader_sample."):
                sys.modules.pop(name, None)
        shutil.rmtree(self.root)

    def _registry(self, versions):
        registry = ExternalPluginRegistry(
            _HttpServer(), registry_root=os.path.join(self.root, "missing"),
            managed_root=self.root, expected_uid=None,
            runtime_collector=_Collector(versions), protected_namespaces=set())
        record = ExternalPluginRecord("loader-sample")
        record.manifest = _Manifest()
        record.release_root = self.root
        record.resolved_release_root = self.root
        registry.records = [record]
        registry._by_id[record.plugin_id] = record
        return registry, record

    def test_runtime_gate_runs_before_importing_release_code(self):
        incompatible = dict(VERSIONS)
        incompatible["qemu"] = "100.0.0"
        registry, record = self._registry(incompatible)

        registry.load_and_start()

        self.assertEqual("FAILED", record.state)
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn(self.root, sys.path)

    def test_successful_import_does_not_mutate_immutable_release(self):
        registry, record = self._registry(VERSIONS)

        registry.load_and_start()

        self.assertEqual("STARTED", record.state)
        self.assertTrue(os.path.exists(self.marker))
        self.assertNotIn(self.root, sys.path)
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "loader_sample", "__pycache__")))


if __name__ == "__main__":
    unittest.main()
