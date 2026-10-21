"""Migration parameter contract tests without a running KVM host.

Execute the current Vm.migrate method, isolating libvirt and host/task services.
The destination XML is a fixture: these tests do not prove XML generation,
libvirt persistence, disk copying, or crash recovery.
"""
import io
import os
import re
import textwrap
import unittest
from collections import deque

try:
    from unittest import mock
except ImportError:
    import mock


DEST_XML = ('<domain><name>migration-test</name><devices>'
            '<disk type="file"><source file="/nfs/new-root.qcow2"/>'
            '<target dev="vda"/></disk>'
            '<disk type="block"><source dev="/dev/shared/data"/>'
            '<target dev="vdb"/></disk></devices></domain>')


class MigrationError(Exception):
    pass


class KvmError(Exception):
    pass


class TaskDaemon(object):
    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._exit(exc_type, exc_val, exc_tb)


def load_migrate():
    # The agent module requires Python 2 and Linux services at import time.
    # Compile the unchanged method instead of replacing its parameter logic.
    source_path = os.path.join(os.path.dirname(__file__), '..', 'plugins',
                               'vm_plugin.py')
    with io.open(source_path, encoding='utf-8') as source_file:
        source = source_file.read()
    vm_source = source.split('class Vm(object):', 1)[1]
    method = re.search(r'^    def migrate\(self, cmd\):\n.*?(?=^    def |\Z)',
                       vm_source, re.M | re.S).group(0)
    libvirt = mock.Mock(
        VIR_MIGRATE_LIVE=1, VIR_MIGRATE_PEER2PEER=2,
        VIR_MIGRATE_UNDEFINE_SOURCE=16, VIR_MIGRATE_PERSIST_DEST=8,
        VIR_MIGRATE_NON_SHARED_DISK=64, VIR_MIGRATE_NON_SHARED_INC=128,
        VIR_MIGRATE_PARAM_MIGRATE_DISKS='migrate_disks',
        VIR_MIGRATE_PARAM_URI='migrate_uri',
        VIR_MIGRATE_PARAM_DEST_XML='destination_xml',
        VIR_MIGRATE_PARAM_PERSIST_XML='persistent_xml',
        VIR_MIGRATE_PARAM_BANDWIDTH='bandwidth', libvirtError=MigrationError)
    vm_class = type('Vm', (object,), {'VM_STATE_SHUTDOWN': 'Shutdown'})
    namespace = {
        'Vm': vm_class, 'libvirt': libvirt, 'deque': deque,
        'linux': mock.Mock(get_host_name=lambda: 'migration-source',
                           wait_callback_success=lambda *a, **kw: True),
        'kvmagent': mock.Mock(KvmError=KvmError),
        'plugin': mock.Mock(TaskDaemon=TaskDaemon), 'logger': mock.Mock(),
        'get_task_stage': lambda cmd: None, 'get_timeout': lambda cmd: 30,
        'check_mirror_jobs': mock.Mock(), 'LIBVIRT_MAJOR_VERSION': 3,
    }
    exec(compile(textwrap.dedent(method), source_path, 'exec'), namespace)
    vm_class.migrate = namespace['migrate']
    return vm_class, libvirt


class Command(object):
    def hasattr(self, name):
        return hasattr(self, name)


