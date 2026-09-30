"""Offline command-boundary tests; no Linux services or block devices are used.

Run with Python 3: python -B -m unittest discover -s tests/offline
    -p test_takeover_registration.py -v

Execute the actual source functions, replacing only agent/Linux boundaries.
This is not a substitute for integration tests with the installed LVM/sanlock.
"""
import ast
import hashlib
import json
import re
from pathlib import Path
import shlex
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import warnings


ROOT = Path(__file__).resolve().parents[2]
LVM_SOURCE = ROOT / 'zstacklib/zstacklib/utils/lvm.py'
LVM_TAKEOVER_SOURCE = ROOT / 'zstacklib/zstacklib/utils/lvm_takeover.py'
PLUGIN_SOURCE = ROOT / 'kvmagent/kvmagent/plugins/shared_block_plugin.py'
SOURCE = '501cda86a0864352a87845dc00db52fd'
TARGET = '037f0161311847179f1a9c3d7396d600'


def load_functions(path, names, namespace):
    source = path.read_text(encoding='utf-8')
    if path == PLUGIN_SOURCE:
        # Unrelated plugin methods contain Python 2 literals (e.g. 0L).
        start = source.index('    def do_takeover(')
        end = source.index('\n    @', start)
        source = '\n' * source[:start].count('\n') + textwrap.dedent(source[start:end])
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and names(node.name):
            node.decorator_list = []
            code = ast.Module(body=[node], type_ignores=[])
            exec(compile(ast.fix_missing_locations(code), str(path), 'exec'), namespace)


