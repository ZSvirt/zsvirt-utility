# -*- coding: utf-8 -*-
"""Unit tests for Windows nic-info collection paths in qga_zwatch.

Mock QGA only - no real VMs / libvirt.
Compatible with Python 2 and Python 3 unittest discovery.
"""
from __future__ import absolute_import

import os
import json
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
        self.qga._zwatch_file_legacy_until = 0
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



class TestWindowsZWatchFile(unittest.TestCase):
    def setUp(self):
        self.qga = mock.Mock()
        self.qga._zwatch_file_legacy_until = 0

    def test_missing_file_and_truncated_response(self):
        self.qga.guest_exec_program.return_value = (2, b'')
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'missing'), (0, None))
        self.qga.guest_exec_program.return_value = (0, b'100:YWJj')
        with self.assertRaises(Exception):
            qga_zwatch.windows_zwatch_file(self.qga, 'metrics')

    def test_failed_exec_does_not_fall_back_to_persistent_handles(self):
        self.qga.guest_exec_program.side_effect = Exception('RPC timeout')
        with self.assertRaises(Exception):
            qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
        self.qga.guest_file_open.assert_not_called()
        self.qga.guest_file_close.assert_not_called()

    def test_guest_exec_byte_output(self):
        self.qga.guest_exec_program.return_value = (0, b'3:YWJj')
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'metrics'), (3, 'abc'))

    def test_rejects_corrupt_base64_even_with_matching_byte_count(self):
        self.qga.guest_exec_program.return_value = (0, b'3:YWJj!')
        with self.assertRaises(Exception):
            qga_zwatch.windows_zwatch_file(self.qga, 'metrics')

    def test_native_command_arguments_do_not_use_a_shell(self):
        path = "C:\\GuestTools-'$-%&!\\vm.info"
        self.qga.guest_exec_program.return_value = (0, b'3:YWJj')
        qga_zwatch.windows_zwatch_file(self.qga, path, 'abc')
        self.qga.guest_exec_program.assert_called_once_with(
            qga_zwatch.VmQga.ZS_TOOLS_PATN_WIN, ['zwatch-file', path, 'YWJj'], retry=10)

    def old_tool(self):
        self.qga.guest_exec_program.return_value = (
            1, b'Error: unknown command "zwatch-file" for "zs-tools"\r\n'
               b'Run \'zs-tools --help\' for usage.\r\n')

    def test_old_tool_reads_metrics_and_empty_files(self):
        self.old_tool()
        self.qga.guest_file_is_exist.return_value = True
        for result in [(3, 'cpu'), (0, '')]:
            self.qga.guest_file_read.return_value = result
            self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'metrics'), result)
        self.qga.guest_file_write.assert_not_called()

    def test_old_tool_missing_file_preserves_missing_semantics(self):
        self.old_tool()
        description = "failed to open file 'vm.info': The system cannot find the file specified."
        self.qga.guest_file_read.side_effect = self.open_error(description)
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'vm.info'), (0, None))
        self.qga.guest_file_is_exist.assert_not_called()

    def test_old_tool_writes_utf8_and_empty_content(self):
        self.old_tool()
        for contents in [u'vm-\u6570\u636e', '']:
            data = contents.encode('utf-8')
            self.qga.guest_file_write.return_value = len(data)
            expected = data if isinstance(data, str) else contents
            self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'vm.info', contents),
                             (len(data), expected))
            self.qga.guest_file_write.assert_called_with('vm.info', data)
        self.qga.guest_file_read.assert_not_called()
        self.qga.guest_file_is_exist.assert_not_called()

    def test_old_tool_short_write_is_not_success(self):
        self.old_tool()
        self.qga.guest_file_write.return_value = 1
        with self.assertRaises(Exception):
            qga_zwatch.windows_zwatch_file(self.qga, 'vm.info', 'uuid')

    def test_runtime_errors_never_fall_back_to_legacy(self):
        for output in [None, b'sharing violation', b'access denied', b'zwatch-file timed out',
                       b'Error: unknown command "other" for "zs-tools"',
                       b'open Error: unknown command "zwatch-file" for "zs-tools": denied']:
            self.qga.guest_exec_program.return_value = (1, output)
            with self.assertRaises(Exception):
                qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
        self.qga.guest_file_is_exist.assert_not_called()
        self.qga.guest_file_read.assert_not_called()
        self.qga.guest_file_write.assert_not_called()

    def test_old_tool_read_and_close_errors_propagate(self):
        self.old_tool()
        self.qga.guest_file_is_exist.return_value = True
        self.qga.guest_file_read.side_effect = Exception('close failed')
        with self.assertRaises(Exception):
            qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
        self.assertEqual(self.qga.guest_file_read.call_count, 1)

    @staticmethod
    def open_error(description, error_class='GenericError'):
        return Exception('qga command return value format error:' +
                         json.dumps({'error': {'class': error_class, 'desc': description}}))

    def test_missing_errors_are_exact_and_other_errors_propagate(self):
        self.old_tool()
        errors = [Exception('RPC timeout: No such file or directory'),
                  self.open_error("failed to open file 'metrics': Access is denied."),
                  self.open_error("failed to open file 'metrics': sharing violation"),
                  self.open_error("failed to open file 'other': No such file or directory"),
                  self.open_error("failed to open file 'metrics': No such file or directory", 'CommandDisabled'),
                  Exception('qga command return value format error:invalid-json')]
        for error in errors:
            self.qga.guest_file_read.side_effect = error
            with self.assertRaises(Exception) as caught:
                qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
            self.assertIs(caught.exception, error)
        self.qga.guest_file_is_exist.assert_not_called()
        self.qga.guest_file_read.assert_called_with('metrics', not_exist_exception=True)

    def test_negative_cache_avoids_probe_until_expiry_and_rechecks_upgrade(self):
        self.old_tool()
        unsupported = self.qga.guest_exec_program.return_value
        self.qga.guest_exec_program.side_effect = [unsupported, (0, b'3:bmV3'), unsupported]
        self.qga.guest_file_read.return_value = (3, 'old')
        with mock.patch.object(qga_zwatch.time, 'time', side_effect=[100, 120, 160, 161]):
            results = [qga_zwatch.windows_zwatch_file(self.qga, 'metrics') for _ in range(4)]
        self.assertEqual(results, [(3, 'old'), (3, 'old'), (3, 'new'), (3, 'old')])
        self.assertEqual(self.qga.guest_file_read.call_count, 3)
        self.assertEqual(self.qga.guest_exec_program.call_count, 3)
        self.assertEqual(self.qga._zwatch_file_legacy_until, 221)

    def test_negative_cache_is_not_shared_between_vms(self):
        self.old_tool()
        self.qga.guest_file_read.return_value = (3, 'old')
        other = mock.Mock()
        other._zwatch_file_legacy_until = 0
        other.guest_exec_program.return_value = (0, b'3:bmV3')
        with mock.patch.object(qga_zwatch.time, 'time', return_value=100):
            self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'metrics'), (3, 'old'))
            self.assertEqual(qga_zwatch.windows_zwatch_file(other, 'metrics'), (3, 'new'))
        other.guest_file_read.assert_not_called()

    def test_clock_rollback_invalidates_negative_cache(self):
        self.qga._zwatch_file_legacy_until = 160
        self.qga.guest_exec_program.return_value = (0, b'3:bmV3')
        with mock.patch.object(qga_zwatch.time, 'time', return_value=10):
            self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'metrics'), (3, 'new'))
        self.assertEqual(self.qga._zwatch_file_legacy_until, 0)
        self.qga.guest_file_read.assert_not_called()

    def test_old_tool_initialization_restart_and_push(self):
        self.old_tool()
        self.qga.os, self.qga.os_version = 'mswindows', '10'
        self.qga.guest_file_is_exist.return_value = True
        self.qga.guest_file_read.side_effect = [(16, 'NeedConfigVmInfo'), (3, 'cpu')]
        self.qga.guest_file_write.return_value = 2
        monitor = qga_zwatch.ZWatchMetricMonitor()
        monitor.running_vm_list = ['vm']
        with mock.patch.object(qga_zwatch, 'push_metrics_to_gateway', side_effect=lambda *args: monitor.running_vm_list.remove('vm')) as push:
            with mock.patch.object(qga_zwatch.time, 'sleep'):
                monitor.zwatch_qga_monitor_vm('vm', self.qga)
        self.qga.guest_file_write.assert_called_once_with(monitor.WIN_ZWATCH_VM_INFO_PATH, b'vm')
        self.qga.guest_exec_cmd_no_exitcode.assert_called_once_with(monitor.WIN_ZWATCH_RESTART_CMD)
        push.assert_called_once_with(monitor.PROMETHEUS_PUSHGATEWAY_URL, 'vm', 'cpu')

    def test_linux_keeps_existing_file_protocol(self):
        monitor = qga_zwatch.ZWatchMetricMonitor()
        self.qga.os, self.qga.os_version = 'linux', '7'
        self.qga.guest_file_is_exist.return_value = True
        self.qga.guest_file_read.side_effect = [(2, 'vm'), (3, 'cpu')]
        monitor.running_vm_list = ['vm']
        with mock.patch.object(qga_zwatch, 'push_metrics_to_gateway', side_effect=lambda *args: monitor.running_vm_list.remove('vm')):
            with mock.patch.object(qga_zwatch.time, 'sleep'):
                monitor.zwatch_qga_monitor_vm('vm', self.qga)
        self.assertEqual(self.qga.guest_file_read.call_count, 2)
        self.qga.guest_exec_program.assert_not_called()

    def test_windows_preserves_initialization_restart_and_push(self):
        monitor = qga_zwatch.ZWatchMetricMonitor()
        self.qga.os, self.qga.os_version = 'mswindows', '10'
        monitor.running_vm_list = ['vm']
        with mock.patch.object(qga_zwatch, 'windows_zwatch_file', side_effect=[(16, 'NeedConfigVmInfo'), (2, 'vm'), (3, 'cpu')]) as files:
            with mock.patch.object(qga_zwatch, 'push_metrics_to_gateway', side_effect=lambda *args: monitor.running_vm_list.remove('vm')) as push:
                with mock.patch.object(qga_zwatch.time, 'sleep'):
                    monitor.zwatch_qga_monitor_vm('vm', self.qga)
        self.assertEqual(files.call_args_list, [mock.call(self.qga, monitor.WIN_ZWATCH_VM_INFO_PATH),
                         mock.call(self.qga, monitor.WIN_ZWATCH_VM_INFO_PATH, 'vm'),
                         mock.call(self.qga, monitor.WIN_ZWATCH_VM_METRIC_PATH)])
        self.qga.guest_exec_cmd_no_exitcode.assert_called_once_with(monitor.WIN_ZWATCH_RESTART_CMD)
        self.qga.guest_file_is_exist.assert_not_called()
        self.qga.guest_file_read.assert_not_called()
        self.qga.guest_file_write.assert_not_called()
        push.assert_called_once()


