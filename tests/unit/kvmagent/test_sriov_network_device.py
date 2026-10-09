"""SR-IOV unit tests for host_plugin using simulated Linux/sysfs boundaries.

Run with Python 3: python -B tests/unit/kvmagent/test_sriov_network_device.py
"""
import ast
import errno
import io
from pathlib import Path
import posixpath
import shlex
import textwrap
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[3]
PF = '0000:42:00.1'
PF_PATH = '/sys/bus/pci/devices/' + PF
VF_ADDRESSES = ['0000:42:10.%s' % value for value in (1, 3, 5, 7)] + [
    '0000:42:11.%s' % value for value in (1, 3, 5, 7)]


class FakeKernel:
    def __init__(self, count=8, driver='txgbevf'):
        self.files = {}
        self.dirs = set()
        self.links = {}
        self.events = []
        self.failures = {}
        self.read_errors = {}
        self.denied = set()
        self.vfs = VF_ADDRESSES[:count]
        self.driver = driver
        self.domains = {}
        self.ips = {}
        self.routes = {}
        self.ovs = []
        self.commands = []
        self.command_error = None
        self.before_command = None
        self.before_write = None
        self.ignore_disable = False
        self.mkdir(PF_PATH)
        self.add_file(PF_PATH + '/sriov_numvfs', str(count))
        self.mkdir('/proc')
        self.mkdir('/proc/sys/net/ipv6')
        self.add_file('/proc/net/packet', 'sk RefCnt Type Proto Iface R Rmem User Inode\n')
        self.links[PF_PATH + '/driver'] = '/sys/bus/pci/drivers/txgbe'
        self.mkdir(PF_PATH + '/net')
        self.links[PF_PATH + '/net/eth11'] = '/sys/class/net/eth11'
        self.mkdir('/sys/class/net/eth11')
        self.links['/sys/class/net/eth11/device'] = PF_PATH
        self.add_file('/sys/class/net/eth11/phys_port_name', '')
        for index, address in enumerate(self.vfs):
            path = '/sys/bus/pci/devices/' + address
            self.mkdir(path)
            self.links[PF_PATH + '/virtfn%s' % index] = path
            self.links[path + '/physfn'] = PF_PATH
            self.add_file(path + '/resource0', '')
            if driver:
                self.links[path + '/driver'] = '/sys/bus/pci/drivers/' + driver
                self.add_file('/sys/bus/pci/drivers/' + driver + '/unbind', '')
                self.add_file('/sys/bus/pci/drivers/' + driver + '/bind', '')
                self.add_net(address)

    def mkdir(self, path):
        while path and path != '/':
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def add_file(self, path, content):
        self.mkdir(posixpath.dirname(path))
        self.files[path] = content

    def realpath(self, path):
        path = posixpath.normpath(path)
        for unused in range(30):
            found = False
            for link in sorted(self.links, key=len, reverse=True):
                if path == link or path.startswith(link + '/'):
                    path = posixpath.normpath(self.links[link] + path[len(link):])
                    found = True
                    break
            if not found:
                return path
        raise AssertionError('cyclic fake sysfs link')

    def exists(self, path):
        path = self.realpath(path)
        return path in self.files or path in self.dirs

    def listdir(self, path):
        if path in self.denied:
            raise PermissionError(errno.EACCES, 'Permission denied', path)
        path = self.realpath(path)
        if path not in self.dirs:
            raise FileNotFoundError(errno.ENOENT, 'No such directory', path)
        prefix = path.rstrip('/') + '/'
        return sorted(set(name[len(prefix):].split('/')[0] for name in
                          list(self.files) + list(self.dirs) + list(self.links)
                          if name.startswith(prefix)))

    def readlink(self, path):
        if path not in self.links:
            raise FileNotFoundError(errno.ENOENT, 'No such link', path)
        return self.links[path]

    def open(self, path, mode='r'):
        path = self.realpath(path)
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, 'No such attribute', path)
        fs = self

        class Stream(io.StringIO):
            def read(self, *args):
                if path in fs.read_errors:
                    raise fs.read_errors[path]
                return super().read(*args)

            def __exit__(self, exc_type, exc_value, traceback):
                if mode == 'w' and exc_type is None:
                    fs.write(path, self.getvalue())
                return super().__exit__(exc_type, exc_value, traceback)

        return Stream(self.files[path] if mode == 'r' else '')

    def interface(self, address):
        return 'eth%s' % (79 + self.vfs.index(address))

    def add_net(self, address):
        interface = self.interface(address)
        path = '/sys/bus/pci/devices/' + address
        net = '/sys/class/net/' + interface
        self.mkdir(path + '/net')
        self.links[path + '/net/' + interface] = net
        self.mkdir(net)
        self.links[net + '/device'] = path
        self.add_file(net + '/flags', '0x1002')  # DOWN, broadcast, multicast
        self.add_file(net + '/ifindex', str(79 + self.vfs.index(address)))
        self.add_file(net + '/address', '00:11:22:33:44:%02x' % self.vfs.index(address))

    def remove_prefix(self, prefix):
        for mapping in (self.files, self.links):
            for name in list(mapping):
                if name == prefix or name.startswith(prefix + '/'):
                    del mapping[name]
        self.dirs = set(name for name in self.dirs if name != prefix and not name.startswith(prefix + '/'))

    def remove_net(self, address):
        self.remove_prefix('/sys/bus/pci/devices/' + address + '/net')
        self.remove_prefix('/sys/class/net/' + self.interface(address))

    def write(self, path, value):
        if self.before_write:
            self.before_write(path, value)
        operation = posixpath.basename(path)
        self.events.append((operation, value))
        error = self.failures.get((operation, value))
        if error:
            raise error
        if operation == 'unbind':
            del self.links['/sys/bus/pci/devices/' + value + '/driver']
            self.remove_net(value)
        elif operation == 'bind':
            driver = posixpath.basename(posixpath.dirname(path))
            self.links['/sys/bus/pci/devices/' + value + '/driver'] = '/sys/bus/pci/drivers/' + driver
            self.add_net(value)
        elif operation == 'sriov_numvfs':
            if self.ignore_disable:
                return
            self.files[path] = value
            for index, address in enumerate(self.vfs):
                self.links.pop(PF_PATH + '/virtfn%s' % index)
                self.remove_prefix('/sys/bus/pci/devices/' + address)
                self.remove_prefix('/sys/class/net/' + self.interface(address))
        else:
            raise AssertionError('unexpected kernel write: ' + path)

    def run(self, command):
        assert command.startswith('timeout -k 5s 15s ')
        command = command[len('timeout -k 5s 15s '):]
        self.commands.append(command)
        if self.before_command:
            self.before_command(command)
        if self.command_error:
            result = self.command_error(command)
            if result:
                return result
        words = shlex.split(command)
        if words[:2] == ['virsh', 'list']:
            return 0, '\n'.join(self.domains), ''
        if words[:2] == ['virsh', 'dumpxml']:
            domain = self.domains[words[2]]
            return 0, domain.get('inactive' if '--inactive' in words else 'live', '<domain/>'), ''
        if words[0] == 'ip':
            interface = words[-1]
            if 'addr' in words:
                return 0, self.ips.get(interface, ''), ''
            return 0, self.routes.get((interface, words[1]), ''), ''
        if words[0] == 'ovs-vsctl':
            name = words[-2]
            return 0, ('"%s"' % name) if name in self.ovs else '[]', ''
        if words[0] == 'lspci':
            self.write(words[-1], '0')
            return 0, '', ''
        raise AssertionError('unexpected command: ' + command)

    def module_os(self):
        path = SimpleNamespace(join=posixpath.join, basename=posixpath.basename,
                               dirname=posixpath.dirname, realpath=self.realpath,
                               exists=self.exists, islink=lambda name: name in self.links,
                               isdir=lambda name: self.realpath(name) in self.dirs,
                               isfile=lambda name: self.realpath(name) in self.files)
        return SimpleNamespace(path=path, listdir=self.listdir, readlink=self.readlink, sep='/')



