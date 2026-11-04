"""Exercise the real Python expansion path without connecting to a host.

The shell fixture represents an LV held SH by both migration peers. The
production callback and production LVM command construction execute together.
This is not a replacement for a live migration and device test.
"""
import ast
import os
import random
import unittest
try:
    from unittest.mock import Mock
except ImportError:
    from mock import Mock
from test_vm_persistent_definition_helpers import load_function


class ExtendError(Exception):
    @property
    def message(self):
        return str(self)


class SharedBlockMigrationExtendTest(unittest.TestCase):
    def setUp(self):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..',
                                          'zstacklib', 'zstacklib', 'utils', 'lvm.py'))
        with open(path) as source:
            tree = ast.parse(source.read())
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('extend_lv', 'subcmd')]
        self.commands = []
        self.size = 1024
        self.shell_failure = False
        self.bash = Mock(in_bash=lambda fn: fn)
        self.bash.bash_roe.side_effect = self.execute
        namespace = dict(bash=self.bash, linux=Mock(retry=lambda **kwargs: lambda fn: fn),
                         random=random, calcLvReservedSize=lambda size: size,
                         get_lv_size=lambda path: self.size,
                         lvm_cmd_timeout_with_locking=210,
                         NOLOCK_CMDS={'lvs', 'pvs', 'vgs'},
                         TIMEOUT_CMDS={'lvchange', 'lvcreate', 'lvrename', 'lvresize', 'lvextend', 'lvremove'},
                         REPAIR_LV_CMDS={'lvchange', 'lvrename', 'lvresize', 'lvextend', 'lvremove'},
                         REPAIR_VG_CMDS={'vgchange'},
                         RetryException=ExtendError, logger=Mock())
        exec(compile(tree, path, 'exec'), namespace)
        self.subcmd = namespace['subcmd']
        self.lvm = Mock(extend_lv=namespace['extend_lv'])
        self.vm = Mock()
        self.callback = load_function('extend_lv', dict(
            self=Mock(auto_extend_size=1024),
            check_lv=lambda *args: (True, 900, 1024, 10000),
            lvm=self.lvm, logger=Mock(), MAX_ACTUAL_SIZE_FACTOR=3), '_extend_sharedblock')

    def execute(self, command):
        self.commands.append(command)
        if command.split().count('--lockopt') > 1:
            return 3, '', 'Option --lockopt may not be repeated.'
        if self.shell_failure:
            return 5, '', 'storage I/O error'
        if 'shupdate,norefresh' not in command:
            return 5, '', 'LV locked in incompatible mode'
        self.size = 2048
        return 0, '', ''

    def test_migration_shared_lease_can_expand_without_ex_lock(self):
        self.callback('suspend', '/dev/vg/lv', self.vm, 'vdb')
        self.assertEqual(2048, self.size)
        self.assertEqual(1, len(self.commands))
        self.lvm.OperateLv.assert_not_called()

    def test_other_host_already_expanded_does_not_repeat_command(self):
        self.size = 2048
        self.callback('suspend', '/dev/vg/lv', self.vm, 'vdb')
        self.assertEqual([], self.commands)
        self.lvm.OperateLv.assert_not_called()

    def test_io_failure_does_not_attempt_ex_fallback(self):
        self.shell_failure = True
        self.callback('suspend', '/dev/vg/lv', self.vm, 'vdb')
        self.assertEqual(1024, self.size)
        self.assertEqual(1, len(self.commands))
        self.lvm.OperateLv.assert_not_called()

    def test_lock_options_merge_without_mutating_caller_list(self):
        options = ['shupdate', 'norefresh', 'repairlv']
        command = self.subcmd('lvextend', 17, lockopts=options)
        self.assertEqual(1, command.split().count('--lockopt'))
        self.assertEqual({'shupdate', 'norefresh', 'repairlv'}, set(command.split()[-1].split(',')))
        self.assertEqual(3, len(command.split()[-1].split(',')))
        self.assertEqual(['shupdate', 'norefresh', 'repairlv'], options)
        self.assertTrue(command.startswith('timeout -s SIGKILL 17 lvextend '))


if __name__ == '__main__':
    unittest.main()
