# encoding: utf-8

import unittest

import mock
from kvmagent.plugins import host_plugin


class TestHostPluginVfioMdev(unittest.TestCase):
    def _new_plugin(self):
        plugin = host_plugin.HostPlugin()
        plugin.NVIDIA_SMI_INSTALLED = True
        return plugin

    def _new_pci_to(self):
        pci_to = host_plugin.PciDeviceTO()
        pci_to.pciDeviceAddress = "0000:3b:00.0"
        return pci_to

    def test_creatable_fails_supported_also_fails(self):
        """When both -v -c and -s fail, card does not support vGPU."""
        def fake_bash_roe(cmd):
            return 1, '', 'error'

        plugin = self._new_plugin()
        pci_to = self._new_pci_to()

        with mock.patch.object(host_plugin, 'bash_roe', side_effect=fake_bash_roe), \
                mock.patch.object(host_plugin.os.path, 'isdir', return_value=False):
            supported = plugin._get_vfio_mdev_info(pci_to)

        self.assertFalse(supported)
        self.assertEqual('', pci_to.virtStatus)

    def test_sriov_vgpu_card_no_vfs(self):
        """SR-IOV backed vGPU card: -v -c fails, -s succeeds, no VFs yet."""
        def fake_bash_roe(cmd):
            if '-v -c' in cmd:
                return 1, '', 'no creatable instances'
            if ' -s' in cmd:
                return 0, 'vGPU Type ID : 239\n  Name : GRID L20-4Q\n', ''
            return 0, '', ''

        plugin = self._new_plugin()
        pci_to = self._new_pci_to()

        with mock.patch.object(host_plugin, 'bash_roe', side_effect=fake_bash_roe), \
                mock.patch.object(host_plugin.os.path, 'isdir', return_value=False):
            supported = plugin._get_vfio_mdev_info(pci_to)

        self.assertTrue(supported)
        self.assertEqual('VFIO_MDEV_VIRTUALIZABLE', pci_to.virtStatus)

    def test_normal_card_creatable_succeeds(self):
        """Normal vGPU card: -v -c succeeds, no sysfs dirs yet."""
        def fake_bash_roe(cmd):
            if '-v -c' in cmd:
                return 0, 'Header\nvGPU Type ID : 239\n  Name : GRID L20-4Q\n', ''
            return 0, '', ''

        plugin = self._new_plugin()
        pci_to = self._new_pci_to()

        with mock.patch.object(host_plugin, 'bash_roe', side_effect=fake_bash_roe), \
                mock.patch.object(host_plugin.os.path, 'isdir', return_value=False):
            supported = plugin._get_vfio_mdev_info(pci_to)

        self.assertTrue(supported)
        self.assertEqual('VFIO_MDEV_VIRTUALIZABLE', pci_to.virtStatus)
        self.assertEqual('239', pci_to.mdevSpecifications[0]['TypeId'])
        self.assertEqual('GRID L20-4Q', pci_to.mdevSpecifications[0]['Name'])

    def test_legacy_mdev_creatable_query_fails(self):
        """Legacy mdev: supported types exist, but creatable query fails."""
        def fake_bash_roe(cmd):
            if '-v -c' in cmd:
                return 0, 'Header\nvGPU Type ID : 239\n  Name : GRID L20-4Q\n', ''
            if ' -c' in cmd:
                return 1, '', 'no creatable instances'
            return 0, '', ''

        def fake_isdir(path):
            return path.endswith('/mdev_supported_types')

        plugin = self._new_plugin()
        pci_to = self._new_pci_to()

        with mock.patch.object(host_plugin, 'bash_roe', side_effect=fake_bash_roe), \
                mock.patch.object(host_plugin.os.path, 'isdir', side_effect=fake_isdir):
            supported = plugin._get_vfio_mdev_info(pci_to)

        self.assertTrue(supported)
        self.assertEqual('VFIO_MDEV_VIRTUALIZABLE', pci_to.virtStatus)


if __name__ == "__main__":
    unittest.main()
