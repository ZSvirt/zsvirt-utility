import mock

from kvmagent.plugins import vm_plugin
from unittest import TestCase


class TestVmStopStrategy(TestCase):
    def test_grace_stop_without_detected_guest_response_completes_cold_stop(self):
        cmd = vm_plugin.StopVmCmd()
        cmd.uuid = "test-vm"
        cmd.type = "grace"
        cmd.timeout = 120
        cmd.forceStopIfNoOperatingSystemDetected = True

        vm = vm_plugin.Vm()
        vm.uuid = cmd.uuid
        vm.state = vm_plugin.Vm.VM_STATE_RUNNING
        vm.domain = mock.Mock()
        vm.domain.memoryStats.return_value = {"last_update": 0}
        vm.domain.isPersistent.return_value = True
        vm.domain.isActive.return_value = False
        vm.domain_xmlobject = mock.Mock()
        vm.domain_xmlobject.devices.get_child_node_as_list.return_value = []
        vm.wait_for_state_change = mock.Mock(return_value=True)

        plugin = vm_plugin.VmPlugin.__new__(vm_plugin.VmPlugin)

        with mock.patch.object(vm_plugin, "get_vm_by_uuid", return_value=vm), \
                mock.patch.object(vm_plugin.ovs, "isVmUseOpenvSwitch", return_value=False), \
                mock.patch.object(vm_plugin.linux, "wait_callback_success",
                                  side_effect=lambda callback, args, **kwargs: callback(args)), \
                mock.patch.object(vm_plugin.linux, "find_process_by_cmdline", return_value=123), \
                mock.patch.object(vm_plugin.linux, "kill_process") as kill_process, \
                mock.patch.object(vm_plugin.bash, "bash_o", return_value=""), \
                mock.patch.object(vm_plugin.logger, "info") as info:
            plugin._stop_vm(cmd)

        info.assert_any_call("vm has no operating system. stop it use 'cold' mode")
        vm.domain.shutdown.assert_not_called()
        vm.domain.destroy.assert_called_once_with()
        vm.domain.undefineFlags.assert_called_once()
        kill_process.assert_not_called()