class TestWindowsZWatchLegacyQgaErrors(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.abspath(os.path.join(here, '..', '..', '..',
                                           'zstacklib', 'zstacklib', 'utils', 'qga.py'))
        self.module = _load_module_from_path('legacy_qga_under_test', path)
        self.qga = self.module.VmQga.__new__(self.module.VmQga)
        self.qga.vm_uuid = 'isolated-unit-test'
        self.qga.domain = object()
        self.qga.supported_commands = {}
        self.qga.guest_exec_program = mock.Mock(return_value=(
            1, b'Error: unknown command "zwatch-file" for "zs-tools"\n'))
        self.calls = []
        self.description = ''
        patcher = mock.patch.object(self.module.libvirt_qemu, 'qemuAgentCommand', side_effect=self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def transport(self, domain, command, timeout, flags):
        request = json.loads(command)
        self.calls.append(request['execute'])
        if request['execute'] == 'guest-file-open':
            return json.dumps({'error': {'class': 'GenericError', 'desc': self.description}})
        raise AssertionError('unexpected QGA request: %s' % request)

    def test_real_qga_open_errors_are_not_missing_files(self):
        for reason in ['Access is denied.', 'sharing violation', 'Guest agent is not responding']:
            self.description = "failed to open file 'metrics': " + reason
            with self.assertRaises(Exception):
                qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
        self.assertEqual(self.calls, ['guest-file-open'] * 3)

    def test_real_qga_english_and_chinese_missing_files(self):
        for reason in ['No such file or directory',
                       'The system cannot find the file specified.',
                       'The system cannot find the path specified.',
                       u'系统找不到指定的文件。',
                       u'系统找不到指定的路径。']:
            self.description = u"failed to open file 'metrics': " + reason
            self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, 'metrics'), (0, None))
        self.assertEqual(self.calls, ['guest-file-open'] * 5)

    def test_real_qga_read_error_still_closes_known_handle(self):
        def read_error(domain, command, timeout, flags):
            request = json.loads(command)
            self.calls.append(request['execute'])
            if request['execute'] == 'guest-file-open':
                return json.dumps({'return': 42})
            if request['execute'] == 'guest-file-read':
                return json.dumps({'error': {'class': 'GenericError', 'desc': 'read failed'}})
            if request['execute'] == 'guest-file-close':
                self.assertEqual(request['arguments']['handle'], 42)
                return json.dumps({'return': {}})
            raise AssertionError(request)
        with mock.patch.object(self.module.libvirt_qemu, 'qemuAgentCommand', side_effect=read_error):
            with self.assertRaises(Exception):
                qga_zwatch.windows_zwatch_file(self.qga, 'metrics')
        self.assertEqual(self.calls, ['guest-file-open', 'guest-file-read', 'guest-file-close'])


