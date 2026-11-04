import json
import os
import sys
import types

try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote

try:
    from unittest.mock import patch
except ImportError:
    from mock import patch

if sys.version_info[0] == 2:
    import imp
else:
    import importlib.util

import pytest


class _LoggerStub(object):
    def debug(self, *_args, **_kwargs):
        pass


def _load_qemu_module():
    package = types.ModuleType('zstacklib')
    package.__path__ = []
    utils = types.ModuleType('zstacklib.utils')
    utils.__path__ = []
    bash_module = types.ModuleType('zstacklib.utils.bash')
    jsonobject_module = types.ModuleType('zstacklib.utils.jsonobject')
    linux_module = types.ModuleType('zstacklib.utils.linux')
    log_module = types.ModuleType('zstacklib.utils.log')
    qemu_img_module = types.ModuleType('zstacklib.utils.qemu_img')
    shell_module = types.ModuleType('zstacklib.utils.shell')

    bash_module.bash_roe = lambda _command: (0, '', '')
    jsonobject_module.loads = json.loads
    linux_module.get_vm_pid = lambda _vm_uuid: None
    linux_module.get_process_start_time = lambda _pid: None
    linux_module.HOST_ARCH = 'x86_64'
    linux_module.shellquote = shell_quote
    log_module.get_logger = lambda _name: _LoggerStub()
    shell_module.call = lambda _command, *_args, **_kwargs: ''
    utils.qemu_img = qemu_img_module

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    module_path = os.path.join(repo_root, 'zstacklib', 'zstacklib', 'utils', 'qemu.py')
    real_exists = os.path.exists
    modules = {
        'zstacklib': package,
        'zstacklib.utils': utils,
        'zstacklib.utils.bash': bash_module,
        'zstacklib.utils.jsonobject': jsonobject_module,
        'zstacklib.utils.linux': linux_module,
        'zstacklib.utils.log': log_module,
        'zstacklib.utils.qemu_img': qemu_img_module,
        'zstacklib.utils.shell': shell_module,
        'bash': bash_module,
        'jsonobject': jsonobject_module,
        'linux': linux_module,
        'log': log_module,
        'shell': shell_module,
    }

    def bash_roe(command):
        if command.startswith('rpm '):
            return 1, '', ''
        return 0, 'QEMU emulator version 6.2.0 (qemu-kvm-6.2.0)', ''

    with patch.dict(sys.modules, modules), \
            patch.object(bash_module, 'bash_roe', side_effect=bash_roe), \
            patch.object(shell_module, 'call', return_value='6.2.0'), \
            patch('os.path.exists', side_effect=lambda path: (
                    path == '/usr/bin/qemu-system-x86_64' or real_exists(path))):
        if sys.version_info[0] == 2:
            module = imp.load_source('zstacklib.utils.qemu_bitmap_under_test', module_path)
        else:
            spec = importlib.util.spec_from_file_location('zstacklib.utils.qemu_bitmap_under_test', module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    return module


qemu = _load_qemu_module()


def test_get_rbd_data_bitmap_splits_offset_extents(monkeypatch):
    monkeypatch.setattr(qemu.linux, 'shellquote', shell_quote)
    monkeypatch.setattr(
        qemu.shell,
        'call',
        lambda _command: '[{"offset":4096,"length":8192,"exists":"true"}]',
    )

    assert qemu.get_rbd_data_bitmap('pool/image', 4096) == {4096: 4096, 8192: 4096}


def test_get_data_bitmap_does_not_require_allocation_depth(monkeypatch):
    monkeypatch.setattr(qemu, 'get_device_map', lambda _path, _option: json.dumps([
        {'start': 0, 'length': 4096, 'zero': False, 'data': True},
    ]))

    assert qemu.get_data_bitmap('cbd:scratch', 4096, False, True, '-f qcow2') == {0: 4096}


def test_merge_data_bitmaps_unions_adjacent_contained_and_transitive_extents():
    result = qemu.merge_data_bitmaps([
        {0: 10, 30: 10},
        {5: 30, 40: 5},
    ], 16)

    assert result == {0: 16, 16: 16, 32: 13}


def test_merge_data_bitmaps_keeps_disjoint_extents():
    assert qemu.merge_data_bitmaps([{0: 5}, {10: 5}], 16) == {0: 5, 10: 5}
