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

    def corrupt_data(self, vg, offset, length=512):
        fd = os.open("/dev/mapper/%s-lvmlock" % vg, os.O_RDWR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            random_data = os.urandom(length)
            os.write(fd, random_data)
            os.fsync(fd)
        finally:
            os.close(fd)

    @pytest_utils.ztest_decorater
    def test_sharedblock_metadata_auto_repair(self):
        ver = lvm.get_sanlock_patch_version()
        if not (ver and ver.isdigit()) or int(ver) < 7:
            logger.warning(
                "Skip metadata auto-repair test: "
                "sanlock patch 7 or later is required, current=%s" % ver
            )
            return

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

        self.sector_size = sanlock.get_sector_size(vgUuid)
        self.align_size = sanlock.sector_size_to_align_size(self.sector_size)
        backup = sanlock.read_lockspace_metadata_from_backup(vgUuid)
        self.assertEqual(hostId, backup.get('host_id'))
        self.assertGreater(backup.get('generation', 0), 0)
        self.assertEqual(self.sector_size, backup.get('sector_size'))
        self.lvm_repair_cmd_test()
        self.dm_table_refresh_test()

        self.lvlk_repair_test()
        self.vglk_repair_test()
        self.lockspace_repair_test()
        self.gllk_repair_test()

    def lvm_repair_cmd_test(self):
        test_lv = "/dev/{}/testlv".format(vgUuid)
        lvm.create_lv_from_absolute_path("/dev/{}/testlv".format(vgUuid), 4194304, exact_size=True)
        lvm.active_lv_with_check(test_lv, shared=True)
        lvm.active_lv(test_lv, shared=False)
        lvm.extend_lv(test_lv, 20971520, lockopts=["shupdate", "norefresh"])
        lvm.lv_rename(test_lv, "/dev/{}/testlv-new".format(vgUuid))
        test_lv = "/dev/{}/testlv-new".format(vgUuid)
        lvm.deactive_lv(test_lv)
        lvm.delete_lv(test_lv)

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

    def lvlk_repair_test(self):
        volume_uuid = misc.uuid()
        volume_path = "sharedblock://{}/{}".format(vgUuid, volume_uuid)
        abs_path = "/dev/{}/{}".format(vgUuid, volume_uuid)
        rsp = sharedblock_utils.shareblock_create_empty_volume(
            installPath=volume_path,
            volumeUuid=volume_uuid,
            size=1048576,
            hostUuid=hostUuid,
            vgUuid=vgUuid
        )
        self.assertEqual(True, rsp.success, rsp.error)

        start = int(lvm.get_lv_attr(abs_path, "lv_lockargs").get("lv_lockargs").split(":")[-1])
        # corrupt the first 50 and last 50 sectors
        for i in list(range(0, 50)) + list(range(self.align_size // self.sector_size - 50, self.align_size // self.sector_size)):
            lock_offset = start + self.sector_size * i
            self.corrupt_data(vgUuid, lock_offset, length=self.sector_size)

            rsp = sharedblock_utils.sharedblock_active_lv(
                installPath=volume_path,
                vgUuid=vgUuid,
                lockType=1
            )
            self.assertEqual(True, rsp.success, "lvUuid %s offset %s err %s" % (volume_uuid, lock_offset, rsp.error))

            self.corrupt_data(vgUuid, lock_offset, length=self.sector_size)
            rsp = sharedblock_utils.sharedblock_active_lv(
                installPath=volume_path,
                vgUuid=vgUuid,
                lockType=2
            )
            self.assertEqual(True, rsp.success, "lvUuid %s offset %s err %s" % (volume_uuid, lock_offset, rsp.error))

            self.corrupt_data(vgUuid, lock_offset, length=self.sector_size)
            rsp = sharedblock_utils.sharedblock_active_lv(
                installPath=volume_path,
                vgUuid=vgUuid,
                lockType=0
            )
            self.assertEqual(True, rsp.success, "lvUuid %s offset %s err %s" % (volume_uuid, lock_offset, rsp.error))


    def vglk_repair_test(self):
        offset = sanlock.VGLK_BEGIN * self.align_size
        for i in list(range(0, 50)) + list(range(self.align_size // self.sector_size - 50, self.align_size // self.sector_size)):
            lock_offset = offset + self.sector_size * i

            self.corrupt_data(vgUuid, lock_offset, length=self.sector_size)
            rsp = sharedblock_utils.sharedblock_ping(vgUuid)
            self.assertEqual(rsp.success, True, rsp.error)

            r, o, e = bash.bash_roe("lvcreate --wipesignatures y -y --size 4M --name test_lv %s" % vgUuid)
            self.assertEqual(True, r == 0, "lvcreate failed: %s uuid %s offset %s" % (str(o)+str(e), "VGLK", lock_offset))

            self.corrupt_data(vgUuid, lock_offset, length=self.sector_size)
            rsp = sharedblock_utils.sharedblock_ping(vgUuid)
            self.assertEqual(rsp.success, True, rsp.error)

            r, o, e = bash.bash_roe("lvremove -y /dev/%s/test_lv" % vgUuid)
            self.assertEqual(True, r == 0, "lvremove failed: %s uuid %s offset %s" % (str(o)+str(e), "VGLK", lock_offset))

    def wait_lockspace_stop(self):
        def _wait(_):
            return bash.bash_r("lvmlockctl -i -d | grep lvm_%s" % vgUuid) != 0
        self.assertTrue(linux.wait_callback_success(_wait, timeout=10, interval=0.5),
                        'lockspace did not stop')

    def lockspace_repair_test(self):
        volume_uuid = misc.uuid()
        volume_path = "sharedblock://{}/{}".format(vgUuid, volume_uuid)
        rsp = sharedblock_utils.shareblock_create_empty_volume(
            installPath=volume_path,
            volumeUuid=volume_uuid,
            size=1048576,
            hostUuid=hostUuid,
            vgUuid=vgUuid
        )
        self.assertEqual(True, rsp.success, rsp.error)
        for i in range(10):
            self.corrupt_data(vgUuid, 0, length=self.align_size)
            self.corrupt_data(vgUuid, sanlock.GLLK_BEGIN * self.align_size, length=self.align_size)
            bash.bash_o("vgchange -an {0} && vgchange --lockstop {0}".format(vgUuid))
            bash.bash_o("lvmlockctl -r {0}".format(vgUuid))
            self.wait_lockspace_stop()
            rsp = self.connect(blockUuids[0 : 1], blockUuids, vgUuid, hostUuid, hostId, forceWipe=False)
            self.assertEqual(True, rsp.success, "lockspace %s hostId %s err %s" % (vgUuid, hostId, rsp.error))

            rsp = sharedblock_utils.sharedblock_active_lv(
                installPath=volume_path,
                vgUuid=vgUuid,
                lockType=2
            )
            self.assertEqual(True, rsp.success, rsp.error)


    def gllk_repair_test(self):
        offset = sanlock.GLLK_BEGIN * self.align_size
        self.corrupt_data(vgUuid, offset, length=self.align_size)
        lvm.fix_global_lock()
        r, o = bash.bash_ro("vgs 2>&1")
        self.assertEqual(False, "global lock" in o, str(o))

        bash.bash_r("lvmlockctl -D %s" % vgUuid)
        self.corrupt_data(vgUuid, offset, length=self.align_size)
        lvm.fix_global_lock()
        r, o = bash.bash_ro("vgs 2>&1")
        self.assertEqual(False, "global lock" in o, str(o))
