import os
import sys

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

if sys.version_info[0] == 2:
    import imp
else:
    import importlib.util


def _load_qemu_nbd_module():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    module_path = os.path.join(repo_root, 'zstacklib', 'zstacklib', 'utils', 'qemu_nbd.py')
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