class CopyDisks:
    def __init__(self, source_visible=False, paths=None):
        self.paths = paths or ['/dev/mapper/copy1']
        self.wwids = ['copy%d' % (i + 1) for i in range(len(self.paths))]
        self.name = SOURCE
        self.internal_uuid = 'original-lvm-id'
        self.source_visible = source_visible
        self.lock_type = 'sanlock'
        self.lock_lv = True
        self.tags = ['zs::sharedblock::init::old-host']
        self.active = False
        self.commands = []
        self.imports = 0
        self.lock_creations = 0
        self.fail_at = None
        self.lock_adding = False
        self.lvmlockd_ready = True
        self.global_lock_ready = source_visible
        self.global_lock_checks = 0
        self.events = []
        self.lock_maps = {}
        self.removed_maps = []
        self.status_failure = False

    def records(self, tag):
        result = [dict(vgUuid=self.name, diskCount=len(self.wwids), disk={'wwid': w})
                  for w in self.wwids if any(t.startswith(tag) for t in self.tags)]
        if self.source_visible and 'zs::sharedblock::init::source-host'.startswith(tag):
            result.append(dict(vgUuid=SOURCE, diskCount=1, disk={'wwid': 'source'}))
        return result

    def execute(self, command):
        self.commands.append(command)
        argv = shlex.split(command)
        if '--devices' in argv:
            devices = argv[argv.index('--devices') + 1].split(',')
            if set(devices) != set(self.paths):
                raise AssertionError('command escaped the selected copy devices: ' + command)
        if 'vgimportclone' in argv:
            if self.name + '-lvmlock' in self.lock_maps and not self.source_visible:
                return 5, '', 'Device or resource busy: stale lvmlock mapping'
            if self.active:
                return 5, '', 'selected device has active LVs'
            self.name = argv[argv.index('--basevgname') + 1]
            self.imports += 1
            self.internal_uuid = 'imported-%d' % self.imports
            self.lock_type = 'none'
        elif 'vgs' in argv:
            if '--reportformat' in argv:
                return 0, json.dumps({'report': [{'vg': [{
                    'vg_name': self.name, 'vg_uuid': self.internal_uuid,
                    'vg_lock_type': self.lock_type, 'vg_tags': ','.join(self.tags)
                }]}]}), ''
            return 0, self.lock_type, ''
        elif 'lvs' in argv:
            if 'lv_name,lv_active' in argv:
                return 0, ('[lvmlock] ' + ('active' if self.active else 'inactive')) if self.lock_lv else '', ''
            if 'lv_active' in argv:
                return 0, 'active' if self.active else '', ''
            return (0 if self.lock_lv else 5), '', ''
        elif 'lvremove' in argv:
            if self.active:
                return 5, '', 'lock LV is in use'
            if self.name + '/lvmlock' not in argv:
                raise AssertionError('attempt to delete a business LV')
            self.lock_lv = False
        elif 'vgrename' in argv:
            if self.fail_at == 'rename':
                return 5, '', 'injected rename failure'
            if not self.global_lock_ready and '--nolocking' not in argv:
                return 5, '', 'Global lock failed: check that global lockspace is started'
            self.name = argv[argv.index('--devices') - 1]
        elif '--locktype' in argv:
            if self.fail_at == 'locktype':
                return 5, '', 'injected lock conversion failure'
            if '--nolocking' in argv:
                return 0, '', 'Using lock type requires lvmlockd.'
            if not self.global_lock_ready and 'skipgl' not in argv:
                return 0, '', 'Global lock failed: check that global lockspace is started'
            self.lock_type = 'sanlock'
            self.lock_lv = True
            self.lock_creations += 1
        elif '--addtag' in argv:
            if self.fail_at == 'tag':
                return 5, '', 'injected marker failure'
            self.tags.append(argv[argv.index('--addtag') + 1])
        elif '--deltag' in argv:
            self.tags.remove(argv[argv.index('--deltag') + 1])
        elif '--lock-start' in argv:
            if self.fail_at == 'start':
                return 5, '', 'injected lock start failure'
            self.active = True
        elif 'sanlock' in argv:
            if self.status_failure:
                return 1, '', 'cannot query sanlock'
            return 0, ('s lvm_%s:1:/dev/mapper/%s-lvmlock:0%s' % (
                self.name, self.name, ' ADD' if self.lock_adding else '')) if self.active else '', ''
        elif 'lvmlockctl' in argv:
            if self.fail_at == 'lvmlockctl':
                return 1, '', 'cannot query lvmlockd'
            return 0, ('VG ' + self.name + ' lock_type=sanlock') if self.lvmlockd_ready and self.active else '', ''
        elif 'dmsetup' in argv:
            if self.fail_at == 'dm-' + argv[argv.index('dmsetup') + 1]:
                return 1, '', 'injected dmsetup failure'
            if 'info' in argv:
                return 0, '\n'.join('%s|%s' % item for item in sorted(self.lock_maps.items())), ''
            if 'remove' in argv:
                name = argv[-1]
                if self.lock_maps.get(name, 0):
                    return 1, '', 'Device or resource busy'
                self.removed_maps.append(name)
                self.lock_maps.pop(name, None)
                return 0, '', ''
            raise AssertionError('unexpected dmsetup command: ' + command)
        else:
            raise AssertionError('unexpected command: ' + command)
        return 0, '', ''


