from unittest import TestCase

from kvmagent.plugins import shared_block_plugin
from zstacklib.utils import lvm


class TestSharedBlockConvertVolumeEncryption(TestCase):
    def test_independent_target_uses_required_chain_size(self):
        source_size = 20 * 1024 * 1024
        required_size = 460110336

        original_get_lv_size = lvm.get_lv_size
        original_get_total_required_size = shared_block_plugin.SharedBlockPlugin.get_total_required_size
        try:
            lvm.get_lv_size = lambda _: str(source_size)
            shared_block_plugin.SharedBlockPlugin.get_total_required_size = staticmethod(lambda _: required_size)

            size = shared_block_plugin.SharedBlockPlugin.get_convert_volume_encryption_lv_size(
                '/dev/vg/top', True, None)
        finally:
            lvm.get_lv_size = original_get_lv_size
            shared_block_plugin.SharedBlockPlugin.get_total_required_size = original_get_total_required_size

        self.assertEqual(required_size + shared_block_plugin.LUKS_HEADER_OVERHEAD, size)

    def test_backed_target_keeps_layer_size(self):
        source_size = 20 * 1024 * 1024

        original_get_lv_size = lvm.get_lv_size
        original_get_total_required_size = shared_block_plugin.SharedBlockPlugin.get_total_required_size
        calls = []
        try:
            lvm.get_lv_size = lambda _: str(source_size)
            shared_block_plugin.SharedBlockPlugin.get_total_required_size = staticmethod(lambda path: calls.append(path))

            size = shared_block_plugin.SharedBlockPlugin.get_convert_volume_encryption_lv_size(
                '/dev/vg/top', True, '/dev/vg/backing')
        finally:
            lvm.get_lv_size = original_get_lv_size
            shared_block_plugin.SharedBlockPlugin.get_total_required_size = original_get_total_required_size

        self.assertFalse(calls)
        self.assertEqual(source_size + shared_block_plugin.LUKS_HEADER_OVERHEAD, size)
