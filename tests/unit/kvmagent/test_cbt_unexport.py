import pytest

from kvmagent.plugins import vm_plugin
from zstacklib.utils import http
from zstacklib.utils import jsonobject


@pytest.mark.parametrize('install_path,actual_path', [
    ('ceph://pool/volume', 'rbd:pool/volume'),
    ('sharedblock://vg/volume', '/dev/vg/volume'),
])
def test_unexport_nbd_volumes_uses_export_name_and_deactivates_actual_path(
        monkeypatch, install_path, actual_path):
    plugin = vm_plugin.VmPlugin.__new__(vm_plugin.VmPlugin)
    kills = []
    deactivated = []
    monkeypatch.setattr(plugin, 'deactive_volume_if_need',
                        lambda path, _force: deactivated.append(path))
    monkeypatch.setattr(vm_plugin.qemu_nbd, 'kill_nbd_process_by_export_name',
                        lambda name: kills.append(name) or 0)

    request = {http.REQUEST_BODY: jsonobject.dumps({
        'volumes': [{'installPath': install_path, 'volumeUuid': 'volume-uuid'}]})}
    response = jsonobject.loads(plugin.unexport_nbd_volumes(request))

    assert response.success
    assert kills == ['volume-uuid']
    assert deactivated == [actual_path]
