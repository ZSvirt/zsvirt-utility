# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import unittest

try:
    from importlib import util as importlib_util
except ImportError:
    importlib_util = None

try:
    import imp
except ImportError:
    imp = None


REPOSITORY_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..'))
LAYOUT_PATH = os.path.join(
    REPOSITORY_ROOT, 'kvmagent', 'ansible', 'external_plugin_layout.py')


class ExternalPluginLayoutTest(unittest.TestCase):

    def _load_layout(self):
        self.assertTrue(
            os.path.isfile(LAYOUT_PATH),
            'external plugin layout installer is missing: %s' % LAYOUT_PATH)
        if importlib_util is not None:
            spec = importlib_util.spec_from_file_location(
                'external_plugin_layout', LAYOUT_PATH)
            module = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        return imp.load_source('external_plugin_layout', LAYOUT_PATH)

    def test_install_creates_root_owned_non_writable_registry_directory(self):
        layout = self._load_layout()
        calls = []

        def run(command, host, **kwargs):
            calls.append((command, host, kwargs))
            return True

        host = object()
        layout.install_registry_root(host, run)

        self.assertEqual(1, len(calls))
        self.assertEqual(
            'install -d -o root -g root -m 0755 -- '
            '/etc/zstack/kvmagent/plugins.d', calls[0][0])
        self.assertIs(host, calls[0][1])
        self.assertEqual({'return_status': True}, calls[0][2])

    def test_install_fails_closed_when_remote_directory_creation_fails(self):
        layout = self._load_layout()

        def run(unused_command, unused_host, **unused_kwargs):
            return False

        with self.assertRaises(layout.ExternalPluginLayoutUnavailable):
            layout.install_registry_root(object(), run)


if __name__ == '__main__':
    unittest.main()
