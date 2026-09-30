import unittest

from zstacklib.utils import bash
from zstacklib.utils import jsonobject
from zstacklib.utils import lvm


class TestGetAllVgs(unittest.TestCase):

    VG_UUID = "501cda86a0864352a87845dc00db52fd"
    SOURCE_WWID = "360014054b8c1aae11f94c4ebe97d320e"
    CLONE_WWID = "360014054e3fc17d18f348aea8f2e3716"

    def _disk(self, wwid, dev_name):
        disk = lvm.SharedBlockCandidateStruct()
        disk.wwid = wwid
        disk.dev_name = dev_name
        return disk

    def test_same_vg_uuid_on_two_luns_is_reported_as_two_physical_records(self):
        """Discovery reports physical VG/LUN records and does not decide management."""
        source = self._disk(self.SOURCE_WWID, "sdf")
        block_copy = self._disk(self.CLONE_WWID, "sdb")
        visible_vgs_output = "%s 1 /dev/sdf %s::host::timestamp::hostname\n" % (
            self.VG_UUID, lvm.COMMON_TAG)
        metadata = '''
  metadata text at 3662848 crc 0xfdd0240f # vgname 501cda86a0864352a87845dc00db52fd seqno 154
  ---
501cda86a0864352a87845dc00db52fd {
id = "nPfH2X-viqI-pJmd-AiCj-6T1V-1NI0-1tmrMZ"
tags = ["zs::sharedblock::init::host::timestamp::hostname"]

physical_volumes {
pv0 {
id = "oqJ4y7-VcSI-BsOQ-yXUh-mFZT-U9l3-4WXUHW"
device = "/dev/sdb"
}
}

logical_volumes {
}
}
'''

        commands = []

        def fake_bash_roe(command):
            commands.append(command)
            if "pvck" in command:
                self.assertIn("/dev/sdb", command)
                self.assertNotIn("/dev/sdf", command)
                return 0, metadata, ""
            if "vgs" in command:
                return 0, visible_vgs_output, ""
            self.fail("unexpected command: %s" % command)

        original_get_block_devices = lvm.get_block_devices
        original_bash_roe = bash.bash_roe
        try:
            lvm.get_block_devices = lambda: [source, block_copy]
            bash.bash_roe = fake_bash_roe

            result = lvm.get_all_vgs(lvm.COMMON_TAG)

            self.assertEqual(2, len(result))
            self.assertEqual([self.VG_UUID, self.VG_UUID],
                             [record["vgUuid"] for record in result])
            self.assertEqual([self.SOURCE_WWID, self.CLONE_WWID],
                             sorted(record["disk"]["wwid"] for record in result))
            self.assertEqual([1, 1], [record["diskCount"] for record in result])
            self.assertEqual(1, len([command for command in commands if "pvck" in command]))
        finally:
            lvm.get_block_devices = original_get_block_devices
            bash.bash_roe = original_bash_roe

    def test_physical_record_keeps_vg_pv_count(self):
        source = self._disk(self.SOURCE_WWID, "sdf")
        visible_vgs_output = "%s 2 /dev/sdf %s::host::timestamp::hostname\n" % (
            self.VG_UUID, lvm.COMMON_TAG)
        commands = []
        metadata = '''
  metadata text at 3662848 crc 0xfdd0240f # vgname 501cda86a0864352a87845dc00db52fd seqno 154
  ---
501cda86a0864352a87845dc00db52fd {
tags = ["zs::sharedblock::init::host::timestamp::hostname"]

physical_volumes {
pv0 {
id = "oqJ4y7-VcSI-BsOQ-yXUh-mFZT-U9l3-4WXUHW"
}
pv1 {
id = "pRJ4y7-VcSI-BsOQ-yXUh-mFZT-U9l3-4WXUHW"
}
}

logical_volumes {
}
}
'''

        def fake_bash_roe(command):
            commands.append(command)
            if "pvck" in command:
                return 0, metadata, ""
            if "vgs" in command:
                return 0, visible_vgs_output, ""
            self.fail("unexpected command: %s" % command)

        original_get_block_devices = lvm.get_block_devices
        original_bash_roe = bash.bash_roe
        try:
            lvm.get_block_devices = lambda: [source]
            bash.bash_roe = fake_bash_roe

            result = lvm.get_all_vgs(lvm.COMMON_TAG)

            self.assertEqual(1, len(result))
            self.assertEqual(self.VG_UUID, result[0]["vgUuid"])
            self.assertEqual(2, result[0]["diskCount"])
            self.assertEqual(self.SOURCE_WWID, result[0]["disk"]["wwid"])
            self.assertFalse(any("pvck" in command for command in commands))
        finally:
            lvm.get_block_devices = original_get_block_devices
            bash.bash_roe = original_bash_roe

    def test_physical_records_are_json_serializable_for_agent_response(self):
        disk = self._disk(self.CLONE_WWID, "sdb")
        records = lvm._to_vg_disk_infos({
            self.VG_UUID: {"disks": [disk], "diskCount": 1},
        })

        class Response(object):
            pass

        rsp = Response()
        rsp.vgDiskInfos = records
        decoded = jsonobject.loads(jsonobject.dumps(rsp))

        self.assertEqual(self.VG_UUID, decoded.vgDiskInfos[0].vgUuid)
        self.assertEqual(self.CLONE_WWID, decoded.vgDiskInfos[0].disk.wwid)

if __name__ == "__main__":
    unittest.main()
