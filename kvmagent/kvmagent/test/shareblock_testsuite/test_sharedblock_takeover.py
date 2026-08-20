# coding=utf-8
from kvmagent.test.shareblock_testsuite.shared_block_plugin_teststub import SharedBlockPluginTestStub
from kvmagent.test.utils import sharedblock_utils, pytest_utils, storage_device_utils
from zstacklib.utils import bash, lvm, jsonobject
from unittest import TestCase
from zstacklib.test.utils import misc, env

storage_device_utils.init_storagedevice_plugin()

PKG_NAME = __name__

# must create iSCSI storage before run test
__ENV_SETUP__ = {
    'self': {
        'xml': 'http://smb.zstack.io/mirror/ztest/xml/twoDiskVm.xml',
        'init': ['bash ./createiSCSIStroage.sh']
    }
}

hostUuid = "8b12f74e6a834c5fa90304b8ea54b1dd"
hostId = 24
vgUuid = "36b02490bb944233b0b01990a450ba83"
takeoverTargetVgUuid = "aabb0011223344556677889900112233"


def _call_takeover(plugin, sharedBlockUuids, allSharedBlockUuids, vgUuid, hostId, hostUuid, enableLvmetad=False,
                   ioTimeout=None):
    rsp_str = plugin.takeover(misc.make_a_request({
        "sharedBlockUuids": sharedBlockUuids,
        "allSharedBlockUuids": allSharedBlockUuids,
        "vgUuid": vgUuid,
        "hostId": hostId,
        "hostUuid": hostUuid,
        "enableLvmetad": enableLvmetad,
        "ioTimeout": ioTimeout
    }))
    return jsonobject.loads(rsp_str)


def _call_vgs_info(plugin):
    rsp_str = plugin.vgs_all_info(misc.make_a_request({}))
    return jsonobject.loads(rsp_str)


class TestSharedBlockPlugin(TestCase, SharedBlockPluginTestStub):
    @classmethod
    def setUpClass(cls):
        pass

    @pytest_utils.ztest_decorater
    def test_1_vgs_info_after_connect(self):
        """vgs_info should return VG info including disk details after connect"""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        # call vgs_info
        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_vgs_info(plugin)
        self.assertEqual(True, rsp.success, rsp.error)
        self.assertTrue(rsp.groupDiskInfos is not None, "groupDiskInfos should not be None")
        self.assertTrue(rsp.groupDiskInfos.hasattr(vgUuid),
                        "vgUuid %s not found in groupDiskInfos: %s" % (vgUuid, rsp.groupDiskInfos))

    @pytest_utils.ztest_decorater
    def test_2_takeover_same_vg_uuid(self):
        """takeover with same vgUuid should succeed without rename"""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        # connect VG normally first
        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        # stop vg lock to simulate a takeover scenario (VG exists but lockspace stale)
        lvm.stop_vg_lock(vgUuid)

        # takeover with same vgUuid (no rename)
        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_takeover(plugin,
                             sharedBlockUuids=[blockUuid],
                             allSharedBlockUuids=[blockUuid],
                             vgUuid=vgUuid,
                             hostId=hostId,
                             hostUuid=hostUuid)
        self.assertEqual(True, rsp.success, rsp.error)

        # verify VG is accessible after takeover
        r, o = bash.bash_ro("vgs --nolocking -t %s" % vgUuid)
        self.assertEqual(0, r, "VG %s not accessible after takeover: %s" % (vgUuid, o))

    @pytest_utils.ztest_decorater
    def test_3_takeover_wwid_not_found(self):
        """takeover with non-existent WWIDs should fail gracefully"""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        # connect VG so lock service is running
        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        # takeover with a fake WWID that does not match any VG
        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_takeover(plugin,
                             sharedBlockUuids=["nonexistent_wwid_00000"],
                             allSharedBlockUuids=[blockUuid],
                             vgUuid=vgUuid,
                             hostId=hostId,
                             hostUuid=hostUuid)
        self.assertEqual(False, rsp.success, "takeover with fake WWID should fail")

    @pytest_utils.ztest_decorater
    def test_4_takeover_with_rename(self):
        """takeover with different vgUuid should rename the VG"""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        # connect VG with original uuid
        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        # create some LVs via create_empty_volume to simulate real volumes before takeover
        lv1 = misc.uuid()
        lv2 = misc.uuid()
        rsp = sharedblock_utils.shareblock_create_empty_volume(
            installPath="sharedblock://%s/%s" % (vgUuid, lv1),
            size=10485760,
            volumeUuid=lv1,
            hostUuid=hostUuid,
            vgUuid=vgUuid
        )
        self.assertEqual(True, rsp.success, rsp.error)
        rsp = sharedblock_utils.shareblock_create_empty_volume(
            installPath="sharedblock://%s/%s" % (vgUuid, lv2),
            size=10485760,
            volumeUuid=lv2,
            hostUuid=hostUuid,
            vgUuid=vgUuid
        )
        self.assertEqual(True, rsp.success, rsp.error)

        # stop vg lock to simulate takeover scenario
        lvm.stop_vg_lock(vgUuid)

        # takeover with a different target vgUuid => triggers rename
        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_takeover(plugin,
                             sharedBlockUuids=[blockUuid],
                             allSharedBlockUuids=[blockUuid],
                             vgUuid=takeoverTargetVgUuid,
                             hostId=hostId,
                             hostUuid=hostUuid)
        self.assertEqual(True, rsp.success, rsp.error)

        # old VG name should be gone
        r, _ = bash.bash_ro("vgs --nolocking -t %s" % vgUuid)
        self.assertNotEqual(0, r, "old VG %s should not exist after rename" % vgUuid)

        # new VG name should exist
        r, o = bash.bash_ro("vgs --nolocking -t %s" % takeoverTargetVgUuid)
        self.assertEqual(0, r, "new VG %s not accessible after takeover rename: %s" % (takeoverTargetVgUuid, o))

        # LVs should now belong to the renamed VG
        r, o = bash.bash_ro("lvs --nolocking -t --noheading -o lv_name -S vg_name=%s" % takeoverTargetVgUuid)
        self.assertEqual(0, r, "failed to list LVs under renamed VG: %s" % o)
        lv_names = o.strip().split()
        self.assertIn(lv1, lv_names, "LV %s not found under renamed VG %s, got: %s" % (lv1, takeoverTargetVgUuid, lv_names))
        self.assertIn(lv2, lv_names, "LV %s not found under renamed VG %s, got: %s" % (lv2, takeoverTargetVgUuid, lv_names))
