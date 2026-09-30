import json
import shlex
import unittest

from zstacklib.utils import bash, lvm, lvm_takeover


class TestTakeoverVg(unittest.TestCase):
    SOURCE = '501cda86a0864352a87845dc00db52fd'
    TARGET = '037f0161311847179f1a9c3d7396d600'

    def setUp(self):
        self.original_scan = lvm.get_all_vgs
        self.original_bash = bash.bash_roe
        self.commands = []

    def tearDown(self):
        lvm.get_all_vgs = self.original_scan
        bash.bash_roe = self.original_bash

    def record(self, wwid, vg=None, count=1):
        return {'vgUuid': vg or self.SOURCE, 'diskCount': count,
                'disk': {'wwid': wwid}}

    def test_selected_copy_reports_duplicate_name_even_when_first_in_lvm_view(self):
        lvm.get_all_vgs = lambda tag: [self.record('copy'), self.record('source')]
        self.assertEqual((self.SOURCE, True),
                         lvm_takeover.get_vg_for_takeover(lvm.COMMON_TAG, [' COPY ']))

    def test_unique_vg_name_does_not_report_duplicate_devices(self):
        for vg in (self.SOURCE, self.TARGET):
            lvm.get_all_vgs = lambda tag: [self.record('copy', vg)]
            self.assertEqual((vg, False),
                             lvm_takeover.get_vg_for_takeover(lvm.COMMON_TAG, ['copy']))

    def test_missing_partial_and_mixed_vg_selections_are_rejected(self):
        for records, selected in (
                ([self.record('source')], ['copy']),
                ([self.record('copy', count=2)], ['copy']),
                ([self.record('copy'), self.record('other', self.TARGET)], ['copy', 'other']),
                ([self.record('copy')], [])):
            lvm.get_all_vgs = lambda tag: records
            with self.assertRaisesRegexp(Exception, 'cannot find one complete VG'):
                lvm_takeover.get_vg_for_takeover(lvm.COMMON_TAG, selected)

    def test_complete_multi_pv_copy_is_matched_separately_from_source(self):
        lvm.get_all_vgs = lambda tag: [self.record(w, count=2)
                                      for w in ('a1', 'a2', 'b1', 'b2')]
        self.assertEqual((self.SOURCE, True),
                         lvm_takeover.get_vg_for_takeover(lvm.COMMON_TAG, ['b1', 'b2']))

    def test_import_command_is_restricted_to_selected_devices(self):
        def execute(command):
            self.commands.append(command)
            return 0, '', ''
        bash.bash_roe = execute
        lvm_takeover.import_vg_clone(self.TARGET, ['/dev/mapper/copy2', '/dev/mapper/copy1'])
        self.assertEqual(1, len(self.commands))
        self.assertIn('vgimportclone', self.commands[0])
        argv = shlex.split(self.commands[0])
        self.assertEqual('/dev/mapper/copy1,/dev/mapper/copy2', argv[argv.index('--devices') + 1])
        self.assertEqual(self.TARGET, argv[argv.index('--basevgname') + 1])
        self.assertEqual(['/dev/mapper/copy1', '/dev/mapper/copy2'], argv[-2:])
        with self.assertRaisesRegexp(Exception, 'without target disk paths'):
            lvm_takeover.import_vg_clone(self.TARGET, [])
        self.assertEqual(1, len(self.commands))

    def test_clone_lock_conversion_removes_only_internal_lock_lv(self):
        lock_types = iter(['none', 'sanlock'])
        def execute(command):
            self.commands.append(command)
            if '--reportformat json' in command:
                return 0, self.vg_info(next(lock_types)), ''
            if 'lv_name,lv_active' in command:
                return 0, '[lvmlock] inactive\nvolume1 inactive', ''
            return 0, '', ''
        bash.bash_roe = execute
        lvm_takeover.ensure_vg_sanlock(self.TARGET, ['/dev/mapper/copy'])
        removals = [c for c in self.commands if 'lvremove ' in c]
        self.assertEqual(1, len(removals))
        self.assertIn(self.TARGET + '/lvmlock', shlex.split(removals[0]))
        for command in self.commands:
            argv = shlex.split(command)
            self.assertEqual('/dev/mapper/copy', argv[argv.index('--devices') + 1])
        self.assertTrue(any('--locktype sanlock' in c for c in self.commands))
        self.assertFalse(any(self.SOURCE in c for c in self.commands))

    def test_clone_lock_conversion_keeps_lvmlockd_without_existing_global_lock(self):
        state = {'lock_type': 'none'}

        def execute(command):
            self.commands.append(command)
            if '--reportformat json' in command:
                return 0, self.vg_info(state['lock_type']), ''
            if 'lv_name,lv_active' in command:
                return 0, '[lvmlock] inactive', ''
            if 'lvremove ' in command:
                return 0, '', ''
            if '--locktype sanlock' in command:
                if '--nolocking' in shlex.split(command):
                    return 0, '', 'Using lock type requires lvmlockd.'
                if 'skipgl' not in shlex.split(command):
                    return 0, '', 'Global lock failed: check that global lockspace is started'
                state['lock_type'] = 'sanlock'
                return 0, '', ''
            raise AssertionError('unexpected command: ' + command)

        bash.bash_roe = execute
        lvm_takeover.ensure_vg_sanlock(self.TARGET, ['/dev/mapper/copy'])
        self.assertEqual('sanlock', state['lock_type'])

    def test_existing_sanlock_is_read_only_and_unknown_lock_type_is_rejected(self):
        for lock_type in ('sanlock', 'dlm'):
            self.commands = []
            def execute(command):
                self.commands.append(command)
                return 0, self.vg_info(lock_type), ''
            bash.bash_roe = execute
            if lock_type == 'sanlock':
                lvm_takeover.ensure_vg_sanlock(self.TARGET, ['/dev/mapper/copy'])
            else:
                with self.assertRaisesRegexp(Exception, 'unexpected lock type'):
                    lvm_takeover.ensure_vg_sanlock(self.TARGET, ['/dev/mapper/copy'])
            self.assertEqual(1, len(self.commands))

    def vg_info(self, lock_type):
        return json.dumps({'report': [{'vg': [{'vg_name': self.TARGET,
                           'vg_uuid': 'lvm-id', 'vg_lock_type': lock_type, 'vg_tags': ''}]}]})


if __name__ == '__main__':
    unittest.main()