class TestVmMigrationParameters(unittest.TestCase):
    def setUp(self):
        vm_class, self.libvirt = load_migrate()
        self.vm = vm_class()
        self.vm.uuid = 'migration-test'
        self.vm.state = 'Running'
        self.vm.domain = mock.Mock()
        self.vm.domain_xmlobject = mock.Mock()
        self.vm.domain_xmlobject.devices.get_child_node_as_list.return_value = []
        self.vm._build_domain_new_xml = mock.Mock(return_value=(['vda'], DEST_XML))
        self.vm._is_vm_paused_with_readonly_flag_on_disk = mock.Mock()
        self.vm._is_vm_migrate_without_dirty_bitmap = mock.Mock(return_value=True)
        self.cmd = Command()
        self.cmd.vmUuid = self.vm.uuid
        self.cmd.migrateFromDestination = False
        self.cmd.srcHostIp = '192.0.2.1'
        self.cmd.destHostIp = '192.0.2.2'
        self.cmd.destHostManagementIp = '192.0.2.3'
        self.cmd.bandwidth = 32
        self.cmd.downTime = 0
        self.cmd.autoConverge = False
        self.cmd.xbzrle = False
        self.cmd.disks = Command()
        self.cmd.disks.root = 'destination-volume'
        self.cmd.storageMigrationPolicy = 'FullCopy'

    def migrate_storage(self):
        self.vm.migrate(self.cmd)
        self.vm.domain.migrateToURI3.assert_called_once()
        self.vm.domain.migrateToURI2.assert_not_called()
        uri, params, flags = self.vm.domain.migrateToURI3.call_args[0]
        self.assertEqual('qemu+tcp://192.0.2.3/system', uri)
        self.assertEqual(DEST_XML, params.get('destination_xml'))
        self.assertEqual(DEST_XML, params.get('persistent_xml'),
                         'persistent XML must use the destination disk paths')
        self.assertTrue(flags & self.libvirt.VIR_MIGRATE_PERSIST_DEST)
        self.assertTrue(flags & self.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE)
        return params, flags

    def test_full_copy_passes_destination_xml_for_both_definitions(self):
        params, flags = self.migrate_storage()
        self.assertEqual(['vda'], params['migrate_disks'])
        self.assertEqual('tcp://192.0.2.2', params['migrate_uri'])
        self.assertEqual(32, params['bandwidth'])
        self.assertTrue(flags & self.libvirt.VIR_MIGRATE_NON_SHARED_DISK)

    def test_incremental_copy_also_updates_persistent_xml(self):
        self.cmd.storageMigrationPolicy = 'IncCopy'
        _, flags = self.migrate_storage()
        self.assertTrue(flags & self.libvirt.VIR_MIGRATE_NON_SHARED_INC)

    def test_partial_copy_forwards_complete_xml_with_unmoved_disk(self):
        params, _ = self.migrate_storage()
        self.assertIn('/nfs/new-root.qcow2', params['persistent_xml'])
        self.assertIn('/dev/shared/data', params['persistent_xml'])
        self.assertEqual(['vda'], params['migrate_disks'])

    def test_zero_bandwidth_keeps_libvirt_default(self):
        self.cmd.bandwidth = 0
        params, _ = self.migrate_storage()
        self.assertNotIn('bandwidth', params)

    def test_shared_storage_migration_keeps_uri2_and_persistence_flags(self):
        for disks in (None, Command()):
            self.vm.domain.reset_mock()
            self.cmd.disks = disks
            self.cmd.storageMigrationPolicy = None
            self.vm._build_domain_new_xml.return_value = (None, None)
            self.vm.migrate(self.cmd)
            self.vm.domain.migrateToURI3.assert_not_called()
            self.vm.domain.migrateToURI2.assert_called_once()
            args = self.vm.domain.migrateToURI2.call_args[0]
            self.assertEqual('qemu+tcp://192.0.2.3/system', args[0])
            self.assertTrue(args[3] & self.libvirt.VIR_MIGRATE_PERSIST_DEST)

    def test_libvirt_error_converts_to_kvm_error_without_retry(self):
        error = MigrationError('migration failed')
        self.vm.domain.migrateToURI3.side_effect = error
        with self.assertRaises(KvmError) as caught:
            self.vm.migrate(self.cmd)
        self.assertEqual('migration failed', str(caught.exception))
        self.vm.domain.migrateToURI3.assert_called_once()
        self.vm.domain.migrateToURI2.assert_not_called()


if __name__ == '__main__':
    unittest.main()
