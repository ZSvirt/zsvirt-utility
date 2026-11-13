import os
import functools
import pipes
import re
import textwrap
import unittest
import uuid
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as etree


class Values(object):
    def __init__(self, **values):
        self.__dict__.update(values)


def element(parent, name, text=None, attrib=None):
    node = etree.SubElement(parent, name, attrib or {})
    node.text = text
    return node


# Isolate production XML/OVS functions from host-only libvirt and Python 2 imports.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
with open(os.path.join(ROOT, 'kvmagent/kvmagent/plugins/vm_plugin.py')) as source:
    VM_SOURCE = source.read()
with open(os.path.join(ROOT, 'zstacklib/zstacklib/utils/ovs.py')) as source:
    OVS_SOURCE = source.read()


def load_function(source, start, end, env):
    exec(compile(textwrap.dedent(source[source.index(start):source.index(end, source.index(start))]),
                 '<production-function>', 'exec'), env)


class TestZnsNic(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.legacy_deletes = []
        self.vm_uuid = '11111111111111111111111111111111'
        self.port_uuid = '22222222-2222-2222-2222-222222222222'
        self.nic = Values(type='dpdkvhostuserclient', bridgePortType='openvswitch',
                          interfaceId=self.port_uuid, bridgeName='br-int', nicInternalName='vnic1.0',
                          mac='fa:16:3e:00:00:01', physicalInterface=None, vlanId=None,
                          srcPath='/var/run/openvswitch/vnic1.0', pciDeviceAddress=None,
                          pci=None, mtu=1450, cleanTraffic=True, ips=[], state='enable',
                          useVirtio=True, driverType='virtio', bootOrder=0,
                          vHostAddOn=Values(queueNum=1))
        self.responses = {
            ('get', 'Bridge', 'br-int', 'datapath_type'): '"netdev"',
            ('get', 'Open_vSwitch', '.', 'dpdk_initialized'): 'true',
            ('--if-exists', 'get', 'Interface', 'vnic1.0', 'name'): '',
        }
        self.env = dict(os=os, re=re, uuid=uuid, pipes=pipes, OvsPath='/var/run/openvswitch/',
                        OvsError=RuntimeError, lock=Values(lock=lambda name: lambda function: function))
        load_function(OVS_SOURCE, 'def isZnsDpdkNic', 'def writeSysfs', self.env)
        self.env['_nicOvsctl'] = self.ovsctl
        self.env['shell'] = Values(ShellError=OSError)
        self.env['getOvsCtl'] = lambda **kwargs: Values(
            destoryNicBackend=lambda *args: self.legacy_deletes.append(args))
        self.ovs = Values(OvsDpdkSupportVnic=['vDPA', 'dpdkvhostuserclient'],
                          isZnsDpdkNic=self.env['isZnsDpdkNic'])
        self.xml_env = dict(ovs=self.ovs, etree=etree, e=element)
        load_function(VM_SOURCE, '    def _build_interface_xml(',
                      '    @staticmethod\n    def _ignore_colo', self.xml_env)

    def ovsctl(self, *args):
        self.commands.append(args)
        if args[0] in ('--may-exist', 'set') or args[:2] == ('--if-exists', 'del-port'):
            return ''
        return self.responses[args]

    def build_xml(self, action):
        return self.xml_env['_build_interface_xml'](self.nic, vhostSrcPath=self.nic.srcPath, action=action)

    def test_kernel_start_attach_detach_use_ovn_port_id(self):
        self.nic.type = 'VNIC'
        self.nic.srcPath = None
        for action in ('Attach', 'Detach', 'Update'):
            xml = self.build_xml(action)
            self.assertEqual('bridge', xml.get('type'))
            self.assertEqual('br-int', xml.find('source').get('bridge'))
            self.assertEqual('openvswitch', xml.find('virtualport').get('type'))
            self.assertEqual(self.port_uuid, xml.find('virtualport/parameters').get('interfaceid'))
            self.assertIsNone(xml.find('filterref'))
            self.assertIsNone(xml.find('vlan'))

    def test_dpdk_start_attach_detach_use_same_qemu_server_socket(self):
        for action in ('Attach', 'Detach', 'Update'):
            xml = self.build_xml(action)
            self.assertEqual('vhostuser', xml.get('type'))
            self.assertEqual('server', xml.find('source').get('mode'))
            self.assertEqual(self.nic.srcPath, xml.find('source').get('path'))
            self.assertEqual('vnic1.0', xml.find('target').get('dev'))
            self.assertIsNone(xml.find('virtualport'))
            self.assertIsNone(xml.find('vlan'))

    def test_linux_bridge_keeps_clean_traffic(self):
        self.nic.type, self.nic.srcPath, self.nic.bridgePortType = 'VNIC', None, None
        self.nic.bridgeName = 'br_eth0'
        xml = self.build_xml('Attach')
        self.assertEqual('br_eth0', xml.find('source').get('bridge'))
        self.assertIsNone(xml.find('virtualport'))
        self.assertEqual('clean-traffic', xml.find('filterref').get('filter'))

    def test_dpdk_port_creation_binds_ovn_id_without_physical_uplink_or_vlan(self):
        path = self.env['createNicBackend'](self.vm_uuid, self.nic)
        self.assertEqual(self.nic.srcPath, path)
        command = self.commands[-1]
        self.assertIn('external_ids:iface-id=' + self.port_uuid, command)
        self.assertIn('external_ids:vm-id=' + self.vm_uuid, command)
        self.assertIn('external_ids:zns-managed=true', command)
        self.assertIn('options:vhost-server-path=' + path, command)
        self.assertFalse(any('tag=' in arg for arg in command))

    def existing_port(self):
        self.responses.update({
            ('--if-exists', 'get', 'Interface', 'vnic1.0', 'name'): '"vnic1.0"',
            ('get', 'Interface', 'vnic1.0', 'external_ids:vm-id'): self.vm_uuid,
            ('get', 'Interface', 'vnic1.0', 'external_ids:iface-id'): self.port_uuid,
            ('get', 'Interface', 'vnic1.0', 'type'): '"dpdkvhostuserclient"',
            ('get', 'Interface', 'vnic1.0', 'options:vhost-server-path'): self.nic.srcPath,
            ('--if-exists', 'get', 'Interface', 'vnic1.0', 'type'): '"dpdkvhostuserclient"',
            ('--if-exists', 'get', 'Interface', 'vnic1.0', 'options:vhost-server-path'): self.nic.srcPath,
            ('port-to-br', 'vnic1.0'): 'br-int',
        })

    def test_repeated_create_keeps_existing_socket_and_port(self):
        self.existing_port()
        self.assertEqual(self.nic.srcPath, self.env['createNicBackend'](self.vm_uuid, self.nic))
        self.assertFalse(any('add-port' in command for command in self.commands))
        self.assertIn(('set', 'Interface', 'vnic1.0', 'external_ids:zns-managed=true'), self.commands)

    def test_foreign_port_is_never_overwritten(self):
        self.existing_port()
        self.responses[('get', 'Interface', 'vnic1.0', 'external_ids:vm-id')] = 'another-vm'
        with self.assertRaises(RuntimeError):
            self.env['createNicBackend'](self.vm_uuid, self.nic)
        self.assertFalse(any('add-port' in command for command in self.commands))

    def test_kernel_host_cannot_create_dpdk_port(self):
        self.responses[('get', 'Bridge', 'br-int', 'datapath_type')] = '"system"'
        with self.assertRaises(RuntimeError):
            self.env['createNicBackend'](self.vm_uuid, self.nic)
        self.assertFalse(any('add-port' in command for command in self.commands))

    def test_cleanup_is_scoped_to_vm_and_idempotent(self):
        query = ('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                 'external_ids:vm-id=' + self.vm_uuid, 'external_ids:zns-managed=true')
        self.responses[query] = 'vnic1.0'
        self.existing_port()
        self.assertEqual(self.nic.srcPath, self.env['destroyNicBackend'](self.vm_uuid, 'vnic1.0'))
        self.assertIn(('--if-exists', 'del-port', 'vnic1.0'), self.commands)
        self.responses[query] = ''
        self.responses[('--if-exists', 'get', 'Interface', 'vnic1.0', 'name')] = ''
        self.commands = []
        self.assertIsNone(self.env['destroyNicBackend'](self.vm_uuid, 'vnic1.0'))
        self.assertFalse(any('del-port' in command for command in self.commands))

    def test_legacy_detach_without_vm_id_keeps_original_named_cleanup(self):
        query = ('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                 'external_ids:vm-id=' + self.vm_uuid, 'external_ids:zns-managed=true')
        self.responses[query] = ''
        self.responses[('--if-exists', 'get', 'Interface', 'legacy.nic', 'name')] = 'legacy.nic'
        self.env['destroyNicBackend'](self.vm_uuid, 'legacy.nic')
        self.assertEqual([(self.vm_uuid, 'legacy.nic')], self.legacy_deletes)
        self.assertEqual(query, self.commands[0])

    def test_legacy_vm_cleanup_keeps_original_controller(self):
        query = ('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                 'external_ids:vm-id=' + self.vm_uuid, 'external_ids:zns-managed=true')
        self.responses[query] = ''
        self.responses[query[:-1]] = 'legacy.nic'
        self.env['destroyNicBackend'](self.vm_uuid)
        self.assertEqual([(self.vm_uuid, None)], self.legacy_deletes)
        self.assertEqual([query, query[:-1]], self.commands)

    def test_failed_zns_probe_falls_back_to_original_error_handling(self):
        def unavailable(*args):
            raise OSError('OVS database unavailable')
        self.env['_nicOvsctl'] = unavailable
        self.assertIsNone(self.env['destroyNicBackend'](self.vm_uuid, 'legacy.nic'))
        self.assertEqual([(self.vm_uuid, 'legacy.nic')], self.legacy_deletes)

    def test_mixed_vm_uses_zns_cleanup_then_original_legacy_cleanup(self):
        query = ('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                 'external_ids:vm-id=' + self.vm_uuid)
        self.responses[query + ('external_ids:zns-managed=true',)] = 'vnic1.0'
        self.responses[query] = 'legacy.nic'
        self.existing_port()
        self.env['destroyNicBackend'](self.vm_uuid)
        self.assertIn(('--if-exists', 'del-port', 'vnic1.0'), self.commands)
        self.assertNotIn(('--if-exists', 'del-port', 'legacy.nic'), self.commands)
        self.assertEqual([(self.vm_uuid,)], self.legacy_deletes)

    def test_legacy_dpdk_creation_still_uses_existing_controller(self):
        self.nic.bridgePortType = None
        calls = []
        self.env['getOvsCtl'] = lambda **kwargs: Values(createNicBackend=lambda *args: calls.append(args))
        self.env['createNicBackend'](self.vm_uuid, self.nic)
        self.assertEqual([(self.vm_uuid, self.nic)], calls)
        self.assertEqual([], self.commands)

    def test_disappeared_port_does_not_block_remaining_cleanup(self):
        self.existing_port()
        query = ('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                 'external_ids:vm-id=' + self.vm_uuid)
        self.responses[query + ('external_ids:zns-managed=true',)] = 'gone\nvnic1.0'
        self.responses[query] = ''
        self.responses[('--if-exists', 'get', 'Interface', 'gone', 'type')] = ''
        self.env['destroyNicBackend'](self.vm_uuid)
        self.assertIn(('--if-exists', 'del-port', 'vnic1.0'), self.commands)
        self.assertNotIn(('--if-exists', 'del-port', 'gone'), self.commands)
        self.assertEqual([], self.legacy_deletes)

    def test_port_disappearing_before_socket_read_is_success(self):
        self.existing_port()
        self.responses[('--format=csv', '--data=bare', '--no-heading', '--columns=name', 'find', 'Interface',
                        'external_ids:vm-id=' + self.vm_uuid, 'external_ids:zns-managed=true')] = 'vnic1.0'
        self.responses[('--if-exists', 'get', 'Interface', 'vnic1.0', 'options:vhost-server-path')] = ''
        self.env['destroyNicBackend'](self.vm_uuid, 'vnic1.0')
        self.assertIn(('--if-exists', 'del-port', 'vnic1.0'), self.commands)

    def test_concurrent_deletes_share_lock_from_lookup_to_removal(self):
        with open(os.path.join(ROOT, 'zstacklib/zstacklib/utils/lock.py')) as source:
            lock_source = source.read()
        lock_env = dict(threading=threading, weakref=weakref, functools=functools,
                        log=Values(get_logger=lambda *args: None), __name__='test_lock')
        load_function(lock_source, '_internal_lock =', 'class Locker', lock_env)
        env = dict(self.env, lock=Values(lock=lock_env['lock']))
        load_function(OVS_SOURCE, 'def isZnsDpdkNic', 'def writeSysfs', env)
        gate = threading.Barrier(2)
        ports = set(['vnic1.0'])
        deleted = []

        def ovsctl(*args):
            self.assertTrue(lock_env['_get_lock']('ovs-zns-nic')._is_owned())
            if 'find' in args:
                return '\n'.join(ports)
            if args[:2] == ('--if-exists', 'del-port'):
                ports.discard(args[2])
                deleted.append(args[2])
                return ''
            self.assertEqual(('--if-exists', 'get', 'Interface'), args[:3])
            if args[3] not in ports:
                return ''
            return {'type': 'dpdkvhostuserclient', 'name': args[3],
                    'options:vhost-server-path': self.nic.srcPath}[args[4]]

        env['_nicOvsctl'] = ovsctl

        def delete(specific):
            gate.wait(timeout=5)
            env['destroyNicBackend'](self.vm_uuid, specific)

        for specific in ('vnic1.0', None):
            ports.add('vnic1.0')
            deleted[:] = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(delete, specific) for _ in range(2)]
                for future in futures:
                    future.result(timeout=5)
            self.assertEqual(['vnic1.0'], deleted)
            self.assertEqual([], self.legacy_deletes)

    def test_dpdk_memory_is_shared_with_or_without_hugepages(self):
        for hugepages in (False, True):
            root = etree.Element('domain')
            env = dict(self.xml_env, elements={'root': root},
                       cmd=Values(nics=[self.nic], MemAccess='private', useHugePage=hugepages))
            load_function(VM_SOURCE, '        def make_memory_backing():', '        def make_cpu():', env)
            env['make_memory_backing']()
            self.assertEqual('shared', root.find('memoryBacking/access').get('mode'))
            if hugepages:
                self.assertIsNotNone(root.find('memoryBacking/hugepages'))
                self.assertIsNone(root.find('memoryBacking/source'))
            else:
                self.assertEqual('memfd', root.find('memoryBacking/source').get('type'))


class TestZnsDetach(unittest.TestCase):
    def setUp(self):
        class LibvirtError(Exception):
            @property
            def message(self):
                return str(self)

        self.attached = True
        self.stays_attached = False
        self.detach_error = None
        self.cleanup_failures = 0
        self.cleanup_calls = 0
        self.detach_calls = 0
        self.nic = Values(type='dpdkvhostuserclient', bridgePortType='openvswitch',
                          mac='fa:16:3e:00:00:01', nicInternalName='vnic1.0')

        def detach(*args):
            self.detach_calls += 1
            self.attached = self.stays_attached
            if self.detach_error:
                raise LibvirtError(self.detach_error)

        def cleanup(*args):
            self.assertEqual(('vm-1', 'vnic1.0'), args)
            self.assertFalse(self.attached)
            self.cleanup_calls += 1
            if self.cleanup_calls <= self.cleanup_failures:
                raise RuntimeError('OVS cleanup failed')

        self.vm = Values(refresh=lambda: None, state='running', VM_STATE_RUNNING='running', VM_STATE_PAUSED='paused',
                         domain=Values(detachDeviceFlags=detach, detachDevice=detach), domain_xml='<domain/>',
                         domain_xmlobject=Values(devices=Values(get_child_node_as_list=lambda name:
                             [Values(mac=Values(address_=self.nic.mac))] if self.attached else [])),
                         _interface_cmd_to_xml=lambda *args, **kwargs: '<interface/>')
        env = dict(linux=Values(wait_callback_success=lambda callback, **kwargs: callback(None),
                               get_exception_stacktrace=lambda: ''),
                   iproute=Values(is_device_ifname_exists=lambda name: False),
                   libvirt=Values(libvirtError=LibvirtError, VIR_DOMAIN_AFFECT_LIVE=1),
                   logger=Values(debug=lambda *args: None, warn=lambda *args: None))
        load_function(OVS_SOURCE, 'def isZnsDpdkNic(', 'def _nicOvsctl(', env)
        env['ovs'] = Values(isZnsDpdkNic=env['isZnsDpdkNic'], destroyNicBackend=cleanup,
                            OvsDpdkSupportVnic=['dpdkvhostuserclient'])
        load_function(VM_SOURCE, '    def _detach_nic(', '    def detach_nic(', env)
        with open(os.path.join(ROOT, 'zstacklib/zstacklib/utils/linux.py')) as source:
            retry_env = dict(functools=functools, time=Values(sleep=lambda seconds: None))
            load_function(source.read(), 'def retry(', 'def retry_if_unexpected_value(', retry_env)
        self.detach = retry_env['retry'](times=3, sleep_time=0)(env['_detach_nic'])

    def run_detach(self):
        self.detach(self.vm, Values(vmUuid='vm-1', nic=self.nic))

    def test_already_absent_device_still_cleans_backend(self):
        self.attached = False
        self.run_detach()
        self.assertEqual(0, self.detach_calls)
        self.assertEqual(1, self.cleanup_calls)

    def test_normal_detach_cleans_backend(self):
        self.run_detach()
        self.assertEqual(1, self.cleanup_calls)

    def test_already_unplugging_cleans_backend_after_device_leaves(self):
        self.detach_error = 'device is already in the process of unplug'
        self.run_detach()
        self.assertEqual(1, self.cleanup_calls)

    def test_other_libvirt_error_with_absent_device_still_cleans_backend(self):
        self.detach_error = 'device not found'
        self.run_detach()
        self.assertEqual(1, self.cleanup_calls)

    def test_cleanup_failure_retries_without_detaching_device_again(self):
        self.cleanup_failures = 1
        self.run_detach()
        self.assertEqual(1, self.detach_calls)
        self.assertEqual(2, self.cleanup_calls)

    def test_persistent_cleanup_failure_is_not_success(self):
        self.cleanup_failures = 3
        with self.assertRaises(RuntimeError):
            self.run_detach()
        self.assertEqual(3, self.cleanup_calls)

    def test_attached_device_keeps_backend(self):
        self.stays_attached = True
        self.detach_error = 'device is already in the process of unplug'
        with self.assertRaises(Exception):
            self.run_detach()
        self.assertEqual(0, self.cleanup_calls)

    def test_absent_non_zns_device_keeps_original_early_return(self):
        self.attached = False
        self.nic.bridgePortType = None
        self.run_detach()
        self.assertEqual(0, self.cleanup_calls)
        self.assertEqual(0, self.detach_calls)


class TestZnsStartRollback(unittest.TestCase):
    ovsctl = TestZnsNic.ovsctl
    existing_port = TestZnsNic.existing_port

    def setUp(self):
        TestZnsNic.setUp(self)
        self.deleted = []
        self.lookup_error = False
        self.active = False
        self.pid = None
        self.failure = 'xml'
        self.cmd = Values(vmInstanceUuid=self.vm_uuid, vmName='test', nics=[self.nic],
                          addons=None, coloPrimary=False, coloSecondary=False,
                          memorySnapshotPath=None, timeout=1, createPaused=False)
        self.ovs.createNicBackend = self.env['createNicBackend']
        self.ovs.destroyNicBackend = lambda vm, name: self.deleted.append(name)

        class LibvirtError(Exception):
            @property
            def message(self):
                return str(self)

        def lookup(vm_uuid, required=True):
            if self.lookup_error:
                raise LibvirtError('lookup failed')
            if required:
                raise RuntimeError('VM is not running')
            if self.active:
                return Values(domain=Values(isActive=lambda: True), state='running', get_name=lambda: 'test')
            return None

        def start(*args):
            raise LibvirtError(self.failure)

        def from_cmd(cmd, created):
            env = dict(cmd=cmd, created_zns_nics=created, ovs=self.ovs,
                       elements={'devices': etree.Element('devices')},
                       Vm=Values(_build_interface_xml=lambda *args: etree.Element('interface')))
            load_function(VM_SOURCE, '        def make_nics():', '        def make_meta():', env)
            env['make_nics']()
            if self.failure == 'xml':
                raise RuntimeError('XML construction failed')
            return Values(start=start)

        self.start_env = dict(ovs=self.ovs, os=os,
            Vm=Values(from_StartVmCmd=from_cmd, VM_STATE_RUNNING='running'),
            get_vm_by_uuid_no_retry=lookup, get_vm_by_uuid=lookup,
            libvirt=Values(libvirtError=LibvirtError), kvmagent=Values(KvmError=RuntimeError),
            linux=Values(find_vm_pid_by_uuid=lambda vm: self.pid, get_exception_stacktrace=lambda: ''),
            logger=Values(warn=lambda *args: None, debug=lambda *args: None))
        load_function(VM_SOURCE, '    def _start_vm(self, cmd):', '    def _cleanup_iptable_chains(', self.start_env)
        self.plugin = Values(_prepare_ebtables_for_mocbr=lambda cmd: None)
        for name in ('_start_vm', '_start_vm_with_nic_tracking', '_rollback_created_zns_nics'):
            setattr(self.plugin, name, functools.partial(self.start_env[name], self.plugin))

    def test_second_nic_validation_failure_rolls_back_first_port(self):
        broken = Values(**self.nic.__dict__)
        broken.nicInternalName = 'vnic1.1'
        broken.interfaceId = 'invalid-uuid'
        self.cmd.nics.append(broken)
        with self.assertRaises(RuntimeError):
            self.plugin._start_vm(self.cmd)
        self.assertEqual(['vnic1.0'], self.deleted)

    def test_xml_and_libvirt_failures_roll_back_created_ports(self):
        for failure in ('xml', 'defineXML', 'createWithFlags'):
            self.failure = failure
            self.deleted[:] = []
            with self.assertRaises(RuntimeError):
                self.plugin._start_vm(self.cmd)
            self.assertEqual(['vnic1.0'], self.deleted)

    def test_reused_port_is_not_rolled_back(self):
        self.existing_port()
        with self.assertRaises(RuntimeError):
            self.plugin._start_vm(self.cmd)
        self.assertEqual([], self.deleted)

    def test_active_or_unknown_vm_keeps_ports(self):
        for condition in ('active', 'pid', 'unknown'):
            def fail_start(cmd, created):
                created.append('vnic1.0')
                self.active = condition == 'active'
                self.pid = 123 if condition == 'pid' else None
                self.lookup_error = condition == 'unknown'
                raise RuntimeError('start failed')
            self.plugin._start_vm_with_nic_tracking = fail_start
            with self.assertRaises(RuntimeError):
                self.plugin._start_vm(self.cmd)
            self.assertEqual([], self.deleted)


if __name__ == '__main__':
    unittest.main()
