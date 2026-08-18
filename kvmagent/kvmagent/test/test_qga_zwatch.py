# -*- coding: utf-8 -*-
"""Unit tests for Windows nic-info collection paths in qga_zwatch.

Mock QGA only - no real VMs / libvirt.
Compatible with Python 2 and Python 3 unittest discovery.
"""
from __future__ import absolute_import

import os
import sys
import threading
import types
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


qga_zwatch = None
_modules_before = None


def _ensure_module(name):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    parent_name, _, child = name.rpartition('.')
    if parent_name:
        parent = _ensure_module(parent_name)
        setattr(parent, child, mod)
    return mod


def _load_module_from_path(name, path):
    """Load a module file on both Python 2 and Python 3."""
    try:
        import importlib.util as importlib_util
    except ImportError:  # Python 2
        import imp
        mod = imp.load_source(name, path)
        sys.modules[name] = mod
        return mod

    spec = importlib_util.spec_from_file_location(name, path)
    mod = importlib_util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _restore_sys_modules(before):
    """Undo sys.modules mutations so discovery order cannot leak stubs."""
    before_keys = set(before)
    after_keys = set(sys.modules)
    for key in after_keys - before_keys:
        sys.modules.pop(key, None)
    for key in before_keys:
        prev = before[key]
        if sys.modules.get(key) is not prev:
            sys.modules[key] = prev


def _load_qga_zwatch():
    """Load qga_zwatch.py with dependency stubs (avoids full kvmagent runtime)."""
    sys.modules['libvirt'] = mock.MagicMock()
    sys.modules['libvirt_qemu'] = mock.MagicMock()

    bare_log = _ensure_module('log')
    bare_log.get_logger = mock.Mock(return_value=mock.Mock())
    bare_log.configure_log = mock.Mock()

    log_mod = _ensure_module('zstacklib.utils.log')
    log_mod.configure_log = mock.Mock()
    log_mod.get_logger = mock.Mock(return_value=mock.Mock())

    thread_mod = _ensure_module('zstacklib.utils.thread')
    lock_mod = _ensure_module('zstacklib.utils.lock')
    _ensure_module('zstacklib.utils.http').json_dump_post = mock.Mock()
    _ensure_module('zstacklib.utils.jsonobject')
    _ensure_module('zstacklib.utils.shell')

    qga_stub = _ensure_module('zstacklib.utils.qga')

    class VmQga(object):
        QGA_STATE_RUNNING = 'running'
        ZS_TOOLS_PATN_WIN = r'C:\Program Files\GuestTools\zs-tools\zs-tools.exe'

    qga_stub.VmQga = VmQga

    def AsyncThread(f):
        """Identity decorator so methods stay normal bound methods in tests."""
        return f

    thread_mod.AsyncThread = AsyncThread

    def _lock_decorator(*_a, **_k):
        def deco(f):
            return f
        return deco

    lock_mod.lock = _lock_decorator

    kvmagent_mod = _ensure_module('kvmagent.kvmagent')

    class KvmAgent(object):
        pass

    class KvmError(Exception):
        pass

    def replyerror(func):
        return func

    kvmagent_mod.KvmAgent = KvmAgent
    kvmagent_mod.KvmError = KvmError
    kvmagent_mod.replyerror = replyerror
    kvmagent_mod.get_http_server = mock.Mock()
    kvmagent_mod.SEND_COMMAND_URL = 'SEND_COMMAND_URL'
    _ensure_module('kvmagent').kvmagent = kvmagent_mod

    vm_plugin = _ensure_module('kvmagent.plugins.vm_plugin')

    def LibvirtAutoReconnect(func):
        return func

    class Vm(object):
        VIR_DOMAIN_RUNNING = 1

    vm_plugin.LibvirtAutoReconnect = LibvirtAutoReconnect
    vm_plugin.Vm = Vm
    _ensure_module('kvmagent.plugins.host_pushgateway')

    here = os.path.dirname(os.path.abspath(__file__))
    plugin_path = os.path.abspath(os.path.join(here, '..', 'plugins', 'qga_zwatch.py'))
    # Flat module name avoids polluting kvmagent.plugins package namespace.
    mod = _load_module_from_path('qga_zwatch_under_test', plugin_path)
    return mod


def setUpModule():
    global qga_zwatch, _modules_before
    _modules_before = sys.modules.copy()
    qga_zwatch = _load_qga_zwatch()


def tearDownModule():
    global qga_zwatch, _modules_before
    if _modules_before is not None:
        _restore_sys_modules(_modules_before)
        _modules_before = None
    qga_zwatch = None


NIC_JSON = '{"fa:11:22:33:44:55":["10.0.0.2/24","gateway:10.0.0.1"]}'
PS_JSON = '{"fa:11:22:33:44:55":["10.0.0.2/24"]}'


