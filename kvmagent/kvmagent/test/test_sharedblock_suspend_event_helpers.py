"""Run the production auto-extension event handler without libvirt services."""
import unittest
from test_vm_persistent_definition_helpers import load_function, Mock


class DiskErrors(dict):
    def viewitems(self):
        return self.items()


class SharedBlockSuspendEventTest(unittest.TestCase):
    def setUp(self):
        self.dom = Mock()
        self.dom.name.return_value = 'vm'
        self.dom.diskErrors.return_value = DiskErrors(vda=1)
        self.vm = Mock(domain=self.dom, state='paused')
        disk = Mock()
        disk.target.dev_ = 'vda'
        self.vm.domain_xmlobject.devices.get_child_node_as_list.return_value = [disk]
        self.lookup = Mock(return_value=self.vm)
        self.extend = Mock(return_value=True)
        self.touch = Mock()
        self.clock = Mock()
        self.events = Mock()
        self.events.suspend_event_to_string.return_value = 'VIR_DOMAIN_EVENT_SUSPENDED_IOERROR'
        self.handler = load_function('handle_event', dict(
            syslog=Mock(), logger=Mock(), LibvirtEventManager=self.events, detail=0,
            get_vm_by_uuid_no_retry=self.lookup, Vm=Mock(VM_STATE_PAUSED='paused'),
            VmPlugin=Mock(get_source_file_by_disk=Mock(return_value='/dev/vg/lv')),
            libvirt=Mock(VIR_DOMAIN_DISK_ERROR_NO_SPACE=1), lvm=Mock(lv_exists=Mock(return_value=True)),
            extend_lv=self.extend, touchQmpSocketWhenExists=self.touch, time=self.clock), '_extend_sharedblock')

    def test_manual_pause_ignores_stale_disk_error(self):
        self.events.suspend_event_to_string.return_value = 'VIR_DOMAIN_EVENT_SUSPENDED_PAUSED'
        self.handler(self.dom, 'suspended')
        self.extend.assert_not_called()
        self.vm.resume.assert_not_called()
        self.dom.resume.assert_not_called()
        self.lookup.assert_not_called()

    def test_io_error_extends_and_resumes(self):
        self.handler(self.dom, 'suspended')
        self.extend.assert_called_once_with('suspended', '/dev/vg/lv', self.vm, 'vda')
        self.assertEqual(1, self.vm.resume.call_count + self.dom.resume.call_count)
        self.touch.assert_called_once_with('vm')

    def test_io_event_without_disk_errors_does_not_resume(self):
        self.dom.diskErrors.return_value = DiskErrors()
        self.handler(self.dom, 'suspended')
        self.extend.assert_not_called()
        self.vm.resume.assert_not_called()
        self.dom.resume.assert_not_called()


if __name__ == '__main__':
    unittest.main()
