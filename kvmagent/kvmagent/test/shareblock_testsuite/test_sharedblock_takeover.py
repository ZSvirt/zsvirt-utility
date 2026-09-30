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
        """vgs/all reports physical records while vgs/managed keeps the LVM view"""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_vgs_info(plugin)
        self.assertEqual(True, rsp.success, rsp.error)
        self.assertTrue(rsp.vgDiskInfos is not None, "vgDiskInfos should not be None")
        matches = [info for info in rsp.vgDiskInfos
                   if info.vgUuid == vgUuid and info.disk.wwid == blockUuid]
        self.assertEqual(1, len(matches),
                         "physical VG/LUN record not found in vgs/all: %s" % rsp.vgDiskInfos)

        rsp = jsonobject.loads(plugin.vgs_managed_info(misc.make_a_request({})))
        self.assertEqual(True, rsp.success, rsp.error)
        self.assertTrue(rsp.groupDiskInfos.hasattr(vgUuid),
                        "vgUuid %s not found in vgs/managed: %s" % (vgUuid, rsp.groupDiskInfos))

    @pytest_utils.ztest_decorater
    def test_2_takeover_same_vg_uuid_rejects_duplicate_device(self):
        """The self-exported iSCSI LUN is not a copy; reject takeover safely."""
        iscsi_server = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(iscsi_server.ip, "3260")
        self.assertEqual(True, rsp.success, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi|awk -F '-' '{print $2}'")
        blockUuid = o.strip().replace(' ', '').replace('\n', '').replace('\r', '')

        # The test target exports this VM's first data disk back to itself.
        # Connect through iSCSI, then prove the original disk is also visible.
        rsp = self.connect([blockUuid], [blockUuid], vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)

        plugin = sharedblock_utils.get_sharedblock_plugin()
        rsp = _call_vgs_info(plugin)
        self.assertEqual(True, rsp.success, rsp.error)
        wwids = set(info.disk.wwid for info in rsp.vgDiskInfos if info.vgUuid == vgUuid)
        self.assertIn(blockUuid, wwids)
        self.assertGreater(len(wwids), 1, "fixture must expose the original disk and its iSCSI alias")
        original_lvm_uuid = lvm.get_vg_lvm_uuid(vgUuid)
        self.assertTrue(original_lvm_uuid)

        # This must fail before vgimportclone or any VG/PV identity rewrite.
        rsp = _call_takeover(plugin,
                             sharedBlockUuids=[blockUuid],
                             allSharedBlockUuids=[blockUuid],
                             vgUuid=vgUuid,
                             hostId=hostId,
                             hostUuid=hostUuid)
        self.assertEqual(False, rsp.success, "takeover must reject a duplicate VG name")
        self.assertIn("already used by another device set", rsp.error)

        self.assertEqual(original_lvm_uuid, lvm.get_vg_lvm_uuid(vgUuid))
        r, o = bash.bash_ro("vgs --nolocking -t %s" % vgUuid)
        self.assertEqual(0, r, "original VG %s is no longer accessible: %s" % (vgUuid, o))

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
