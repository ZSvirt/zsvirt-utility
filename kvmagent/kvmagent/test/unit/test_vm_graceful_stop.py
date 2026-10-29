import unittest
try:
    from unittest import mock
except ImportError:
    import mock

from kvmagent.plugins import vm_plugin


class TestGracefulStop(unittest.TestCase):
    def setUp(self):
        self.vm = object.__new__(vm_plugin.Vm)
        self.vm.uuid = 'test-vm'
        self.vm.domain = mock.Mock()
        self.vm.domain.isPersistent.return_value = False
        self.vm.domain.isActive.return_value = False
        self.vm.domain.info.return_value = (1, 0, 0, 0, 0)
        self.vm.domain_xmlobject = mock.Mock()
        self.vm.domain_xmlobject.devices.get_child_node_as_list.return_value = []
        self.vm.wait_for_state_change = mock.Mock(return_value=True)

    def test_paused_grace_does_not_touch_domain(self):
        self.vm.domain.info.return_value = (3, 0, 0, 0, 0)
        with self.assertRaises(vm_plugin.kvmagent.KvmError):
            self.vm.stop(timeout=60)
        self.vm.domain.shutdown.assert_not_called()
        self.vm.domain.destroy.assert_not_called()
        self.vm.domain_xmlobject.devices.get_child_node_as_list.assert_not_called()

    def test_timeout_does_not_destroy_or_clean_up(self):
        self.vm.wait_for_state_change.return_value = False
        def wait_once(callback, args, **kwargs):
            return callback(args)
        with mock.patch.object(vm_plugin.linux, 'wait_callback_success', side_effect=wait_once) as wait:
            with self.assertRaises(vm_plugin.kvmagent.KvmError):
                self.vm.stop(timeout=37)
        self.assertEqual(37, wait.call_args[1]['timeout'])
        self.vm.domain.shutdown.assert_called_once_with()
        self.vm.domain.destroy.assert_not_called()
        self.vm.domain_xmlobject.devices.get_child_node_as_list.assert_not_called()

    def test_successful_grace_does_not_destroy(self):
        self.vm.domain.info.side_effect = [(1, 0, 0, 0, 0), (5, 0, 0, 0, 0)]
        def wait(callback, args, **kwargs):
            return callback(args)
        with mock.patch.object(vm_plugin.linux, 'wait_callback_success', side_effect=wait):
            self.vm.stop(timeout=60)
        self.vm.domain.shutdown.assert_called_once_with()
        self.vm.domain.destroy.assert_not_called()

    def test_shutting_down_is_not_yet_stopped(self):
        self.vm.domain.info.side_effect = [(1, 0, 0, 0, 0), (4, 0, 0, 0, 0)]
        def wait(callback, args, **kwargs):
            return callback(args)
        with mock.patch.object(vm_plugin.linux, 'wait_callback_success', side_effect=wait):
            with self.assertRaises(vm_plugin.kvmagent.KvmError):
                self.vm.stop(timeout=60)
        self.vm.domain.destroy.assert_not_called()
        self.vm.domain_xmlobject.devices.get_child_node_as_list.assert_not_called()

    def test_grace_undefine_failure_does_not_kill(self):
        self.vm.domain.isPersistent.return_value = True
        self.vm.domain.info.return_value = (5, 0, 0, 0, 0)
        self.vm.is_alive = mock.Mock(return_value=True)
        self.vm.domain.undefineFlags.side_effect = vm_plugin.libvirt.libvirtError('failed')
        self.vm.domain.undefine.side_effect = RuntimeError('failed')
        def wait(callback, args, **kwargs):
            return callback(args)
        with mock.patch.object(vm_plugin.linux, 'wait_callback_success', side_effect=wait), \
                mock.patch.object(vm_plugin.linux, 'kill_process') as kill:
            with self.assertRaises(vm_plugin.kvmagent.KvmError):
                self.vm.stop(timeout=60)
        kill.assert_not_called()
        self.vm.domain.destroy.assert_not_called()

    def test_cold_can_destroy_paused_vm(self):
        self.vm.domain.info.return_value = (3, 0, 0, 0, 0)
        def destroy():
            self.vm.domain.info.return_value = (5, 0, 0, 0, 0)
        self.vm.domain.destroy.side_effect = destroy
        def wait(callback, args, **kwargs):
            return callback(args)
        with mock.patch.object(vm_plugin.linux, 'wait_callback_success', side_effect=wait):
            self.vm.stop(strategy='cold')
        self.vm.domain.destroy.assert_called_once_with()
        self.vm.domain.shutdown.assert_not_called()

    def run_stop_request(self, strategy, failure):
        plugin = object.__new__(vm_plugin.VmPlugin)
        plugin.kill_vm = mock.Mock()
        cmd = mock.Mock(uuid='test-vm', type=strategy, timeout=120)
        vm = mock.Mock()
        vm.stop.side_effect = failure
        with mock.patch.object(vm_plugin, 'get_vm_by_uuid', return_value=vm), \
                mock.patch.object(vm_plugin.ovs, 'isVmUseOpenvSwitch', return_value=False):
            if strategy == 'grace' and failure:
                with self.assertRaises(vm_plugin.kvmagent.KvmError):
                    plugin._stop_vm(cmd)
            else:
                try:
                    plugin._stop_vm(cmd)
                except vm_plugin.kvmagent.KvmError:
                    if not failure:
                        raise
        if strategy == 'grace':
            plugin.kill_vm.assert_not_called()
            vm.check_if_vm_has_operating_system_by_memory_state.assert_not_called()
        else:
            plugin.kill_vm.assert_called_once_with('test-vm')

    def test_grace_failure_is_propagated_without_kill(self):
        self.run_stop_request('grace', vm_plugin.kvmagent.KvmError('timeout'))

    def test_grace_success_does_not_kill(self):
        self.run_stop_request('grace', None)

    def test_cold_failure_keeps_force_fallback(self):
        self.run_stop_request('cold', vm_plugin.kvmagent.KvmError('timeout'))

    def test_force_keeps_kill(self):
        self.run_stop_request('force', None)


if __name__ == '__main__':
    unittest.main()
