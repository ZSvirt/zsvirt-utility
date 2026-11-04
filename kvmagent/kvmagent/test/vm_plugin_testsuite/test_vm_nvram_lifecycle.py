import os
import shutil
import tempfile
import unittest
import uuid

from kvmagent.plugins import vm_plugin
from kvmagent.plugins.nvram import nvram, nvram_common
from zstacklib.utils import http, jsonobject


class Domain(object):
    def __init__(self, vm_uuid):
        self.vm_uuid = vm_uuid

    def name(self):
        return self.vm_uuid


class TestVmNvramLifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='test-vm-nvram-')
        self.addCleanup(shutil.rmtree, self.directory)
        self.vm_uuid = uuid.uuid4().hex
        self.fd_path = os.path.join(self.directory, self.vm_uuid + '.fd')
        self.host_directory = os.path.join(self.directory, 'host-files')
        self.token_path = os.path.join(self.host_directory, 'delete')
        self.contents = b'BootOrder\x00Boot0003\x00\\EFI\\debian\\grubx64.efi\x00'
        with open(self.fd_path, 'wb') as stream:
            stream.write(self.contents)

        remove_file = nvram.linux.rm_file_checked

        def remove_test_nvram(path):
            self.assertEqual('/var/lib/libvirt/qemu/nvram/%s.fd' % self.vm_uuid, path)
            remove_file(self.fd_path)

        # Redirect the fixed libvirt path while retaining real file removal.
        self.patch(nvram.linux, 'rm_file_checked', remove_test_nvram)
        self.patch(nvram_common, 'build_nvram_mount_folder_path',
                   lambda vm_uuid: os.path.join(self.directory, 'unmounted'))
        self.patch(nvram_common, 'build_nvram_delete_token_file_path',
                   lambda vm_uuid: self.token_path)
        self.extension = nvram.NvRamVmExtensions()
        self.extension.vm_uuid = self.vm_uuid
        self.plugin = vm_plugin.VmPlugin.__new__(vm_plugin.VmPlugin)

    def patch(self, target, name, replacement):
        original = getattr(target, name)
        setattr(target, name, replacement)
        self.addCleanup(setattr, target, name, original)

    def assert_nvram_preserved(self):
        self.assertTrue(os.path.isfile(self.fd_path))
        with open(self.fd_path, 'rb') as stream:
            self.assertEqual(self.contents, stream.read())

    def test_keep_default_path_nvram_preserves_boot_entries(self):
        self.extension.cleanup(keep_default_path_nvram=True)

        self.assert_nvram_preserved()

    def test_explicit_cleanup_removes_default_path_nvram(self):
        self.extension.cleanup()

        self.assertFalse(os.path.exists(self.fd_path))

    def test_stopped_event_preserves_boot_entries(self):
        self.plugin._deactivate_nvram(None, Domain(self.vm_uuid),
                                     vm_plugin.libvirt.VIR_DOMAIN_EVENT_STOPPED,
                                     0, None)

        self.assert_nvram_preserved()

    def test_stopped_event_still_marks_managed_host_files_for_collection(self):
        os.mkdir(self.host_directory)
        managed_fd = os.path.join(self.host_directory, self.vm_uuid + '.fd')
        with open(managed_fd, 'wb') as stream:
            stream.write(self.contents)

        self.plugin._deactivate_nvram(None, Domain(self.vm_uuid),
                                     vm_plugin.libvirt.VIR_DOMAIN_EVENT_STOPPED,
                                     0, None)

        self.assert_nvram_preserved()
        with open(managed_fd, 'rb') as stream:
            self.assertEqual(self.contents, stream.read())
        with open(self.token_path, 'r') as stream:
            self.assertGreater(int(stream.read().strip()), 0)

    def test_firmware_cleanup_removes_nvram_when_domain_is_absent(self):
        self.patch(vm_plugin, 'get_vm_by_uuid_no_retry', lambda *args: None)
        request = {http.REQUEST_BODY: jsonobject.dumps({'vmUuid': self.vm_uuid})}

        response = jsonobject.loads(self.plugin.clean_firmware_flash(request))

        self.assertTrue(response.success)
        self.assertFalse(os.path.exists(self.fd_path))

    def test_firmware_cleanup_keeps_nvram_when_domain_exists(self):
        self.patch(vm_plugin, 'get_vm_by_uuid_no_retry',
                   lambda *args: Domain(self.vm_uuid))
        request = {http.REQUEST_BODY: jsonobject.dumps({'vmUuid': self.vm_uuid})}

        response = jsonobject.loads(self.plugin.clean_firmware_flash(request))

        self.assertTrue(response.success)
        self.assert_nvram_preserved()


if __name__ == '__main__':
    unittest.main()
