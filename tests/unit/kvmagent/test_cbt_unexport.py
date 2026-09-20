import pytest

from kvmagent.plugins import vm_plugin
from zstacklib.utils import http
from zstacklib.utils import jsonobject


@pytest.mark.parametrize('existing_paths,expected_kills', [
    (['ceph://pool/volume'], ['ceph://pool/volume']),
    (['rbd:pool/volume', 'ceph://pool/volume'],
     ['rbd:pool/volume', 'ceph://pool/volume']),
])
def test_unexport_nbd_volumes_finds_each_exported_path(
        monkeypatch, existing_paths, expected_kills):
    plugin = vm_plugin.VmPlugin.__new__(vm_plugin.VmPlugin)
    kills = []
    deactivated = []
    monkeypatch.setattr(plugin, 'deactive_volume_if_need',
                        lambda path, _force: deactivated.append(path))
    monkeypatch.setattr(vm_plugin.qemu_nbd, 'find_qemu_nbd_process',
                        lambda path: 0 if path in existing_paths else 1)
    monkeypatch.setattr(vm_plugin.qemu_nbd, 'kill_nbd_process_by_flag',
                        lambda path: kills.append(path) or 0)

    request = {http.REQUEST_BODY: '{"volumes":[{"installPath":"ceph://pool/volume"}]}'}
    response = jsonobject.loads(plugin.unexport_nbd_volumes(request))

    assert response.success
    assert kills == expected_kills
    assert deactivated == ['rbd:pool/volume']
