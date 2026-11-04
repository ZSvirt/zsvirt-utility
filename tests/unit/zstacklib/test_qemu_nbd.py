import os
import sys
import types
import io

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch
import pytest

if sys.version_info[0] == 2:
    import imp
else:
    import importlib.util


def _load_qemu_nbd_module():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    module_path = os.path.join(repo_root, 'zstacklib', 'zstacklib', 'utils', 'qemu_nbd.py')
    package = types.ModuleType('zstacklib')
    package.__path__ = []
    utils = types.ModuleType('zstacklib.utils')
    utils.linux = types.ModuleType('zstacklib.utils.linux')
    utils.shell = types.ModuleType('zstacklib.utils.shell')
    with patch.dict(sys.modules, {'zstacklib': package, 'zstacklib.utils': utils}):
        if sys.version_info[0] == 2:
            module = imp.load_source('qemu_nbd_under_test', module_path)
        else:
            spec = importlib.util.spec_from_file_location('qemu_nbd_under_test', module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    return module


def test_export_uses_argv_without_shell_and_preserves_path():
    qemu_nbd = _load_qemu_nbd_module()
    path = "cbd:pool physical/pool/vol$(touch /tmp/not-executed)'quoted"

    with patch.object(qemu_nbd.subprocess, 'Popen') as popen:
        qemu_nbd.export(10888, '-b', '0.0.0.0', '-f', 'qcow2', path, '-x', 'volume-uuid')

    command = popen.call_args[0][0]
    assert command == [
        'qemu-nbd', '-p', '10888', '-b', '0.0.0.0', '-f', 'qcow2', path, '-x', 'volume-uuid',
    ]
    assert popen.call_args[1]['shell'] is False


def test_find_export_processes_matches_exact_export_name():
    qemu_nbd = _load_qemu_nbd_module()
    command_lines = {
        '101': b'\0'.join([b'qemu-nbd', b'-p', b'10800', b'/path/vol', b'-x', b'vol', b'']),
        '102': b'\0'.join([b'qemu-nbd', b'-p', b'10801', b'/path/vol2', b'-x', b'vol2', b'']),
        '103': b'\0'.join([b'qemu-nbd', b'-p', b'10802', b'/path/vol', b'-x', b'other', b'']),
    }

    def read_cmdline(path, mode):
        assert mode == 'rb'
        return io.BytesIO(command_lines[os.path.basename(os.path.dirname(path))])

    with patch.object(qemu_nbd.subprocess, 'check_output', return_value=b'101\n102\n103\n'):
        with patch.object(qemu_nbd, 'open', create=True, side_effect=read_cmdline):
            assert qemu_nbd.find_qemu_nbd_processes_by_export_name('vol') == [101]


def test_kill_export_process_only_targets_exact_name_and_waits():
    qemu_nbd = _load_qemu_nbd_module()
    with patch.object(qemu_nbd, 'find_qemu_nbd_processes_by_export_name',
                      side_effect=[[101], []]) as find:
        with patch.object(qemu_nbd.os, 'kill') as kill:
            qemu_nbd.linux.wait_callback_success = lambda callback, timeout, interval: callback(None)
            assert qemu_nbd.kill_nbd_process_by_export_name('vol') == 0
    kill.assert_called_once_with(101, qemu_nbd.signal.SIGTERM)
    assert find.call_count == 2


def test_kill_export_process_is_idempotent_when_no_process_exists():
    qemu_nbd = _load_qemu_nbd_module()
    no_process = qemu_nbd.subprocess.CalledProcessError(1, ['pgrep', '-x', 'qemu-nbd'])
    with patch.object(qemu_nbd.subprocess, 'check_output', side_effect=no_process):
        with patch.object(qemu_nbd.os, 'kill') as kill:
            assert qemu_nbd.kill_nbd_process_by_export_name('vol') == 0
    kill.assert_not_called()


def test_kill_export_process_reports_timeout():
    qemu_nbd = _load_qemu_nbd_module()
    with patch.object(qemu_nbd, 'find_qemu_nbd_processes_by_export_name',
                      return_value=[101]):
        with patch.object(qemu_nbd.os, 'kill'):
            qemu_nbd.linux.wait_callback_success = lambda callback, timeout, interval: False
            with pytest.raises(Exception, match='timeout waiting qemu-nbd export'):
                qemu_nbd.kill_nbd_process_by_export_name('vol')