@unittest.skipUnless(os.name == 'nt' and os.environ.get('ZSV_ZWATCH_NATIVE_EXE'), 'built native zs-tools required')
class TestWindowsZWatchFileNative(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.root = tempfile.mkdtemp(prefix="qga-min-'$-")
        def cleanup():
            target = os.path.realpath(self.root)
            self.assertEqual(os.path.dirname(target), os.path.realpath(tempfile.gettempdir()))
            self.assertTrue(os.path.basename(target).startswith("qga-min-'$-"))
            shutil.rmtree(target)
        self.addCleanup(cleanup)
        self.path = os.path.join(self.root, 'vm_metrics.prom')
        with open(self.path, 'wb') as f:
            f.write(b'metric 123\n')
        self.qga = mock.Mock()
        self.qga._zwatch_file_legacy_until = 0
        self.qga.guest_exec_program.side_effect = self.execute

    @staticmethod
    def execute(path, args, **kwargs):
        import subprocess
        process = subprocess.Popen([os.environ.get('ZSV_ZWATCH_NATIVE_EXE', path)] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        return process.returncode, out if process.returncode == 0 else err

    def assert_released(self):
        with open(self.path, 'ab') as f:
            f.write(b'\n')
        os.rename(self.path, self.path + '.closed')
        os.rename(self.path + '.closed', self.path)

    def test_read_and_write_with_quoted_path(self):
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, self.path), (11, 'metric 123\n'))
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, self.path, 'new value'), (9, 'new value'))
        self.assert_released()

    def test_missing_and_empty_files(self):
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, self.path + '.missing'), (0, None))
        with open(self.path, 'wb'):
            pass
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, self.path), (0, ''))
        self.assert_released()

    def test_lost_read_and_write_reply_does_not_hold_file(self):
        import subprocess
        for contents in (None, 'changed'):
            children = []
            def lose_reply(path, args, **kwargs):
                children.append(subprocess.Popen([os.environ.get('ZSV_ZWATCH_NATIVE_EXE', path)] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                raise Exception('RPC reply lost while guest process is running')
            self.qga.guest_exec_program.side_effect = lose_reply
            with self.assertRaises(Exception):
                qga_zwatch.windows_zwatch_file(self.qga, self.path, contents)
            for child in children:
                out, err = child.communicate()
                self.assertEqual(child.returncode, 0, err)
            self.assert_released()

    def test_does_not_read_partial_data_while_writer_is_open(self):
        with open(self.path, 'wb') as f:
            f.write(b'partial')
            f.flush()
            with self.assertRaises(Exception):
                qga_zwatch.windows_zwatch_file(self.qga, self.path)
            f.write(b' complete')
        self.assertEqual(qga_zwatch.windows_zwatch_file(self.qga, self.path), (16, 'partial complete'))
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
