import os
import sys
from contextlib import contextmanager

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

if sys.version_info[0] == 2:
    import imp
else:
    import importlib.util

import pytest


def _load_imagestore_module():
    bash_module = sys.modules.get('zstacklib.utils.bash')
    if bash_module is not None and not hasattr(bash_module, 'bash_progress_1'):
        bash_module.bash_progress_1 = lambda *args, **kwargs: (0, '', None)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    module_path = os.path.join(repo_root, 'kvmagent', 'kvmagent', 'plugins', 'imagestore.py')
    if sys.version_info[0] == 2:
        module = imp.load_source('imagestore_under_test', module_path)
    else:
        spec = importlib.util.spec_from_file_location('imagestore_under_test', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    if not hasattr(module, 'linux'):
        module.linux = sys.modules['zstacklib.utils.linux']
    return module


imagestore = _load_imagestore_module()
ImageStoreClient = imagestore.ImageStoreClient


@contextmanager
def no_libvirt_error():
    yield


TEST_CBD_PATH = 'cbd:pool_physical/pool/volume'
TEST_CONFIGURED_CBD_PATH = TEST_CBD_PATH + '_zbs_:/etc/zbs/client.conf'


@pytest.mark.parametrize(
    'device_type, lookup_method, unused_lookup_method',
    [('cbd', '_get_target_disk_by_path', '_get_target_disk'),
     ('iscsi', '_get_target_disk', '_get_target_disk_by_path')],
)
def test_cbt_backup_passes_storage_target_unchanged(
        monkeypatch, tmpdir, device_type, lookup_method, unused_lookup_method):
    client = ImageStoreClient()
    disk = MagicMock(type_='file')
    disk.alias.name_ = 'virtio-disk0'
    vm = MagicMock(uuid='vm-uuid')
    vm._get_target_disk_by_path.return_value = (disk, None)
    vm._get_target_disk.return_value = (disk, None)
    volume_info = MagicMock(target=TEST_CBD_PATH)
    volume_info.volume = MagicMock(installPath=TEST_CBD_PATH, deviceType=device_type)
    result_file = tmpdir.join('cbt-result.json')
    commands = []

    monkeypatch.setattr(imagestore.linux, 'create_temp_file', lambda: str(result_file))
    monkeypatch.setattr(imagestore.linux, 'rm_file_force', lambda _path: None)
    monkeypatch.setattr(imagestore.linux, 'ShowLibvirtErrorOnException', lambda _vm: no_libvirt_error())

    shell_cmd = MagicMock()
    shell_cmd.return_code = 0

    def call(_exception):
        result_file.write(
            '[{"device":"drive-virtio-disk0","mode":"full",'
            '"scratchNodeName":"scratch","nbdPort":"10888","bitmap":"bitmap"}]'
        )

    shell_cmd.side_effect = call
    monkeypatch.setattr(
        imagestore.shell, 'ShellCmd',
        lambda command: commands.append(command) or shell_cmd,
    )

    result = client.cbt_backup_volume(vm, [volume_info], '', '10888-10888')

    assert len(result) == 1
    assert TEST_CBD_PATH in commands[0]
    assert TEST_CONFIGURED_CBD_PATH not in commands[0]
    assert volume_info.target == TEST_CBD_PATH
    assert volume_info.volume.installPath == TEST_CBD_PATH
    assert getattr(vm, lookup_method).call_count == 2
    assert getattr(vm, unused_lookup_method).call_count == 0


def test_cbt_backup_preserves_partial_state_on_command_failure(monkeypatch, tmpdir):
    client = ImageStoreClient()
    disk = MagicMock(type_='file')
    disk.alias.name_ = 'virtio-disk0'
    vm = MagicMock(uuid='vm-uuid')
    vm._get_target_disk_by_path.return_value = (disk, None)
    volume_info = MagicMock(target=TEST_CBD_PATH)
    volume_info.volume = MagicMock(installPath=TEST_CBD_PATH, deviceType='cbd')
    result_file = tmpdir.join('cbt-failed-result.json')
    removed_files = []

    monkeypatch.setattr(imagestore.linux, 'create_temp_file', lambda: str(result_file))
    monkeypatch.setattr(imagestore.linux, 'rm_file_force', removed_files.append)
    monkeypatch.setattr(imagestore.linux, 'ShowLibvirtErrorOnException', lambda _vm: no_libvirt_error())

    shell_cmd = MagicMock()
    shell_cmd.return_code = 1
    shell_cmd.raise_error.side_effect = RuntimeError('cbtbak failed')

    def fail(_exception):
        result_file.write(
            '[{"device":"drive-virtio-disk0","mode":"full",'
            '"scratchNodeName":"scratch","nbdPort":"10888","bitmap":"new-bitmap"}]'
        )

    shell_cmd.side_effect = fail
    monkeypatch.setattr(imagestore.shell, 'ShellCmd', lambda _command: shell_cmd)

    with pytest.raises(RuntimeError, match='cbtbak failed'):
        client.cbt_backup_volume(vm, [volume_info], 'previous-bitmap', '10888-10888')

    assert volume_info.scratchNodeName == 'scratch'
    assert volume_info.bitmapName == 'new-bitmap'
    assert removed_files == [str(result_file)]


def test_cbt_backup_rejects_empty_success_output(monkeypatch, tmpdir):
    client = ImageStoreClient()
    disk = MagicMock(type_='file')
    disk.alias.name_ = 'virtio-disk0'
    vm = MagicMock(uuid='vm-uuid')
    vm._get_target_disk_by_path.return_value = (disk, None)
    volume_info = MagicMock(target=TEST_CBD_PATH)
    volume_info.volume = MagicMock(installPath=TEST_CBD_PATH, deviceType='cbd')
    result_file = tmpdir.join('cbt-empty-result.json')
    result_file.ensure()

    monkeypatch.setattr(imagestore.linux, 'create_temp_file', lambda: str(result_file))
    monkeypatch.setattr(imagestore.linux, 'rm_file_force', lambda _path: None)
    monkeypatch.setattr(imagestore.linux, 'ShowLibvirtErrorOnException', lambda _vm: no_libvirt_error())

    shell_cmd = MagicMock(return_code=0)
    monkeypatch.setattr(imagestore.shell, 'ShellCmd', lambda _command: shell_cmd)

    with pytest.raises(Exception, match='cbtbak returned empty result'):
        client.cbt_backup_volume(vm, [volume_info], '', '10888-10888')


def test_stop_cbt_backup_passes_storage_target_unchanged(monkeypatch):
    client = ImageStoreClient()
    record = MagicMock(
        scratchNodeName='scratch', target=TEST_CBD_PATH,
        lastBitmapName='previous-bitmap', bitmapName='new-bitmap', mode='full',
    )
    commands = []

    monkeypatch.setattr(imagestore.linux, 'ShowLibvirtErrorOnException', lambda _vm: no_libvirt_error())
    monkeypatch.setattr(imagestore.shell, 'call', lambda command: commands.append(command) or '')

    client.stop_vm_cbt_backup_jobs('vm-uuid', [record])

    assert '-volumes "scratch,%s;"' % TEST_CBD_PATH in commands[0]
    assert ',full;' not in commands[0]
    assert TEST_CONFIGURED_CBD_PATH not in commands[0]
    assert '-rollback' not in commands[0]


def test_rollback_cbt_backup_carries_previous_and_new_bitmap(monkeypatch):
    client = ImageStoreClient()
    records = [
        MagicMock(
            scratchNodeName='scratch-1', target=TEST_CBD_PATH,
            lastBitmapName='previous-bitmap', bitmapName='new-bitmap', mode='incremental',
        ),
        MagicMock(
            scratchNodeName='scratch-2', target=TEST_CBD_PATH + '-2',
            lastBitmapName='previous-bitmap', bitmapName='new-bitmap', mode='full',
        ),
        MagicMock(
            scratchNodeName=None, target=TEST_CBD_PATH + '-unprocessed',
            lastBitmapName='previous-bitmap', bitmapName=None, mode=None,
        ),
    ]
    commands = []

    monkeypatch.setattr(imagestore.linux, 'ShowLibvirtErrorOnException', lambda _vm: no_libvirt_error())
    monkeypatch.setattr(imagestore.shell, 'call', lambda command: commands.append(command) or '')

    client.rollback_vm_cbt_backup_jobs('vm-uuid', records)

    assert '-rollback=true' in commands[0]
    assert '-bitmap "previous-bitmap"' in commands[0]
    assert '-newbitmap "new-bitmap"' in commands[0]
    assert TEST_CBD_PATH in commands[0]
    assert TEST_CONFIGURED_CBD_PATH not in commands[0]
    assert 'scratch-1' in commands[0]
    assert 'scratch-2' in commands[0]
    assert 'unprocessed' not in commands[0]
    assert ',incremental;' in commands[0]
    assert ',full;' in commands[0]


def test_rollback_cbt_backup_rejects_missing_new_bitmap():
    records = [MagicMock(
        scratchNodeName='scratch', target=TEST_CBD_PATH,
        lastBitmapName='previous-bitmap', bitmapName=None, mode='incremental',
    )]
    with pytest.raises(ValueError, match='missing new bitmap'):
        ImageStoreClient().rollback_vm_cbt_backup_jobs('vm-uuid', records)


def test_rollback_cbt_backup_skips_unprocessed_volume(monkeypatch):
    commands = []
    monkeypatch.setattr(imagestore.shell, 'call', lambda command: commands.append(command) or '')
    record = MagicMock(
        scratchNodeName=None, target=TEST_CBD_PATH,
        lastBitmapName='previous-bitmap', bitmapName=None, mode=None,
    )

    ImageStoreClient().rollback_vm_cbt_backup_jobs('vm-uuid', [record])

    assert commands == []
