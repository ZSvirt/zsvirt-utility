"""Device-free tests of the real recovery helpers.

Load selected definitions without importing Linux-only agent dependencies. Shell,
device and host responses are fixtures; the recovery decisions execute the actual
production function/class bodies. No sanlock, LVM or block device is accessed.
"""
import ast
import errno
import json
import logging
import os
import re
import struct
import threading
import unittest
import weakref
try:
    from unittest.mock import Mock
except ImportError:
    from mock import Mock


def identity(func):
    return func


def load_definitions(filename, names, namespace):
    root = os.environ.get('ZSV_LOCK_TEST_SOURCE_ROOT') or os.path.join(
        os.path.dirname(__file__), '..', '..', 'utils')
    with open(os.path.join(root, filename)) as source:
        tree = ast.parse(source.read())
    tree.body = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(tree, filename, 'exec'), namespace)
    return namespace


class RecoveryHelpersTest(unittest.TestCase):
    def setUp(self):
        self.bash = Mock()
        self.bash.in_bash = identity
        self.linux = Mock()
        self.linux.retry = lambda *args, **kwargs: identity
        self.linux.ignoreerror = identity
        self.linux.shellquote = lambda value: "'%s'" % value
        self.sanlock = load_definitions('sanlock.py', {
            'Resource', 'get_lockspace_sector_size', 'get_sector_size',
            'sector_size_to_align_size', 'direct_init_resource', 'is_lease_corrupted',
            'is_vglk_lvb_invalid', 'repair_vglk_metadata',
        }, dict(bash=self.bash, linux=self.linux, re=re, struct=struct, os=Mock(),
                ignoreerror=identity, lock=Mock(), logger=logging.getLogger(__name__),
                sizeunit=type('Size', (), {'Byte': type('Byte', (), {
                    'toMegaByte': staticmethod(lambda value: value // (1024 ** 2))})}),
                SECTOR_SIZE_512=512, SECTOR_SIZE_4K=4096,
                SMALL_ALIGN_SIZE=1024 ** 2, BIG_ALIGN_SIZE=8 * 1024 ** 2,
                GLLK_BEGIN=65, VGLK_BEGIN=66, sector_size_cache={},
                LEASE_CORRUPTED_ERR=(-222, -223, -224, -225, -226, -227, -229, -214),
                VAL_BLK_VERSION=0x0101, VBF_REMOVED=1))
        self.api = type('Sanlock', (), {})()
        for name in ('Resource', 'sector_size_to_align_size'):
            setattr(self.api, name, self.sanlock[name])
        self.api.get_sector_size = Mock(return_value=512)
        self.api.direct_dump_resource = Mock()
        self.api.direct_init_resource = Mock(return_value=0)
        self.api.get_host_status = Mock()
        self.api.check_host_liveness = Mock()
        self.lvm = load_definitions('lvm.py', {
            '_need_retry_active_lv', 'subcmd', 'list_lvs_expected_devices',
            'list_dm_actual_devices', 'find_mismatched_lvs', 'refresh_mismatched_lvs', 'LvmlockdStatus',
        }, dict(sanlock=self.api, bash=self.bash, linux=self.linux, re=re,
                simplejson=json, errno=errno, logger=logging.getLogger(__name__),
                get_lv_attr=Mock(return_value={'lv_uuid': 'volume-uuid', 'lv_lockargs': '1.0.0:73400320'}),
                get_vg_uuid=Mock(return_value='vg'),
                get_lockspace=Mock(return_value='lvm_vg:36:/dev/mapper/vg-lvmlock:0'),
                lvm_cmd_timeout_with_locking=210,
                NOLOCK_CMDS={'pvs', 'vgs', 'lvs'},
                TIMEOUT_CMDS={'lvchange', 'lvcreate', 'lvrename', 'lvresize', 'lvextend', 'lvremove'},
                REPAIR_LV_CMDS={'lvchange', 'lvrename', 'lvresize', 'lvextend', 'lvremove'},
                REPAIR_VG_CMDS={'vgchange'}))
        self.bash.bash_r.return_value = 1  # no local client currently owns the lease

    def function(self, namespace, name):
        self.assertIn(name, namespace, 'missing recovery helper: ' + name)
        return namespace[name]

    def resource(self, owner=36, timestamp=1906629, shared=None, uuid='volume-uuid'):
        value = '73400320 lvm_vg %s %s %s 1 1' % (uuid, timestamp, owner)
        for host in shared or []:
            value += '\n%s SH' % host
        self.api.direct_dump_resource.return_value = (0, value, '')

    def retry(self):
        return self.lvm['_need_retry_active_lv'](('/dev/vg/lv',), Exception('LV locked by other host'))

    def test_sector_selection_512n_512e_4kn_and_incomplete_info(self):
        select = self.function(self.sanlock, 'get_lockspace_sector_size')
        for logical, physical, expected in [(512, 512, 512), (512, 4096, 4096),
                                            (4096, 4096, 4096), (512, 0, 512), (0, 4096, 4096)]:
            self.linux.get_dev_logical_sector_size.return_value = logical
            self.linux.get_dev_physical_sector_size.return_value = physical
            self.assertEqual(expected, select('vg'))
        self.linux.get_dev_logical_sector_size.return_value = 0
        self.linux.get_dev_physical_sector_size.return_value = 8192
        with self.assertRaises(Exception):
            select('vg')

    def test_saved_layout_takes_precedence_over_device_geometry(self):
        get_sector = self.function(self.sanlock, 'get_sector_size')
        self.sanlock['read_lockspace_metadata_from_backup'] = Mock(return_value={'sector_size': 512})
        self.sanlock['get_lockspace_sector_size'] = Mock(return_value=4096)
        self.assertEqual(512, get_sector('vg'))
        self.sanlock['get_lockspace_sector_size'].assert_not_called()

    def test_direct_init_uses_explicit_layout(self):
        init = self.function(self.sanlock, 'direct_init_resource')
        self.sanlock['get_sector_size'] = Mock(return_value=4096)
        init('lvm_vg:LV:/dev/mapper/vg-lvmlock:73400320', 'vg')
        self.assertIn('-A 8M -Z 4096', self.bash.bash_r.call_args[0][0])

    def test_io_errors_are_not_lease_corruption(self):
        corrupted = self.function(self.sanlock, 'is_lease_corrupted')
        for code in (-5, -202, 0):
            self.assertFalse(corrupted(code))
        self.assertTrue(corrupted(-222))

    def test_vglk_repair_requires_corruption_and_lvm_repair_result(self):
        repair = self.function(self.sanlock, 'repair_vglk_metadata')
        class AcquiredLock(object):
            acquired = True
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        self.sanlock['lock'].NonBlockNamedLock.return_value = AcquiredLock()
        self.sanlock['get_sector_size'] = Mock(return_value=4096)
        init = self.sanlock['direct_init_resource'] = Mock()
        for code in (-5, -202, 0):
            self.bash.bash_o.return_value = 'read done %s' % code
            repair('vg')
        init.assert_not_called()
        self.bash.bash_o.side_effect = ['read done -222', 'repaired by lvm']
        repair('vg')
        init.assert_not_called()
        self.bash.bash_o.side_effect = ['read done -222', 'sanlock lease needs repair']
        repair('vg')
        init.assert_called_once_with('lvm_vg:VGLK:/dev/mapper/vg-lvmlock:553648128', 'vg')

    def test_nonblocking_lock_does_not_wait_or_release_another_owner(self):
        root = os.environ.get('ZSV_LOCK_TEST_SOURCE_ROOT') or os.path.join(
            os.path.dirname(__file__), '..', '..', 'utils')
        with open(os.path.join(root, 'lock.py')) as source:
            text = source.read()
        self.assertIn('class NonBlockNamedLock', text)
        # The unrelated FileLock class uses legacy Python 2 octal syntax.
        get_lock = text[text.index('def _get_lock('):text.index('class NamedLock(')]
        start = text.index('class NonBlockNamedLock(')
        end = text.find('\ndef ', start)
        nonblock = text[start:] if end < 0 else text[start:end]
        namespace = dict(_internal_lock=threading.RLock(), _locks=weakref.WeakValueDictionary(),
                         threading=threading, logger=logging.getLogger(__name__))
        exec(compile(get_lock + nonblock, 'lock.py', 'exec'), namespace)
        lock_type = namespace['NonBlockNamedLock']
        result = []
        def contender():
            with lock_type('recovery') as gate:
                result.append(gate.acquired)
        with lock_type('recovery') as held:
            self.assertTrue(held.acquired)
            worker = threading.Thread(target=contender)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive(), 'contender blocked on recovery lock')
            self.assertEqual([False], result)
        with lock_type('recovery') as acquired:
            self.assertTrue(acquired.acquired)

    def test_subcmd_preserves_timeouts_and_scopes_repair_options(self):
        self.assertEqual('timeout -s SIGKILL 17 lvchange --lockopt repairlv', self.lvm['subcmd']('lvchange', 17))
        self.assertEqual('vgchange --lockopt repair', self.lvm['subcmd']('vgchange'))
        self.assertEqual('lvs --nolocking -t', self.lvm['subcmd']('lvs'))
        self.assertNotIn('repairlv', self.lvm['subcmd']('lvcreate'))

    def test_local_orphan_repaired_at_exact_offset_with_4k_lease(self):
        self.resource()
        self.api.get_sector_size.return_value = 4096
        self.assertTrue(self.retry())
        self.api.direct_dump_resource.assert_called_once_with('/dev/mapper/vg-lvmlock', 73400320, size=8 * 1024 ** 2)
        self.api.direct_init_resource.assert_called_once_with('lvm_vg:volume-uuid:/dev/mapper/vg-lvmlock:73400320', 'vg')

    def test_no_owner_retries_without_initializing_lease(self):
        self.resource(timestamp=0)
        self.assertTrue(self.retry())
        self.api.direct_init_resource.assert_not_called()

    def test_in_use_or_wrong_resource_is_never_initialized(self):
        self.resource()
        self.bash.bash_r.return_value = 0
        self.assertFalse(self.retry())
        self.bash.bash_r.return_value = 1
        self.resource(uuid='another-volume')
        self.assertFalse(self.retry())
        self.api.direct_init_resource.assert_not_called()

    def test_multiple_shared_owners_are_never_initialized(self):
        self.resource(timestamp=0, shared=[36, 37])
        self.assertFalse(self.retry())
        self.api.direct_init_resource.assert_not_called()
        self.api.check_host_liveness.assert_not_called()

    def test_remote_owner_retries_only_after_proven_dead(self):
        self.resource(owner=37)
        status = self.api.get_host_status.return_value
        status.get_io_timeout.return_value = 40
        status.get_last_check.return_value = 200
        status.get_last_live.return_value = 50
        self.api.check_host_liveness.return_value = True
        self.assertFalse(self.retry())
        self.api.check_host_liveness.return_value = False
        self.assertTrue(self.retry())
        self.api.check_host_liveness.side_effect = Exception('unknown host status')
        self.assertFalse(self.retry())
        status.get_last_live.return_value = 180
        self.api.check_host_liveness.reset_mock()
        self.assertFalse(self.retry())
        self.api.check_host_liveness.assert_not_called()
        self.api.direct_init_resource.assert_not_called()

    def test_failed_lease_read_fails_closed(self):
        self.api.direct_dump_resource.return_value = (1, '', 'I/O error')
        self.assertFalse(self.retry())
        self.api.direct_init_resource.assert_not_called()

    def test_lvmlockctl_enospc_marks_service_failed(self):
        self.bash.bash_roe.return_value = (0, 'result -28\n', '')
        self.assertTrue(self.lvm['LvmlockdStatus']().failed)

    def test_pvs_parser_includes_lvmlock_and_escapes_dm_names(self):
        query = self.function(self.lvm, 'list_lvs_expected_devices')
        self.bash.bash_roe.return_value = (0, json.dumps({'report': [{'pvseg': [
            {'vg_name': 'vg-test', 'lv_name': 'lvmlock', 'pv_name': '/dev/dm-1',
             'pv_major': '253', 'pv_minor': '1', 'segtype': 'linear'}]}]}), '')
        entry = query('vg-test')['vg--test-lvmlock']
        self.assertEqual({'253:1': '/dev/dm-1'}, entry['expected_devices'])
        self.assertFalse(entry['incomplete'])

    def test_dm_parser_marks_non_linear_mapping_incomplete(self):
        query = self.function(self.lvm, 'list_dm_actual_devices')
        self.bash.bash_roe.return_value = (0, 'vg-lv,,,rw,0 8192 linear 253:2 0;vg-raid,,,rw,0 8192 striped 2 64 253:3 0;', '')
        result = query()
        self.assertIn('vg-lv', result)
        self.assertIn('vg-raid', result)
        self.assertEqual(({'253:2'}, True), result['vg-lv'])
        self.assertFalse(result['vg-raid'][1])

    def test_refresh_only_stale_complete_linear_mappings(self):
        refresh = self.function(self.lvm, 'refresh_mismatched_lvs')
        lvs = {}
        for name in ('stale', 'current', 'incomplete', 'striped'):
            lvs['vg-' + name] = dict(path='vg/' + name, expected_devices={'253:1': '/dev/dm-1'},
                                    segtypes={'striped'} if name == 'striped' else {'linear'},
                                    incomplete=name == 'incomplete')
        tables = {name: ({'253:2'}, True) for name in lvs}
        tables['vg-current'] = ({'253:1'}, True)
        self.lvm['list_lvs_expected_devices'] = Mock(return_value=lvs)
        self.lvm['list_dm_actual_devices'] = Mock(return_value=tables)
        self.bash.bash_roe.return_value = (0, '', '')
        refresh('vg')
        self.bash.bash_roe.assert_called_once_with(
            "timeout -s SIGKILL 210 lvchange --lockopt repairlv --refresh 'vg/stale'")


if __name__ == '__main__':
    unittest.main()
