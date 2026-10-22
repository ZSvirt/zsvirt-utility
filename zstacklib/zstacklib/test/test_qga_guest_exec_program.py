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


if __name__ == '__main__':
    unittest.main()
