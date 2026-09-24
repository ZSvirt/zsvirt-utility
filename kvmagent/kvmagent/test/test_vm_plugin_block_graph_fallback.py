import unittest

from kvmagent.plugins import vm_plugin


class TestVmPluginBlockGraphFallback(unittest.TestCase):
    def setUp(self):
        self._orig_execute_qmp_command = vm_plugin.qmp.execute_qmp_command

    def tearDown(self):
        vm_plugin.qmp.execute_qmp_command = self._orig_execute_qmp_command

    @staticmethod
    def _block_graph():
        return {
            "nodes": [
                {"id": 1, "name": "drive-node0", "type": "block-driver"},
                {"id": 2, "name": "root-node0", "type": "block-driver"},
                {"id": 3, "name": "", "type": "block-backend"},
            ],
            "edges": [
                {"parent": 2, "child": 1},
                {"parent": 3, "child": 2},
            ],
        }

    @staticmethod
    def _query_block_result():
        return [{
            "device": "drive-virtio-disk0",
            "inserted": {
                "node-name": "drive-node0",
                "file": "/var/lib/zstack/volumes/volume-vol-qemu.qcow2",
            },
        }]

    def test_query_block_match_is_used_when_block_graph_unavailable(self):
        calls = []

        def fake_execute_qmp_command(vm_uuid, command,
                                     raise_exception=False, **kwargs):
            calls.append(command)
            if command == "query-block":
                return [{
                    "device": "drive-virtio-disk0",
                    "inserted": {
                        "node-name": "drive-node0",
                        "file": (
                            "/var/lib/zstack/volumes/"
                            "volume-vol-old-qemu.qcow2")
                    }
                }]
            if command == "x-debug-query-block-graph":
                return None
            return None

        vm_plugin.qmp.execute_qmp_command = fake_execute_qmp_command

        node_name, device_name = vm_plugin.get_mirror_device_for_volume_uuid(
            "vm-qemu-old", "vol-old-qemu")

        self.assertEqual("drive-node0", node_name)
        self.assertEqual("drive-virtio-disk0", device_name)
        self.assertEqual(
            ["query-block", "x-debug-query-block-graph"], calls)

    def test_transient_block_graph_failure_is_retried_for_same_vm(self):
        graph_results = [None, self._block_graph()]

        def fake_execute_qmp_command(unused_vm_uuid, command,
                                     raise_exception=False, **kwargs):
            if command == "query-block":
                return self._query_block_result()
            if command == "x-debug-query-block-graph":
                return graph_results.pop(0)
            return None

        vm_plugin.qmp.execute_qmp_command = fake_execute_qmp_command

        first = vm_plugin.get_mirror_device_for_volume_uuid(
            "vm-transient", "vol-qemu")
        second = vm_plugin.get_mirror_device_for_volume_uuid(
            "vm-transient", "vol-qemu")

        self.assertEqual(
            ("drive-node0", "drive-virtio-disk0"), first)
        self.assertEqual(
            ("root-node0", "drive-virtio-disk0"), second)
        self.assertEqual([], graph_results)

    def test_supported_block_graph_is_queried_once_per_resolution(self):
        graph_calls = []

        def fake_execute_qmp_command(unused_vm_uuid, command,
                                     raise_exception=False, **kwargs):
            if command == "query-block":
                return self._query_block_result()
            if command == "x-debug-query-block-graph":
                graph_calls.append(command)
                return self._block_graph()
            return None

        vm_plugin.qmp.execute_qmp_command = fake_execute_qmp_command

        resolved = vm_plugin.get_mirror_device_for_volume_uuid(
            "vm-supported", "vol-qemu")

        self.assertEqual(
            ("root-node0", "drive-virtio-disk0"), resolved)
        self.assertEqual(
            ["x-debug-query-block-graph"], graph_calls)


if __name__ == "__main__":
    unittest.main()