class TestTakeoverRegistration(unittest.TestCase):
    def test_vg_discovery_helpers_describe_their_actual_contract(self):
        tree = ast.parse(LVM_SOURCE.read_text(encoding='utf-8'))
        functions = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }

        self.assertTrue({
            '_get_block_device_node_path', '_parse_pvck_vg_summary',
        } <= functions)
        self.assertFalse({
            '_block_device_path', '_parse_pvck_vg_header',
        } & functions)

    def test_takeover_helpers_live_in_dedicated_logged_module(self):
        self.assertTrue(LVM_TAKEOVER_SOURCE.exists())

        takeover_tree = ast.parse(LVM_TAKEOVER_SOURCE.read_text(encoding='utf-8'))
        takeover_functions = {
            node.name: node for node in ast.walk(takeover_tree)
            if isinstance(node, ast.FunctionDef)
        }
        expected = {
            'get_vg_for_takeover', 'import_vg_clone', 'ensure_vg_sanlock',
            '_takeover_lvm', '_takeover_vg_info', 'prepare_takeover_vg',
            'start_takeover_vg_lock', 'update_imported_vg_tag',
        }
        self.assertTrue(expected <= set(takeover_functions))

        lvm_tree = ast.parse(LVM_SOURCE.read_text(encoding='utf-8'))
        lvm_functions = {
            node.name for node in ast.walk(lvm_tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertFalse(expected & lvm_functions)

        logged_stages = {
            name for name, node in takeover_functions.items()
            if any('bash' in ast.dump(item) and 'in_bash' in ast.dump(item)
                   for item in node.decorator_list)
        }
        self.assertEqual({
            'prepare_takeover_vg', 'start_takeover_vg_lock',
            'update_imported_vg_tag',
        }, logged_stages & expected)

    def setup_runtime(self, disks, target=TARGET):
        self.disks = disks
        self.legacy = []
        def old_operation(name):
            def run(*args, **kwargs):
                self.legacy.append(name)
                disks.events.append(name)
                if name == 'start':
                    disks.active = True
                elif name == 'drop':
                    disks.active = False
            return run
        linux = SimpleNamespace(shellquote=shlex.quote, get_physical_disk=lambda: [],
                                get_hostname=lambda: 'test-host')
        sanlock = SimpleNamespace(get_lockspace=lambda vg: 'lvm_' + vg if disks.active else None)
        namespace = dict(get_all_vgs=disks.records,
                         vg_exists=lambda vg: vg == disks.name or
                         (disks.source_visible and vg == SOURCE),
                         lvm_cmd_timeout_with_locking=210,
                         COMMON_TAG='zs::sharedblock')
        def check_gl_lock():
            disks.global_lock_checks += 1
            disks.global_lock_ready = True
            disks.events.append('global-lock')
        namespace['check_gl_lock'] = check_gl_lock
        self.lvm = SimpleNamespace(**namespace)
        for name in ('config_lvm', 'start_lock_service', 'check_missing_pv',
                     'update_lockspace_io_timeout_if_need', 'log_vg_lock_diagnostics'):
            setattr(self.lvm, name, Mock())
        self.lvm.get_retry_times_for_checking_vg_lockspace = lambda: 1
        self.lvm.start_vg_lock = old_operation('start')
        self.lvm.reset_sanlock_lockspace = old_operation('reset')
        self.lvm.reset_pv_uuids = old_operation('pv-reset')
        self.lvm.update_vg_tag = old_operation('tag-update')
        self.lvm.drop_vg_lock = old_operation('drop')
        self.lvm.check_stuck_vglk_and_gllk = old_operation('host-lock-cleanup')
        self.lvm.remove_device_map_for_vg = old_operation('remove-maps')
        def rename_vg(old, new):
            if not disks.global_lock_ready:
                raise Exception('Global lock failed: check that global lockspace is started')
            disks.events.append('rename')
            disks.name = new
        self.lvm.rename_vg = rename_vg
        takeover_namespace = dict(
            bash=SimpleNamespace(bash_roe=disks.execute), linux=linux, sanlock=sanlock,
            logger=Mock(), simplejson=json, hashlib=hashlib, re=re, lvm=self.lvm)
        load_functions(LVM_TAKEOVER_SOURCE, lambda n: n.startswith('_takeover_') or n in (
            'get_vg_for_takeover', 'import_vg_clone', 'ensure_vg_sanlock',
            'prepare_takeover_vg', 'start_takeover_vg_lock',
            'update_imported_vg_tag'), takeover_namespace)
        self.lvm_takeover = SimpleNamespace(**takeover_namespace)
        plugin_ns = dict(lvm=self.lvm, lvm_takeover=self.lvm_takeover,
                         sanlock=sanlock, linux=linux, logger=Mock(), time=time,
                         kvmagent=SimpleNamespace(get_host_os_type=lambda: 'linux'),
                         INIT_TAG='zs::sharedblock::init', DEFAULT_SANLOCK_LV_SIZE=64,
                         TakeoverRsp=lambda: SimpleNamespace(success=True),
                         jsonobject=SimpleNamespace(dumps=lambda x: x),
                         CheckDisk=lambda w: SimpleNamespace(get_path=lambda **kw: disks.paths[disks.wwids.index(w)]))
        load_functions(PLUGIN_SOURCE, lambda n: n == 'do_takeover', plugin_ns)
        self.logger = plugin_ns['logger']
        self.takeover = plugin_ns['do_takeover']
        self.plugin = SimpleNamespace(clear_stalled_qmp_socket=Mock())
        self.cmd = SimpleNamespace(vgUuid=target, hostId='1', hostUuid='test-host',
                                   sharedBlockUuids=disks.wwids, allSharedBlockUuids=disks.wwids,
                                   enableLvmetad=False, ioTimeout=40)

    def run_takeover(self):
        return self.takeover(self.plugin, self.cmd)

    def stage_logs(self):
        return [call.args[0] for call in self.logger.info.call_args_list
                if 'takeover[' in call.args[0]]

    def test_success_logs_each_stage_and_target_in_execution_order(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.run_takeover()
        stages = self.stage_logs()
        self.assertEqual(14, len(stages))
        for index, line in enumerate(stages):
            self.assertIn('takeover[%d/7]' % (index // 2 + 1), line)
            self.assertIn(TARGET, line)
            self.assertIn('begin' if index % 2 == 0 else 'done', line)
        self.lvm.update_lockspace_io_timeout_if_need.assert_called_once_with(TARGET, 40)
        self.assertTrue(any(tag.startswith('zs::sharedblock::init::test-host::')
                            for tag in self.disks.tags))

    def test_lock_failure_logs_started_stage_without_false_completion(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'start'
        with self.assertRaisesRegex(Exception, 'injected lock start failure'):
            self.run_takeover()
        stages = self.stage_logs()
        self.assertEqual(9, len(stages))
        self.assertIn('takeover[5/7]', stages[-1])
        self.assertIn('begin', stages[-1])
        self.lvm.check_missing_pv.assert_not_called()
        self.assertFalse(any('takeover completed' in call.args[0]
                             for call in self.logger.info.call_args_list))

    def test_final_tag_failure_cannot_log_success(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.lvm_takeover.update_imported_vg_tag = Mock(side_effect=Exception('tag write failed'))
        with self.assertRaisesRegex(Exception, 'tag write failed'):
            self.run_takeover()
        stages = self.stage_logs()
        self.assertEqual(13, len(stages))
        self.assertIn('takeover[7/7]', stages[-1])
        self.assertIn('begin', stages[-1])
        self.assertFalse(any('takeover completed' in call.args[0]
                             for call in self.logger.info.call_args_list))

    def test_isolated_preserve_name_retry_reuses_imported_identity(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.assertTrue(self.run_takeover().success)
        self.assertTrue(self.run_takeover().success)
        self.assertEqual(SOURCE, self.disks.name)
        self.assertEqual(1, self.disks.imports)
        self.assertEqual('imported-1', self.disks.internal_uuid)
        self.assertEqual(1, self.disks.lock_creations)
        self.assertEqual([], self.legacy)
        self.assertEqual(0, self.disks.global_lock_checks)

    def test_cross_cluster_reset_name_imports_clone_without_legacy_takeover(self):
        self.setup_runtime(CopyDisks())
        self.run_takeover()
        self.assertEqual(TARGET, self.disks.name)
        self.assertEqual(1, self.disks.imports)
        self.assertEqual('imported-1', self.disks.internal_uuid)
        self.assertEqual(1, self.disks.lock_creations)
        self.assertEqual([], self.legacy)
        self.assertEqual(0, self.disks.global_lock_checks)

    def test_same_cluster_copy_never_uses_old_lease_reset(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.run_takeover()
        self.assertEqual([], self.legacy)
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)

    def test_preserve_name_conflict_fails_without_disk_writes(self):
        self.setup_runtime(CopyDisks(source_visible=True), SOURCE)
        with self.assertRaises(Exception):
            self.run_takeover()
        self.assertFalse(any('vgimportclone' in c or 'lvremove' in c for c in self.disks.commands))

    def test_completed_takeover_does_not_import_or_rebuild_again(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.run_takeover()
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)
        self.assertEqual([], self.legacy)
        self.assertTrue(self.disks.active)

    def test_start_failure_does_not_fallback_to_direct_lease_reset(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'start'
        with self.assertRaises(Exception):
            self.run_takeover()
        self.assertEqual([], self.legacy)
        self.disks.fail_at = None
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)

    def test_retry_after_rename_failure_reuses_imported_identity(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'rename'
        with self.assertRaises(Exception):
            self.run_takeover()
        self.disks.fail_at = None
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(TARGET, self.disks.name)

    def test_cross_cluster_clone_renames_local_import_without_global_lock(self):
        self.setup_runtime(CopyDisks())
        self.run_takeover()
        rename_commands = [c for c in self.disks.commands if 'vgrename' in shlex.split(c)]
        self.assertEqual(1, len(rename_commands))
        self.assertIn('--nolocking', shlex.split(rename_commands[0]))
        self.assertEqual(0, self.disks.global_lock_checks)

    def test_every_native_write_is_scoped_to_all_selected_copy_devices(self):
        self.setup_runtime(CopyDisks(source_visible=True, paths=['/dev/mapper/copy1', '/dev/mapper/copy2']))
        self.run_takeover()
        for command in self.disks.commands:
            argv = shlex.split(command)
            if any(x in argv for x in ('vgimportclone', 'vgrename', 'vgchange', 'lvremove')):
                self.assertIn('--devices', argv, command)
                self.assertEqual(set(self.disks.paths), set(argv[argv.index('--devices') + 1].split(',')))

    def test_retry_after_import_before_marker_does_not_reimport(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'tag'
        with self.assertRaises(Exception):
            self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.disks.fail_at = None
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(TARGET, self.disks.name)

    def test_retry_after_lock_conversion_failure_does_not_reimport(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'locktype'
        with self.assertRaises(Exception):
            self.run_takeover()
        self.disks.fail_at = None
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)
        self.assertEqual([], self.legacy)

    def test_marker_copied_from_another_wwid_set_cannot_skip_import(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        old_identity = 'original-disk'.encode('utf-8')
        old_marker = 'zs::takeover::' + TARGET + '::' + hashlib.sha256(old_identity).hexdigest()
        self.disks.tags.append(old_marker)
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertNotIn(old_marker, self.disks.tags)

    def test_active_unimported_copy_fails_without_lock_reset(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.active = True
        with self.assertRaisesRegex(Exception, 'active LVs'):
            self.run_takeover()
        self.assertEqual(0, self.disks.imports)
        self.assertEqual([], self.legacy)
        self.assertFalse(any('lvremove' in c or '--locktype' in c for c in self.disks.commands))

    def test_missing_pv_is_rejected_before_any_native_write(self):
        self.setup_runtime(CopyDisks(paths=['/dev/mapper/copy1', '/dev/mapper/copy2']))
        self.cmd.sharedBlockUuids = ['copy1']
        with self.assertRaisesRegex(Exception, 'cannot find one complete VG'):
            self.run_takeover()
        self.assertEqual([], self.disks.commands)

    def test_repeated_registration_reuses_already_independent_copy(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.run_takeover()
        self.disks.active = False  # storage detached before its control-plane record is deleted
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)
        self.assertEqual([], self.legacy)

    def test_takeover_requires_no_control_plane_operation_identity(self):
        self.setup_runtime(CopyDisks())
        self.assertFalse(hasattr(self.cmd, 'registrationId'))
        self.assertTrue(self.run_takeover().success)

    def test_adding_lockspace_is_not_reported_as_takeover_success(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.lock_adding = True
        with self.assertRaisesRegex(Exception, 'lockspace is not ready'):
            self.run_takeover()
        self.assertEqual([], self.legacy)
        self.disks.lock_adding = False
        self.run_takeover()
        self.assertEqual(1, self.disks.imports)

    def test_sanlock_without_lvmlockd_lockspace_is_not_success(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.lvmlockd_ready = False
        with self.assertRaisesRegex(Exception, 'lvmlockd'):
            self.run_takeover()
        self.assertEqual([], self.legacy)

    def test_stale_lock_mapping_is_removed_before_isolated_clone_import(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.disks.lock_maps[SOURCE + '-lvmlock'] = 0
        self.run_takeover()
        self.assertEqual([SOURCE + '-lvmlock'], self.disks.removed_maps)
        self.assertTrue(self.disks.active)

    def test_same_name_source_mapping_survives_copy_registration(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.lock_maps[SOURCE + '-lvmlock'] = 1
        self.disks.lock_maps[TARGET + '-lvmlock'] = 0
        self.run_takeover()
        self.assertEqual([TARGET + '-lvmlock'], self.disks.removed_maps)
        self.assertEqual({SOURCE + '-lvmlock': 1}, self.disks.lock_maps)

    def test_open_lock_mapping_blocks_import_without_removal(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.disks.lock_maps[SOURCE + '-lvmlock'] = 1
        with self.assertRaisesRegex(Exception, 'in use'):
            self.run_takeover()
        self.assertEqual([], self.disks.removed_maps)
        self.assertEqual(0, self.disks.imports)

    def test_failed_sanlock_query_cannot_authorize_map_removal(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.disks.lock_maps[SOURCE + '-lvmlock'] = 0
        self.disks.status_failure = True
        with self.assertRaisesRegex(Exception, 'sanlock'):
            self.run_takeover()
        self.assertEqual([], self.disks.removed_maps)
        self.assertEqual(0, self.disks.imports)

    def test_only_internal_lock_map_is_removed(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.disks.lock_maps = {SOURCE + '-lvmlock': 0, SOURCE + '-volume1': 0,
                               'prefix' + SOURCE + '-lvmlock': 0}
        self.run_takeover()
        self.assertEqual([SOURCE + '-lvmlock'], self.disks.removed_maps)
        self.assertEqual(2, len(self.disks.lock_maps))

    def test_active_lock_mapping_survives_completed_retry(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.run_takeover()
        self.disks.lock_maps[TARGET + '-lvmlock'] = 1
        self.run_takeover()
        self.assertEqual([], self.disks.removed_maps)
        self.assertEqual(1, self.disks.imports)
        self.assertEqual(1, self.disks.lock_creations)

    def test_failed_lvmlockd_query_cannot_authorize_map_removal(self):
        self.setup_runtime(CopyDisks(), SOURCE)
        self.disks.lock_maps[SOURCE + '-lvmlock'] = 0
        self.disks.fail_at = 'lvmlockctl'
        with self.assertRaisesRegex(Exception, 'lvmlockd'):
            self.run_takeover()
        self.assertEqual([], self.disks.removed_maps)
        self.assertEqual(0, self.disks.imports)

    def test_device_map_command_failure_stops_before_import(self):
        for operation in ('dm-info', 'dm-remove'):
            with self.subTest(operation=operation):
                self.setup_runtime(CopyDisks(), SOURCE)
                self.disks.lock_maps[SOURCE + '-lvmlock'] = 0
                self.disks.fail_at = operation
                with self.assertRaisesRegex(Exception, 'dmsetup failure'):
                    self.run_takeover()
                self.assertEqual({SOURCE + '-lvmlock': 0}, self.disks.lock_maps)
                self.assertEqual(0, self.disks.imports)

    def test_diagnostic_failure_preserves_original_takeover_error(self):
        self.setup_runtime(CopyDisks(source_visible=True))
        self.disks.fail_at = 'start'
        self.lvm.log_vg_lock_diagnostics.side_effect = Exception('diagnostic unavailable')
        with self.assertRaisesRegex(Exception, 'injected lock start failure'):
            self.run_takeover()


if __name__ == '__main__':
    unittest.main()
