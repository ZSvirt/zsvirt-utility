# coding=utf-8
from kvmagent.test.shareblock_testsuite.shared_block_plugin_teststub import SharedBlockPluginTestStub
from kvmagent.test.utils import sharedblock_utils,pytest_utils,storage_device_utils
from zstacklib.utils import bash, lvm, jsonobject, sanlock, linux, log
from unittest import TestCase
from zstacklib.test.utils import misc,env
import os

storage_device_utils.init_storagedevice_plugin()

PKG_NAME = __name__

# must create iSCSI stroage before run test
__ENV_SETUP__ = {
    'self': {
        'xml':'http://smb.zstack.io/mirror/ztest/xml/twoDiskVm.xml',
        'init':['bash ./createiSCSIStroage.sh']
    }
}

logger = log.get_logger(__name__)
hostUuid = "8b12f74e6a834c5fa90304b8ea54b1dd"
hostId = 24
vgUuid = "36b02490bb944233b0b01990a450ba83"
blockUuids = []

## describe: case will manage by ztest
class TestSharedBlockPlugin(TestCase, SharedBlockPluginTestStub):
    @classmethod
    def setUpClass(cls):
        pass

    @pytest_utils.ztest_decorater
    def test_sharedblock_dm_table_refresh(self):
        self_vm = env.get_vm_metadata('self')
        rsp = storage_device_utils.iscsi_login(
            self_vm.ip,"3260"
        )
        self.assertEqual(rsp.success, True, rsp.error)

        r, o = bash.bash_ro("ls /dev/disk/by-id | grep scsi-3 | awk -F '-' '{print $2}'")
        blockUuids.extend(o.strip().splitlines())

        rsp = self.connect(blockUuids[0 : 1], blockUuids, vgUuid, hostUuid, hostId, forceWipe=True)
        self.assertEqual(True, rsp.success, rsp.error)
        o = bash.bash_o("vgs")
        self.assertEqual(True, rsp.success, o)

        self.dm_table_refresh_test()

    def dm_table_refresh_test(self):
        lv_name = "dm_table_refresh"
        test_lv = "/dev/{}/{}".format(vgUuid, lv_name)
        dm_name = "{}-{}".format(vgUuid, lv_name)
        wrong_dm_name = "{}-wrong".format(dm_name)
        lvm.create_lv_from_absolute_path(test_lv, 4194304, exact_size=True)
        lvm.active_lv_with_check(test_lv, shared=True)
        try:
            original_table = bash.bash_o("dmsetup table %s" % dm_name).strip()
            segments = original_table.splitlines()
            self.assertEqual(1, len(segments), original_table)
            tokens = segments[0].split()
            self.assertEqual("linear", tokens[2], original_table)

            bash.bash_errorout("dmsetup create %s --table %s" % (
                wrong_dm_name, linux.shellquote("0 %s zero" % tokens[1])))
            wrong_rdev = os.stat("/dev/mapper/%s" % wrong_dm_name).st_rdev
            wrong_device = "%s:%s" % (os.major(wrong_rdev), os.minor(wrong_rdev))
            self.assertNotEqual(tokens[3], wrong_device)
            tokens[3] = wrong_device
            tokens[4] = "0"
            wrong_table = " ".join(tokens)

            bash.bash_errorout("dmsetup reload %s --table %s" % (dm_name, linux.shellquote(wrong_table)))
            bash.bash_errorout("dmsetup suspend %s && dmsetup resume %s" % (dm_name, dm_name))
            self.assertEqual(wrong_table, bash.bash_o("dmsetup table %s" % dm_name).strip())

            rsp = self.connect(blockUuids[0 : 1], blockUuids, vgUuid, hostUuid, hostId, forceWipe=False)
            self.assertEqual(True, rsp.success, rsp.error)
            self.assertEqual(original_table, bash.bash_o("dmsetup table %s" % dm_name).strip())
        finally:
            lvm.delete_lv(test_lv, raise_exception=False)
            bash.bash_r("dmsetup remove %s" % wrong_dm_name)