HOST_SOURCE = ROOT / 'kvmagent/kvmagent/plugins/host_plugin.py'


class SriovNetworkTests(unittest.TestCase):
    def setUp(self):
        self.use_kernel(FakeKernel())

    def use_kernel(self, kernel):
        self.fs = kernel
        source = HOST_SOURCE.read_text(encoding='utf-8')
        start = source.index('    def _ungenerate_sriov_net_devices(')
        end = source.index('\n    @', start)
        self.module_os = self.fs.module_os()
        # The handler runs on Linux, where EOPNOTSUPP and ENOTSUP are both 95.
        namespace = {'os': self.module_os, 're': __import__('re'),
                     'errno': SimpleNamespace(EOPNOTSUPP=95, ENOTSUP=95),
                     'shell_quote': shlex.quote, 'bash_roe': self.fs.run, 'open': self.fs.open}
        exec(compile(ast.parse(textwrap.dedent(source[start:end])), str(HOST_SOURCE), 'exec'), namespace)
        self.disable = namespace['_ungenerate_sriov_net_devices']

    def invoke(self, address=PF):
        response = SimpleNamespace(success=True, error=None)
        command = SimpleNamespace(pciDeviceAddress=address, pciDeviceType='Ethernet_Controller')
        self.disable(None, command, response)
        return response

    def assert_blocked(self, text):
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertRegex(response.error, text)
        self.assertEqual([], self.fs.events)
        self.assertEqual('8', self.fs.files[PF_PATH + '/sriov_numvfs'])

    def assert_restored(self):
        self.assertEqual('8', self.fs.files[PF_PATH + '/sriov_numvfs'])
        self.assertTrue(all(self.fs.links.get('/sys/bus/pci/devices/' + vf + '/driver') ==
                            '/sys/bus/pci/drivers/txgbevf' for vf in self.fs.vfs))

    def domain_xml(self, interface=False):
        if interface:
            device = '<interface type="direct"><source dev="eth79" mode="bridge"/></interface>'
        else:
            device = ('<hostdev type="pci"><source>'
                      '<address domain="0x0000" bus="0x42" slot="0x10" function="0x1"/>'
                      '</source></hostdev>')
        return '<domain><name>customer-vm</name><devices>' + device + '</devices></domain>'

    def test_eight_idle_wangxun_vfs_unbound_before_disable(self):
        self.assertTrue(self.invoke().success)
        self.assertEqual([('unbind', vf) for vf in self.fs.vfs] + [('sriov_numvfs', '0')], self.fs.events)
        self.assertEqual('0', self.fs.files[PF_PATH + '/sriov_numvfs'])
        self.assertEqual('/sys/bus/pci/drivers/txgbe', self.fs.links[PF_PATH + '/driver'])
        self.assertFalse(any('/virtfn' in link for link in self.fs.links))

    def test_pci_and_class_net_symlinks_resolve_to_physical_sysfs_paths(self):
        realpath = self.fs.realpath
        def physical_path(path):
            resolved = realpath(path)
            for vf in self.fs.vfs:
                prefix = '/sys/bus/pci/devices/' + vf
                if resolved == prefix or resolved.startswith(prefix + '/'):
                    return '/sys/devices/pci0000:40/' + vf + resolved[len(prefix):]
            return resolved
        self.module_os.path.realpath = physical_path
        self.assertTrue(self.invoke().success)
        self.assertEqual('0', self.fs.files[PF_PATH + '/sriov_numvfs'])

    def test_idle_host_vf_drivers_share_the_release_flow(self):
        for driver in ('txgbevf', 'ixgbevf', 'igbvf', 'i40evf', 'iavf', 'hns3', 'bnxt_en', 'ena', 'vendor_vf'):
            with self.subTest(driver=driver):
                self.use_kernel(FakeKernel(driver=driver))
                self.assertTrue(self.invoke().success)
                self.assertEqual([('unbind', vf) for vf in self.fs.vfs] + [('sriov_numvfs', '0')], self.fs.events)
                self.assertEqual('0', self.fs.files[PF_PATH + '/sriov_numvfs'])

    def mixed_drivers(self):
        expected = {}
        for index, vf in enumerate(self.fs.vfs):
            driver = ('txgbevf', 'ixgbevf', 'iavf', 'hns3')[index % 4]
            self.fs.links['/sys/bus/pci/devices/' + vf + '/driver'] = '/sys/bus/pci/drivers/' + driver
            for attribute in ('bind', 'unbind'):
                self.fs.add_file('/sys/bus/pci/drivers/' + driver + '/' + attribute, '')
            expected[vf] = '/sys/bus/pci/drivers/' + driver
        return expected

    def test_mixed_host_drivers_are_all_released(self):
        self.mixed_drivers()
        self.assertTrue(self.invoke().success)
        self.assertEqual([('unbind', vf) for vf in self.fs.vfs] + [('sriov_numvfs', '0')], self.fs.events)

    def test_mixed_drivers_are_restored_to_each_original_driver(self):
        for failure in [('unbind', self.fs.vfs[5]), ('sriov_numvfs', '0')]:
            with self.subTest(failure=failure):
                self.use_kernel(FakeKernel())
                expected = self.mixed_drivers()
                self.fs.failures[failure] = OSError(errno.EPERM, 'device rejected operation')
                response = self.invoke()
                self.assertFalse(response.success)
                self.assertIn('device rejected operation', response.error)
                for vf, driver in expected.items():
                    self.assertEqual(driver, self.fs.links.get('/sys/bus/pci/devices/' + vf + '/driver'))
                self.assertEqual('8', self.fs.files[PF_PATH + '/sriov_numvfs'])

    def test_busy_vf_on_other_driver_blocks_all_releases(self):
        self.mixed_drivers()
        self.fs.files['/sys/class/net/eth86/flags'] = '0x1003'
        self.assert_blocked(r'VF\[0000:42:11.7\].*hns3.*eth86.*UP')

    def test_non_network_driver_is_not_detached(self):
        self.use_kernel(FakeKernel(driver='other_driver'))
        self.fs.remove_net(self.fs.vfs[0])
        self.assert_blocked('other_driver.*no visible host network interface')

    def test_rdma_and_uio_endpoints_require_usage_verification(self):
        for kind in ('infiniband', 'infiniband_verbs', 'uio'):
            with self.subTest(kind=kind):
                self.use_kernel(FakeKernel(driver='mlx5_core'))
                self.fs.mkdir('/sys/bus/pci/devices/' + self.fs.vfs[-1] + '/' + kind + '/device0')
                self.assert_blocked('mlx5_core.*' + kind + ' endpoints require userspace usage verification')

    def unsupported_pf_port_name(self):
        path = '/sys/class/net/eth11/phys_port_name'
        self.assertTrue(self.fs.exists(PF_PATH + '/net/eth11/phys_port_name'))
        self.fs.read_errors[path] = OSError(95, 'Operation not supported', path)

    def test_pf_port_name_unsupported_allows_idle_bound_vfs(self):
        for driver in ('txgbevf', 'ixgbevf', 'iavf'):
            with self.subTest(driver=driver):
                self.use_kernel(FakeKernel(driver=driver))
                self.unsupported_pf_port_name()
                response = self.invoke()
                self.assertTrue(response.success, response.error)
                self.assertEqual([('unbind', vf) for vf in self.fs.vfs] +
                                 [('sriov_numvfs', '0')], self.fs.events)

    def test_pf_port_name_unsupported_allows_already_unbound_vfs(self):
        self.use_kernel(FakeKernel(driver=None))
        self.unsupported_pf_port_name()
        response = self.invoke()
        self.assertTrue(response.success, response.error)
        self.assertEqual([('sriov_numvfs', '0')], self.fs.events)

    def test_pf_port_name_other_read_errors_still_block(self):
        for code in (errno.EACCES, errno.EIO, errno.ENOENT, errno.ENODEV, errno.EINVAL):
            with self.subTest(errno=code):
                path = '/sys/class/net/eth11/phys_port_name'
                self.fs.read_errors[path] = OSError(code, 'PF attribute read failed', path)
                self.assert_blocked('PF attribute read failed')

    def test_unsupported_pf_port_name_does_not_bypass_vf_usage_checks(self):
        self.unsupported_pf_port_name()
        self.fs.files['/sys/class/net/eth86/flags'] = '0x1003'
        self.assert_blocked('eth86.*UP')

    def test_unsupported_error_on_other_sysfs_attributes_still_blocks(self):
        path = '/sys/class/net/eth86/flags'
        self.fs.read_errors[path] = OSError(95, 'flags read failed', path)
        self.assert_blocked('flags read failed')

    def test_pf_representor_requires_switchdev_cleanup(self):
        self.use_kernel(FakeKernel(driver='bnxt_en'))
        self.unsupported_pf_port_name()
        self.fs.add_file(PF_PATH + '/net/rep3/phys_port_name', 'pf0vf3')
        self.assert_blocked(r'VF\[0000:42:10.7\].*bnxt_en.*rep3.*switchdev cleanup')

    def test_persistent_vm_reference_blocks_even_when_vf_is_unbound(self):
        for vf in self.fs.vfs:
            del self.fs.links['/sys/bus/pci/devices/' + vf + '/driver']
            self.fs.remove_net(vf)
        self.fs.domains['uuid-1'] = {'inactive': self.domain_xml()}
        self.assert_blocked('unbound.*customer-vm')

    def test_other_driver_missing_restore_attribute_blocks_all_releases(self):
        self.mixed_drivers()
        del self.fs.files['/sys/bus/pci/drivers/iavf/bind']
        self.assert_blocked('iavf.*no bind attribute')

    def test_already_unbound_vfs(self):
        for vf in self.fs.vfs:
            del self.fs.links['/sys/bus/pci/devices/' + vf + '/driver']
            self.fs.remove_net(vf)
        self.assertTrue(self.invoke().success)
        self.assertEqual([('sriov_numvfs', '0')], self.fs.events)

    def test_already_disabled_is_idempotent(self):
        self.assertTrue(self.invoke().success)
        self.fs.events.clear()
        self.fs.commands.clear()
        self.assertTrue(self.invoke().success)
        self.assertEqual([], self.fs.events)
        self.assertEqual([], self.fs.commands)

    def test_passthrough_or_dpdk_driver_blocks_all_unbinds(self):
        for driver in ('vfio-pci', 'igb_uio', 'uio_pci_generic', 'pci-stub'):
            with self.subTest(driver=driver):
                self.fs.links['/sys/bus/pci/devices/' + self.fs.vfs[-1] + '/driver'] = '/sys/bus/pci/drivers/' + driver
                self.assert_blocked(r'VF\[0000:42:11.7\].*' + driver + '.*userspace')

    def test_last_vf_up_blocks_before_first_unbind(self):
        self.fs.files['/sys/class/net/eth86/flags'] = '0x1003'
        self.assert_blocked(r'VF\[0000:42:11.7\].*txgbevf.*eth86.*UP')

    def test_bridge_bond_and_vlan_block(self):
        for entry, owner in [('master', 'br0'), ('master', 'bond0'), ('upper_eth79.20', 'eth79.20')]:
            with self.subTest(owner=owner):
                path = '/sys/class/net/eth79/' + entry
                self.fs.links[path] = '/sys/class/net/' + owner
                self.assert_blocked(owner)
                del self.fs.links[path]

    def test_ipv4_and_ipv6_on_down_vf_block(self):
        for family in ('inet', 'inet6'):
            with self.subTest(family=family):
                self.fs.ips['eth79'] = '79: eth79 %s 192.0.2.1/24' % family
                self.assert_blocked('has IP addresses')

    def test_routes_on_down_vf_block(self):
        for family in ('-4', '-6'):
            with self.subTest(family=family):
                self.fs.routes[('eth79', family)] = 'default dev eth79 table 101'
                self.assert_blocked('table 101')
                self.fs.routes.clear()

    def test_persistent_host_config_blocks(self):
        self.fs.add_file('/etc/sysconfig/network-scripts/ifcfg-eth79', 'BOOTPROTO=dhcp')
        self.assert_blocked('persistent host network configuration')

    def test_network_namespace_not_visible_blocks(self):
        self.fs.remove_prefix('/sys/class/net/eth79')
        self.assert_blocked('outside the host network namespace')

    def test_no_visible_interface_blocks(self):
        self.fs.remove_net(self.fs.vfs[0])
        self.assert_blocked('no visible host network interface')

    def test_live_and_inactive_vm_pci_reference_block(self):
        for kind in ('live', 'inactive'):
            with self.subTest(kind=kind):
                self.fs.domains['uuid-1'] = {kind: self.domain_xml()}
                self.assert_blocked(r'VF\[0000:42:10.1\].*customer-vm')

    def test_resolved_interface_hostdev_blocks(self):
        xml = self.domain_xml().replace('<hostdev type="pci">', '<interface><actual type="hostdev">')
        xml = xml.replace('</hostdev>', '</actual></interface>')
        self.fs.domains['uuid-1'] = {'live': xml}
        self.assert_blocked('customer-vm')

    def test_direct_vm_interface_blocks(self):
        self.fs.domains['uuid-1'] = {'live': self.domain_xml(interface=True)}
        self.assert_blocked('customer-vm.*network interface')

    def test_ovs_interface_blocks(self):
        self.fs.add_file('/var/run/openvswitch/db.sock', '')
        self.fs.ovs = ['eth86']
        self.assert_blocked('OVS interface.*eth86')

    def test_unrelated_vm_and_ovs_interface_allow_disable(self):
        self.fs.domains['other'] = {'live': '<domain><devices/></domain>'}
        self.fs.add_file('/var/run/openvswitch/db.sock', '')
        self.fs.ovs = ['other0']
        self.assertTrue(self.invoke().success)

    def test_failed_usage_queries_block_without_mutation(self):
        for prefix in ('virsh', 'ip', 'ovs-vsctl'):
            with self.subTest(prefix=prefix):
                self.fs.add_file('/var/run/openvswitch/db.sock', '')
                self.fs.command_error = lambda command: (124, '', 'query timed out') if command.startswith(prefix) else None
                self.assert_blocked('query timed out')

    def test_invalid_vm_xml_blocks_without_mutation(self):
        self.fs.domains['uuid-1'] = {'live': '<domain>'}
        self.assert_blocked('no element found')

    def test_middle_unbind_failure_restores_previous_vfs(self):
        self.fs.failures[('unbind', self.fs.vfs[3])] = OSError(errno.EPERM, 'unbind denied')
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('unbind denied', response.error)
        self.assertNotIn(('sriov_numvfs', '0'), self.fs.events)
        self.assert_restored()

    def test_pf_write_error_restores_all_vfs_and_preserves_stderr(self):
        self.fs.command_error = lambda command: (1, '', 'echo: write error: Operation not permitted') if command.startswith('lspci') else None
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('Operation not permitted', response.error)
        self.assertIn(self.fs.vfs[-1] + ':txgbevf', response.error)
        self.assert_restored()

    def test_restore_failure_is_reported_with_original_error(self):
        self.fs.failures[('sriov_numvfs', '0')] = OSError(errno.EPERM, 'disable denied')
        self.fs.failures[('bind', self.fs.vfs[-1])] = OSError(errno.EIO, 'bind denied')
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('disable denied', response.error)
        self.assertIn('failed to restore VF drivers', response.error)
        self.assertIn('VF[' + self.fs.vfs[-1] + '], driver[txgbevf]', response.error)
        self.assertIn('bind denied', response.error)

    def test_new_driver_binding_is_never_replaced_during_recovery(self):
        def before_write(path, value):
            if path.endswith('/unbind') and value == self.fs.vfs[1]:
                self.fs.links['/sys/bus/pci/devices/' + self.fs.vfs[0] + '/driver'] = '/sys/bus/pci/drivers/vfio-pci'
        self.fs.before_write = before_write
        self.fs.failures[('unbind', self.fs.vfs[1])] = OSError(errno.EPERM, 'unbind denied')
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('refusing to replace', response.error)
        self.assertEqual('/sys/bus/pci/drivers/vfio-pci', self.fs.links['/sys/bus/pci/devices/' + self.fs.vfs[0] + '/driver'])
        self.assertNotIn(('sriov_numvfs', '0'), self.fs.events)

    def test_vf_going_up_during_unbind_stops_and_restores(self):
        def before_write(path, value):
            if path.endswith('/unbind') and value == self.fs.vfs[0]:
                self.fs.files['/sys/class/net/eth86/flags'] = '0x1003'
        self.fs.before_write = before_write
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('eth86', response.error)
        self.assert_restored()
        self.assertNotIn(('sriov_numvfs', '0'), self.fs.events)

    def test_vm_reference_appearing_after_unbind_stops_and_restores(self):
        def before_write(path, value):
            if path.endswith('/unbind') and value == self.fs.vfs[-1]:
                self.fs.domains['uuid-late'] = {'live': self.domain_xml()}
        self.fs.before_write = before_write
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('customer-vm', response.error)
        self.assert_restored()
        self.assertNotIn(('sriov_numvfs', '0'), self.fs.events)

    def test_missing_bind_attribute_blocks_before_unbind(self):
        del self.fs.files['/sys/bus/pci/drivers/txgbevf/bind']
        self.assert_blocked('no bind attribute')

    def test_numvfs_and_sysfs_link_count_mismatch_blocks(self):
        self.fs.files[PF_PATH + '/sriov_numvfs'] = '9'
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('VF topology changed', response.error)
        self.assertEqual([], self.fs.events)

    def test_wrong_pf_ownership_blocks(self):
        self.fs.links['/sys/bus/pci/devices/' + self.fs.vfs[0] + '/physfn'] = '/sys/bus/pci/devices/0000:43:00.0'
        self.assert_blocked('cannot verify PF ownership')

    def test_short_address_is_supported(self):
        self.assertTrue(self.invoke('42:00.1').success)

    def test_invalid_address_blocks(self):
        response = self.invoke('../42:00.1')
        self.assertFalse(response.success)
        self.assertIn('invalid PCI address', response.error)
        self.assertEqual([], self.fs.events)

    def test_missing_numvfs_reports_original_device(self):
        del self.fs.files[PF_PATH + '/sriov_numvfs']
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn(PF, response.error)
        self.assertIn('cannot find sriov_numvfs', response.error)

    def test_ignored_pf_disable_is_detected_and_restores_vfs(self):
        self.fs.ignore_disable = True
        response = self.invoke()
        self.assertFalse(response.success)
        self.assertIn('VFs still exist', response.error)
        self.assert_restored()


if __name__ == '__main__':
    unittest.main()
