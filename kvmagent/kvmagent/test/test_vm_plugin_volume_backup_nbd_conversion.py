import json
import os
import shlex
import unittest

from kvmagent.plugins import imagestore


class TestVolumeBackupNbdConversion(unittest.TestCase):
    def setUp(self):
        self.client = imagestore.ImageStoreClient()
        self.command = None
        self.request = None
        self.removed = None
        self._shell_call = imagestore.shell.call
        self._remove = imagestore.linux.rm_file_force
        imagestore.shell.call = self._call
        imagestore.linux.rm_file_force = self._remove_file

    def tearDown(self):
        imagestore.shell.call = self._shell_call
        imagestore.linux.rm_file_force = self._remove
        if self.removed and os.path.exists(self.removed):
            os.remove(self.removed)

    def _call(self, command):
        self.command = command
        args = shlex.split(command)
        args_file = args[args.index('-args-json-file') + 1]
        with open(args_file) as fd:
            self.request = json.load(fd)
        return json.dumps({'installPaths': {
            'zstore://source/old': 'zstore://target/new',
        }})

    def _remove_file(self, path):
        self.removed = path
        if os.path.exists(path):
            os.remove(path)

    def test_kvmagent_delegates_backup_conversion_to_zstcli(self):
        request = {
            'taskUuid': 'task-uuid',
            'bsAgentHost': 'agent.test',
            'bsAgentPort': 8001,
            'nbdHost': 'backup.test',
            'targetChainName': 'target',
            'targetEncrypted': True,
            'encryptedDek': 'sealed-dek',
            'leafInstallPaths': ['zstore://source/old'],
        }

        install_paths = self.client.change_backup_encryption(request)

        self.assertEqual({'zstore://source/old': 'zstore://target/new'}, install_paths)
        self.assertIn(' -url agent.test:8001 bakconvert ', ' %s ' % self.command)
        self.assertIn(' -secret-channel-provider %s' % self.client.KEY_AGENT_PROVIDER, self.command)
        self.assertNotIn('sealed-dek', self.command)
        self.assertEqual(request, self.request)
        self.assertFalse(os.path.exists(self.removed))


if __name__ == '__main__':
    unittest.main()