class TestQgaGetVmNic(unittest.TestCase):
    def setUp(self):
        self.monitor = qga_zwatch.ZWatchMetricMonitor()
        self.monitor.send_nic_info_to_mn = mock.Mock()
        self.uuid = 'vm-uuid-1'
        self.qga = mock.Mock()
        self.qga.os = 'mswindows'
        self.qga.os_version = '10'  # not 2008r2
        self.exe = self.monitor.WIN_ZWATCH_GET_NIC_INFO_EXE_PATH
        self.ps1 = self.monitor.WIN_ZWATCH_GET_NIC_INFO_PATH

    def _exist_map(self, mapping):
        def _exist(path):
            return bool(mapping.get(path))
        return _exist

    def test_exe_success_does_not_fallback_to_powershell(self):
        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: True,
        })
        self.qga.guest_exec_program_no_exitcode.return_value = NIC_JSON

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)

        self.qga.guest_exec_program_no_exitcode.assert_called_once_with(self.exe, ['nic-info'])
        self.qga.guest_exec_cmd_no_exitcode.assert_not_called()
        self.assertEqual(self.monitor.vm_nic_info[self.uuid], NIC_JSON)
        self.monitor.send_nic_info_to_mn.assert_called_once_with(self.uuid, NIC_JSON)
        self.assertNotIn(self.uuid, self.monitor.vm_nic_inflight)

    def test_exe_nonzero_falls_back_to_powershell(self):
        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: True,
        })
        self.qga.guest_exec_program_no_exitcode.side_effect = Exception('exitcode 1')
        self.qga.guest_exec_cmd_no_exitcode.return_value = PS_JSON

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)

        self.qga.guest_exec_program_no_exitcode.assert_called_once()
        self.qga.guest_exec_cmd_no_exitcode.assert_called_once_with(self.ps1)
        self.assertEqual(self.monitor.vm_nic_info[self.uuid], PS_JSON)
        self.assertNotIn(self.uuid, self.monitor.vm_nic_inflight)

    def test_ps1_only_when_exe_missing(self):
        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: False,
        })
        self.qga.guest_exec_cmd_no_exitcode.return_value = PS_JSON

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)

        self.qga.guest_exec_program_no_exitcode.assert_not_called()
        self.qga.guest_exec_cmd_no_exitcode.assert_called_once_with(self.ps1)
        self.assertEqual(self.monitor.vm_nic_info[self.uuid], PS_JSON)

    def test_concurrent_second_call_skipped_and_inflight_cleared(self):
        started = threading.Event()
        release = threading.Event()

        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: True,
        })

        def _slow_exe(_path, _args):
            started.set()
            self.assertTrue(release.wait(timeout=2), 'test deadlock waiting for release')
            return NIC_JSON

        self.qga.guest_exec_program_no_exitcode.side_effect = _slow_exe

        t = threading.Thread(target=self.monitor.qga_get_vm_nic, args=(self.uuid, self.qga))
        t.start()
        self.assertTrue(started.wait(timeout=2))

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)
        self.assertEqual(self.qga.guest_exec_program_no_exitcode.call_count, 1)

        release.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertNotIn(self.uuid, self.monitor.vm_nic_inflight)

        self.qga.guest_exec_program_no_exitcode.side_effect = None
        self.qga.guest_exec_program_no_exitcode.return_value = NIC_JSON
        self.monitor.qga_get_vm_nic(self.uuid, self.qga)
        self.assertEqual(self.qga.guest_exec_program_no_exitcode.call_count, 2)
        self.assertNotIn(self.uuid, self.monitor.vm_nic_inflight)

    def test_lock_busy_skip_keeps_cache_and_does_not_send_or_fallback(self):
        """Cross-repo: zs-tools lock-busy sentinel must skip (cache/MN/PS unchanged)."""
        cached = NIC_JSON
        self.monitor.vm_nic_info[self.uuid] = cached
        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: True,
        })
        # Simulate zs-tools stdout including Windows CRLF.
        self.qga.guest_exec_program_no_exitcode.return_value = (
            qga_zwatch.NIC_INFO_SKIP_LOCK_BUSY_JSON + '\r\n'
        )

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)

        self.qga.guest_exec_program_no_exitcode.assert_called_once_with(self.exe, ['nic-info'])
        self.assertEqual(self.qga.guest_exec_cmd_no_exitcode.call_count, 0)
        self.assertEqual(self.monitor.vm_nic_info[self.uuid], cached)
        self.assertEqual(self.monitor.send_nic_info_to_mn.call_count, 0)
        self.assertNotIn(self.uuid, self.monitor.vm_nic_inflight)
        self.assertTrue(qga_zwatch.is_nic_info_skip(
            qga_zwatch.NIC_INFO_SKIP_LOCK_BUSY_JSON + '\r\n'))
        self.assertFalse(qga_zwatch.is_nic_info_skip('{}'))
        self.assertFalse(qga_zwatch.is_nic_info_skip(cached))

    def test_empty_map_is_not_treated_as_lock_busy_skip(self):
        """Real empty nic snapshot "{}" must still update cache / report."""
        self.monitor.vm_nic_info[self.uuid] = NIC_JSON
        self.qga.guest_file_is_exist.side_effect = self._exist_map({
            self.ps1: True,
            self.exe: True,
        })
        self.qga.guest_exec_program_no_exitcode.return_value = '{}'

        self.monitor.qga_get_vm_nic(self.uuid, self.qga)

        self.qga.guest_exec_cmd_no_exitcode.assert_not_called()
        self.assertEqual(self.monitor.vm_nic_info[self.uuid], '{}')
        self.monitor.send_nic_info_to_mn.assert_called_once_with(self.uuid, '{}')


if __name__ == '__main__':
    unittest.main()
