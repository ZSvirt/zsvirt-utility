# -*- coding: utf-8 -*-
"""Unit tests for VmQga.guest_exec_program helpers.

Compatible with Python 2 and Python 3 unittest discovery.
"""
from __future__ import absolute_import

import os
import sys
import types
import unittest
import warnings

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


qga_mod = None
VmQga = None
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


def _load_qga_module():
    sys.modules['libvirt'] = mock.MagicMock()
    sys.modules['libvirt_qemu'] = mock.MagicMock()

    # qga.py uses bare `import log`
    log_mod = _ensure_module('log')
    if not hasattr(log_mod, 'get_logger'):
        log_mod.get_logger = mock.Mock(return_value=mock.Mock())

    here = os.path.dirname(os.path.abspath(__file__))
    qga_path = os.path.abspath(os.path.join(here, '..', 'utils', 'qga.py'))
    # Flat module name avoids Parent module RuntimeWarning and package pollution.
    return _load_module_from_path('qga_under_test', qga_path)


def setUpModule():
    global qga_mod, VmQga, _modules_before
    _modules_before = sys.modules.copy()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        qga_mod = _load_qga_module()
        parent_warns = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and 'Parent module' in str(w.message)
        ]
        if parent_warns:
            raise AssertionError(
                'unexpected Parent module RuntimeWarning during qga load: %s'
                % parent_warns[0].message
            )
    VmQga = qga_mod.VmQga


def tearDownModule():
    global qga_mod, VmQga, _modules_before
    if _modules_before is not None:
        _restore_sys_modules(_modules_before)
        _modules_before = None
    qga_mod = None
    VmQga = None


def _make_qga():
    obj = VmQga.__new__(VmQga)
    obj.vm_uuid = 'test-vm'
    obj.os = 'mswindows'
    return obj


class TestGuestExecProgram(unittest.TestCase):
    def setUp(self):
        self.qga = _make_qga()

    def test_guest_exec_program_success(self):
        self.qga.guest_exec = mock.Mock(return_value={'pid': 7})
        self.qga.guest_exec_status = mock.Mock(
            return_value={'exited': True, 'exitcode': 0, 'out-data': b'hello'}
        )
        with mock.patch.object(qga_mod.time, 'sleep', mock.Mock()):
            with mock.patch.object(
                qga_mod,
                'decode_with_fallback',
                side_effect=lambda x: x.decode('utf-8') if isinstance(x, bytes) else x,
            ):
                code, data = self.qga.guest_exec_program('C:\\a.exe', ['nic-info'], wait=0, retry=1)

        self.assertEqual(code, 0)
        self.assertEqual(data, 'hello')
        self.qga.guest_exec.assert_called_once()

    def test_guest_exec_program_no_exitcode_nonzero_raises(self):
        self.qga.guest_exec = mock.Mock(return_value={'pid': 7})
        self.qga.guest_exec_status = mock.Mock(
            return_value={'exited': True, 'exitcode': 2, 'err-data': b'fail'}
        )
        with mock.patch.object(qga_mod.time, 'sleep', mock.Mock()):
            with mock.patch.object(
                qga_mod,
                'decode_with_fallback',
                side_effect=lambda x: x.decode('utf-8') if isinstance(x, bytes) else x,
            ):
                with self.assertRaises(Exception) as ctx:
                    self.qga.guest_exec_program_no_exitcode('C:\\a.exe', ['nic-info'])
        self.assertIn('exitcode', str(ctx.exception))

    def test_guest_exec_program_timeout(self):
        self.qga.guest_exec = mock.Mock(return_value={'pid': 7})
        self.qga.guest_exec_status = mock.Mock(return_value={'exited': False})

        with mock.patch.object(qga_mod.time, 'sleep', mock.Mock()):
            with self.assertRaises(Exception) as ctx:
                self.qga.guest_exec_program('C:\\a.exe', ['nic-info'], wait=0, retry=2)
        self.assertIn('timeout', str(ctx.exception))


