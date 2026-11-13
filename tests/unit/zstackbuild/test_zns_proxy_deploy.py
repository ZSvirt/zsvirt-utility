import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[3] / 'zstackznsproxy/ansible/znsproxy.py'


class ZnsProxyDeployTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proxy's package ")
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source.bin'
        self.source.write_bytes(b'new package')
        self.destination = Path(self.temp.name) / 'remote/proxy.bin'
        self.marker = Path(str(self.destination) + '.installed.sha256')
        self.install_calls = 0
        self.fail_install = False
        self.healthy = True
        self.running_package = b'old package'
        self.copy_results = []
        self.lib = types.ModuleType('zstacklib')
        self.lib.HostPostInfo = type('HostPostInfo', (), {})
        self.lib.CopyArg = type('CopyArg', (), {})
        self.lib.create_log = lambda *args: None
        self.lib.banner = lambda *args: None
        self.lib.handle_ansible_info = lambda *args: None
        self.lib.copy = self.copy
        self.lib.run_remote_command = self.remote

    def copy(self, args, info):
        unchanged = os.path.exists(args.dest) and Path(args.dest).read_bytes() == Path(args.src).read_bytes()
        shutil.copyfile(args.src, args.dest)
        result = 'changed:' + str(not unchanged)
        self.copy_results.append(result)
        return result

    def remote(self, command, info, return_status=False):
        if ' install --listen-address ' in command:
            self.install_calls += 1
            if self.fail_install:
                raise SystemExit(1)
            self.running_package = self.destination.read_bytes()
            return True
        if command.startswith('curl '):
            success = self.healthy
        else:
            success = subprocess.call(command, shell=True, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL) == 0
        if not success and not return_status:
            raise SystemExit(1)
        return success

    def deploy(self):
        args = {'host': 'test-host', 'src_pkg_znsproxy': str(self.source),
                'dst_pkg_znsproxy': str(self.destination)}
        with patch.dict(sys.modules, {'zstacklib': self.lib}), patch.object(sys, 'argv',
                [str(SCRIPT), '-e', json.dumps(args)]):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(SCRIPT), run_name='__main__')
        return result.exception.code

    def test_failed_upgrade_retries_even_when_old_service_is_healthy(self):
        self.fail_install = True
        self.assertEqual(1, self.deploy())
        self.assertFalse(self.marker.exists())
        self.assertEqual(b'old package', self.running_package)
        self.fail_install = False
        self.assertEqual(0, self.deploy())
        self.assertEqual(['changed:True', 'changed:False'], self.copy_results)
        self.assertEqual(2, self.install_calls)
        self.assertEqual(self.source.read_bytes(), self.running_package)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.marker.read_text().strip())
        self.assertEqual(0, self.deploy())
        self.assertEqual(2, self.install_calls)

    def test_failed_health_check_does_not_mark_package_installed(self):
        self.healthy = False
        self.assertEqual(1, self.deploy())
        self.assertFalse(self.marker.exists())
        self.healthy = True
        self.assertEqual(0, self.deploy())
        self.assertEqual(2, self.install_calls)

    def test_different_package_requires_install_despite_healthy_service(self):
        self.assertEqual(0, self.deploy())
        self.source.write_bytes(b'next package')
        self.assertEqual(0, self.deploy())
        self.assertEqual(2, self.install_calls)
        self.assertEqual(b'next package', self.running_package)

    def test_unhealthy_service_reinstalls_and_invalidates_previous_marker(self):
        self.assertEqual(0, self.deploy())
        self.assertTrue(self.marker.exists())
        self.healthy = False
        self.fail_install = True
        self.assertEqual(1, self.deploy())
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