class TestZsToolsRebaseDiagnostics(unittest.TestCase):
    def setUp(self):
        self.qga = _make_qga()
        self.qga.ZS_TOOLS_PATN_WIN = r'C:\zs-tools.exe'
        self.qga.guest_exec = mock.Mock(return_value={'pid': 42})
        self.qga.guest_exec_status_with_timeout = mock.Mock(return_value={
            'exited': True, 'exitcode': 0, 'out-data': b'hello\r\n', 'err-data': b'warning'})
        self.qga._try_read_zs_tools_log_tail = mock.Mock(return_value='known log tail')
        self.now = 100.0
        self.patches = [mock.patch.object(qga_mod.log, 'mask_sensitive_field', side_effect=lambda obj, value: value, create=True),
                        mock.patch.object(qga_mod.time, 'time', side_effect=lambda: self.now),
                        mock.patch.object(qga_mod.time, 'sleep', side_effect=self.sleep),
                        mock.patch.object(qga_mod, 'decode_with_fallback', side_effect=lambda x: x.decode('utf-8') if isinstance(x, bytes) else x)]
        for patch in self.patches: patch.start()

    def sleep(self, seconds): self.now += seconds

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()

    def run_host(self):
        return self.qga.guest_exec_zs_tools('host', '{}', wait=1, retry=3)

    def test_target_stdout_stderr_labels_survive_rebase(self):
        code, output = self.run_host()
        self.assertEqual(0, code)
        self.assertEqual('stdout: hello\n\nstderr: warning', output)
        self.qga._try_read_zs_tools_log_tail.assert_not_called()

    def test_target_nonzero_exit_keeps_pid_and_log_tail(self):
        self.qga.guest_exec_status_with_timeout.return_value['exitcode'] = 2
        code, output = self.run_host()
        self.assertEqual(2, code)
        self.assertIn('pid 42 exitcode 2', output)
        self.assertIn('stderr: warning', output)
        self.assertIn('zs-tools.log tail:\nknown log tail', output)

    def test_target_missing_exitcode_is_failure_with_log_tail(self):
        del self.qga.guest_exec_status_with_timeout.return_value['exitcode']
        code, output = self.run_host()
        self.assertEqual(1, code)
        self.assertIn('exited without exitcode', output)
        self.assertIn('known log tail', output)

    def test_task_bound_net_result_keeps_target_diagnostics_with_deadline(self):
        self.qga._net_task_control = mock.Mock(return_value={
            'phase': 'finished', 'pid': 43, 'exitcode': 2, 'out-data': b'net output', 'err-data': b'net error'})
        code, output = self.qga.guest_exec_zs_tools('net', '{}', wait=1, retry=3)
        self.assertEqual(2, code)
        self.assertIn('stdout: net output', output)
        self.assertIn('stderr: net error', output)
        self.assertIn('known log tail', output)
        self.qga._try_read_zs_tools_log_tail.assert_called_once_with(deadline=103.0)

    def real_log_tail(self, budget):
        del self.qga._try_read_zs_tools_log_tail
        calls = []
        def transport(command, args, timeout):
            calls.append((command, timeout, self.now))
            self.now += timeout
            if command == 'guest-file-open': return 7
            if command == 'guest-file-seek': return {'position': 100}
            if command == 'guest-file-read': return {'buf-b64': b'log detail'}
            self.assertEqual('guest-file-close', command)
        self.qga.call_qga_command = mock.Mock(side_effect=transport)
        result = self.qga._try_read_zs_tools_log_tail(deadline=100+budget)
        self.assertLessEqual(self.now, 100+budget)
        self.assertTrue(all(timeout == 1 for _, timeout, _ in calls))
        self.assertEqual('guest-file-close', calls[-1][0])
        return result, calls

    def test_net_log_reads_respect_budget_and_close_handle(self):
        result, calls = self.real_log_tail(6)
        self.assertEqual('log detail', result)
        self.assertEqual(5, len(calls))

    def test_short_log_budget_skips_reads_but_reserves_close(self):
        result, calls = self.real_log_tail(3)
        self.assertEqual('', result)
        self.assertEqual(['guest-file-open', 'guest-file-close'], [c[0] for c in calls])


if __name__ == '__main__':
    unittest.main()
