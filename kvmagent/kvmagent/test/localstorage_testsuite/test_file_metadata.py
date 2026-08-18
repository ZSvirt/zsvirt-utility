import json
import os
import shutil
import tempfile
import threading
import time
from unittest import TestCase

from kvmagent.test.utils import pytest_utils
from kvmagent.test.utils.stub import *

from zstacklib.utils import bash, file_metadata_handler, linux
from zstacklib.utils.file_metadata_handler import (
    FileBasedMetadataHandler,
    qcow2_prefix_rebase_backing_files,
    _is_imagecache_path,
    _get_rebase_lock_path,
    _validate_metadata_path,
)
from zstacklib.utils.vm_metadata_handler import StaleMetadataGeneration

PKG_NAME = __name__

__ENV_SETUP__ = {
    'self': {}
}


# ---- helpers ----------------------------------------------------------------

def _qemu_img_create(path, size='64M', backing=None, fmt='qcow2'):
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    if backing:
        bash.bash_errorout(
            'qemu-img create -f %s -b %s -F qcow2 %s' % (fmt, backing, path))
    else:
        bash.bash_errorout(
            'qemu-img create -f %s %s %s' % (fmt, path, size))


def _get_backing(path):
    return linux.qcow2_get_backing_file(path)


# #############################################################################
# TestFileMetadataApi
# #############################################################################

class TestFileMetadata(TestCase):

    @classmethod
    def setUpClass(cls):
        return

    def setUp(self):
        self.handler = FileBasedMetadataHandler()
        self.tmpdir = tempfile.mkdtemp(prefix='vmmeta_api_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers --------------------------------------------------------------

    def _meta_path(self, vm_uuid):
        return os.path.join(self.tmpdir, vm_uuid + '.vmmeta')

    def _clean_tmpdir(self):
        for root, dirs, files in os.walk(self.tmpdir, topdown=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    os.remove(fp)
                except OSError:
                    pass
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    os.rmdir(dp)
                except OSError:
                    pass

    # == write_vm_metadata ====================================================

    def _test_write_creates_file_and_stores_data(self):
        vm_uuid = 'a' * 32
        meta_path = self._meta_path(vm_uuid)
        payload = '{"volumes":["vol-1"]}'

        self.handler._do_write(
            meta_path, payload,
            vmUuid=vm_uuid, vmName='test-vm',
            vmCategory='AppCenter', architecture='x86_64',
            schemaVersion='1')

        self.assertTrue(os.path.isfile(meta_path),
                        "metadata file should be created")
        result = self.handler._do_get(meta_path)
        self.assertEqual(result['metadata'], payload)

    def _test_write_creates_parent_dir(self):
        nested = os.path.join(self.tmpdir, 'a', 'b', 'c',
                              'a' * 32 + '.vmmeta')
        self.handler._do_write(
            nested, '{}',
            vmUuid='a' * 32, vmName='', vmCategory='',
            architecture='', schemaVersion='')
        self.assertTrue(os.path.isfile(nested))

    def _test_write_with_vm_summary(self):
        vm_uuid = 'b' * 32
        meta_path = self._meta_path(vm_uuid)

        self.handler._do_write(
            meta_path, '{"test":1}',
            vmUuid=vm_uuid, vmName='summary-vm',
            vmCategory='AppCenter', architecture='aarch64',
            schemaVersion='3')

        summary_path = meta_path + '.summary'
        self.assertTrue(os.path.isfile(summary_path),
                        ".summary sidecar should be created")
        with open(summary_path, 'r') as f:
            summary = json.loads(f.read())
        self.assertEqual(summary['vmUuid'], vm_uuid)
        self.assertEqual(summary['vmName'], 'summary-vm')
        self.assertEqual(summary['vmCategory'], 'AppCenter')
        self.assertEqual(summary['architecture'], 'aarch64')
        self.assertEqual(summary['schemaVersion'], '3')

    def _test_write_without_vm_uuid_uses_path_derived_uuid_for_summary(self):
        """When vmUuid is empty, summary should still be written using UUID
        derived from metadataPath (not removed)."""
        vm_uuid = 'b1' * 16
        meta_path = self._meta_path(vm_uuid)
        summary_path = meta_path + '.summary'

        self.handler._do_write(
            meta_path, '{}',
            vmUuid=vm_uuid, vmName='vm1',
            vmCategory='', architecture='',
            schemaVersion='')
        self.assertTrue(os.path.isfile(summary_path))

        # Re-write without vmUuid -> summary should be updated with path-derived UUID
        self.handler._do_write(
            meta_path, '{}',
            vmUuid='', vmName='updated-vm',
            vmCategory='AppCenter', architecture='',
            schemaVersion='')
        self.assertTrue(os.path.isfile(summary_path),
                        ".summary should still exist (path-derived UUID)")
        with open(summary_path, 'r') as f:
            summary = json.loads(f.read())
        self.assertEqual(summary['vmUuid'], vm_uuid)
        self.assertEqual(summary['vmName'], 'updated-vm')
        self.assertEqual(summary['vmCategory'], 'AppCenter')

    def _test_write_mismatched_vm_uuid_raises(self):
        """vmUuid that doesn't match metadataPath basename should be rejected."""
        vm_uuid = 'a' * 32
        wrong_uuid = 'b' * 32
        meta_path = self._meta_path(vm_uuid)
        with self.assertRaises(ValueError) as ctx:
            self.handler._do_write(
                meta_path, '{}',
                vmUuid=wrong_uuid, vmName='',
                vmCategory='', architecture='',
                schemaVersion='')
        self.assertIn('does not match', str(ctx.exception))

    def _test_write_empty_vm_uuid_derives_from_path_for_summary(self):
        """When vmUuid is empty, summary should still be written using UUID
        derived from metadataPath."""
        vm_uuid = 'c3' * 16
        meta_path = self._meta_path(vm_uuid)
        summary_path = meta_path + '.summary'

        self.handler._do_write(
            meta_path, '{"test":1}',
            vmUuid='', vmName='derived-vm',
            vmCategory='AppCenter', architecture='x86_64',
            schemaVersion='2')

        self.assertTrue(os.path.isfile(summary_path),
                        ".summary should be written using path-derived UUID")
        with open(summary_path, 'r') as f:
            summary = json.loads(f.read())
        self.assertEqual(summary['vmUuid'], vm_uuid)
        self.assertEqual(summary['vmName'], 'derived-vm')

    def _test_write_does_not_leave_tmp_file(self):
        vm_uuid = 'b2' * 16
        meta_path = self._meta_path(vm_uuid)
        self.handler._do_write(
            meta_path, '{"ok":true}',
            vmUuid=vm_uuid, vmName='',
            vmCategory='', architecture='',
            schemaVersion='')
        self.assertFalse(os.path.exists(meta_path + '.tmp'))

    def _test_write_overwrite_updates_payload(self):
        vm_uuid = 'c' * 32
        meta_path = self._meta_path(vm_uuid)

        self.handler._do_write(
            meta_path, '{"v":1}',
            vmUuid=vm_uuid, vmName='',
            vmCategory='', architecture='',
            schemaVersion='')
        self.handler._do_write(
            meta_path, '{"v":2}',
            vmUuid=vm_uuid, vmName='',
            vmCategory='', architecture='',
            schemaVersion='')

        result = self.handler._do_get(meta_path)
        self.assertEqual(result['metadata'], '{"v":2}')

    def _test_write_concurrent_writes_do_not_corrupt(self):
        vm_uuid = 'c1' * 16
        meta_path = self._meta_path(vm_uuid)
        errors = []
        start_event = threading.Event()

        def writer(idx):
            try:
                start_event.wait(5)
                payload = '{"writer":%d}' % idx
                self.handler._do_write(
                    meta_path, payload,
                    vmUuid=vm_uuid, vmName='',
                    vmCategory='', architecture='',
                    schemaVersion='')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,))
                   for i in range(4)]
        for t in threads:
            t.start()
        start_event.set()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            self.assertFalse(t.is_alive(), "concurrent write deadlocked")

        self.assertEqual(len(errors), 0,
                         "concurrent writes raised: %s" % errors)
        result = self.handler._do_get(meta_path)
        self.assertIsNotNone(result['metadata'])
        data = json.loads(result['metadata'])
        self.assertIn('writer', data)

    def _test_write_unicode_content(self):
        vm_uuid = 'c2' * 16
        meta_path = self._meta_path(vm_uuid)
        payload = u'{"name":"\u6d4b\u8bd5\u865a\u62df\u673a"}'
        self.handler._do_write(
            meta_path, payload,
            vmUuid=vm_uuid, vmName=u'\u6d4b\u8bd5VM',
            vmCategory='', architecture='',
            schemaVersion='')

        result = self.handler._do_get(meta_path)
        self.assertIn(u'\u6d4b\u8bd5', result['metadata'])

    # == get_vm_instance_metadata =============================================

    def _test_get_nonexistent_returns_none(self):
        meta_path = self._meta_path('d' * 32)
        result = self.handler._do_get(meta_path)
        self.assertIsNone(result['metadata'])

    def _test_get_orphan_tmp_returns_none(self):
        """If only a .tmp file exists (crash before rename), get returns None
        and does NOT promote the .tmp to the final path."""
        vm_uuid = 'd1' * 16
        meta_path = self._meta_path(vm_uuid)
        tmp_path = meta_path + '.tmp'
        with open(tmp_path, 'w') as f:
            f.write('partial data')

        result = self.handler._do_get(meta_path)
        self.assertIsNone(result['metadata'])
        self.assertFalse(os.path.isfile(meta_path))
        self.assertTrue(os.path.isfile(tmp_path))

    def _test_get_after_write_returns_payload(self):
        vm_uuid = 'e' * 32
        meta_path = self._meta_path(vm_uuid)
        payload = '{"key":"value","num":42}'

        self.handler._do_write(
            meta_path, payload,
            vmUuid=vm_uuid, vmName='',
            vmCategory='', architecture='',
            schemaVersion='')

        result = self.handler._do_get(meta_path)
        self.assertEqual(json.loads(result['metadata']),
                         json.loads(payload))

    # == scan_vm_metadata =====================================================

    def _test_scan_finds_written_metadata(self):
        uuid_1 = 'a1' * 16
        uuid_2 = 'b2' * 16

        self.handler._do_write(
            self._meta_path(uuid_1), '{"vm":1}',
            vmUuid=uuid_1, vmName='vm-one',
            vmCategory='AppCenter', architecture='x86_64',
            schemaVersion='1')
        self.handler._do_write(
            self._meta_path(uuid_2), '{"vm":2}',
            vmUuid=uuid_2, vmName='vm-two',
            vmCategory='', architecture='aarch64',
            schemaVersion='2')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 2)

        by_uuid = {e.vmUuid: e for e in entries}
        self.assertIn(uuid_1, by_uuid)
        self.assertIn(uuid_2, by_uuid)
        self.assertEqual(by_uuid[uuid_1].vmName, 'vm-one')
        self.assertEqual(by_uuid[uuid_2].architecture, 'aarch64')
        self.assertEqual(by_uuid[uuid_2].schemaVersion, '2')

    def _test_scan_ignores_non_uuid_filenames(self):
        # Files with names that don't match 32-char hex should be ignored
        with open(os.path.join(self.tmpdir, 'not-a-uuid.vmmeta'), 'w') as f:
            f.write('{}')
        with open(os.path.join(self.tmpdir, 'SHORT.vmmeta'), 'w') as f:
            f.write('{}')
        # Non-hex chars (Z not in [0-9a-f])
        with open(os.path.join(self.tmpdir, 'Z' * 32 + '.vmmeta'), 'w') as f:
            f.write('{}')
        # Valid one
        valid_uuid = 'a1b2c3d4e5f6' + 'a' * 20
        with open(os.path.join(self.tmpdir, valid_uuid + '.vmmeta'), 'w') as f:
            f.write('{}')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].vmUuid, valid_uuid)

    def _test_scan_ignores_unrelated_files(self):
        with open(os.path.join(self.tmpdir, 'readme.txt'), 'w') as f:
            f.write('text')
        with open(os.path.join(self.tmpdir, 'a' * 32 + '.json'), 'w') as f:
            f.write('{}')
        with open(os.path.join(self.tmpdir, 'a' * 32 + '.summary'), 'w') as f:
            f.write('{}')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 0)

    def _test_scan_tmp_only_entry_marked_incomplete(self):
        vm_uuid = 'b' * 32
        tmp_path = os.path.join(self.tmpdir, vm_uuid + '.vmmeta.tmp')
        with open(tmp_path, 'w') as f:
            f.write('partial')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].incomplete)
        expected_path = os.path.join(self.tmpdir, vm_uuid + '.vmmeta')
        self.assertEqual(entries[0].metadataPath, expected_path)

    def _test_scan_tmp_ignored_when_final_exists(self):
        """If both .vmmeta and .vmmeta.tmp exist, only the final file is
        reported (not marked incomplete)."""
        vm_uuid = 'c' * 32
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta'), 'w') as f:
            f.write('{"final":true}')
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta.tmp'), 'w') as f:
            f.write('{"tmp":true}')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].incomplete)

    def _test_scan_reads_summary_sidecar(self):
        vm_uuid = 'd' * 32
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta'), 'w') as f:
            f.write('{}')
        summary = {
            'vmUuid': vm_uuid,
            'vmName': 'test-vm',
            'vmCategory': 'AppCenter',
            'architecture': 'x86_64',
            'schemaVersion': '2',
        }
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta.summary'), 'w') as f:
            f.write(json.dumps(summary))

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].vmName, 'test-vm')
        self.assertEqual(entries[0].vmCategory, 'AppCenter')
        self.assertEqual(entries[0].architecture, 'x86_64')
        self.assertEqual(entries[0].schemaVersion, '2')

    def _test_scan_tmp_entry_does_not_read_summary(self):
        """Summary is NOT loaded for incomplete (.tmp-only) entries."""
        vm_uuid = 'e' * 32
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta.tmp'), 'w') as f:
            f.write('partial')
        summary = {'vmUuid': vm_uuid, 'vmName': 'should-not-appear'}
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta.summary'), 'w') as f:
            f.write(json.dumps(summary))

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].vmName, '')

    def _test_scan_tolerates_corrupt_summary(self):
        vm_uuid = 'f' * 32
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta'), 'w') as f:
            f.write('{}')
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta.summary'), 'w') as f:
            f.write('NOT JSON')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].vmName, '')

    def _test_scan_empty_dir_returns_empty_list(self):
        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 0)

    def _test_scan_missing_directory(self):
        missing_dir = os.path.join(self.tmpdir, 'missing_subdir_xyz')
        entries = self.handler._do_scan(missing_dir)
        self.assertEqual(len(entries), 0)

    def _test_scan_relative_path_returns_empty(self):
        entries = self.handler._do_scan('relative/path')
        self.assertEqual(len(entries), 0)

    def _test_scan_none_returns_empty(self):
        entries = self.handler._do_scan(None)
        self.assertEqual(len(entries), 0)

    def _test_scan_populates_last_update_time(self):
        vm_uuid = 'a' * 32
        before_ms = int(time.time() * 1000)
        with open(os.path.join(self.tmpdir, vm_uuid + '.vmmeta'), 'w') as f:
            f.write('{}')

        entries = self.handler._do_scan(self.tmpdir)
        self.assertEqual(len(entries), 1)
        self.assertGreaterEqual(entries[0].lastUpdateTime, before_ms - 1000)

    # == cleanup_vm_metadata ==================================================

    def _test_cleanup_removes_all_related_files(self):
        vm_uuid = 'f' * 32
        meta_path = self._meta_path(vm_uuid)

        # Create all 4 related files
        for suffix in ['', '.tmp', '.summary', '.summary.tmp']:
            with open(meta_path + suffix, 'w') as f:
                f.write('data')

        self.handler._do_cleanup(meta_path)

        self.assertFalse(os.path.exists(meta_path))
        self.assertFalse(os.path.exists(meta_path + '.tmp'))
        self.assertFalse(os.path.exists(meta_path + '.summary'))
        self.assertFalse(os.path.exists(meta_path + '.summary.tmp'))

    def _test_cleanup_only_main_file(self):
        vm_uuid = 'f1' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')
        self.handler._do_cleanup(meta_path)
        self.assertFalse(os.path.exists(meta_path))

    def _test_cleanup_main_plus_summary(self):
        vm_uuid = 'f2' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')
        with open(meta_path + '.summary', 'w') as f:
            f.write('{}')
        self.handler._do_cleanup(meta_path)
        self.assertFalse(os.path.exists(meta_path))
        self.assertFalse(os.path.exists(meta_path + '.summary'))

    def _test_cleanup_nonexistent_is_noop(self):
        meta_path = self._meta_path('00' * 16)
        # should not raise
        self.handler._do_cleanup(meta_path)

    def _test_cleanup_raises_if_main_file_cannot_be_removed(self):
        if os.getuid() == 0:
            # root can remove files from read-only dirs, skip this test
            return
        vm_uuid = 'f3' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')
        os.chmod(self.tmpdir, 0o444)
        try:
            with self.assertRaises(Exception) as ctx:
                self.handler._do_cleanup(meta_path)
            self.assertIn('failed to cleanup metadata file',
                          str(ctx.exception))
        finally:
            os.chmod(self.tmpdir, 0o755)

    def _test_cleanup_tolerates_sidecar_removal_failure(self):
        """Cleanup succeeds even if summary file was already removed."""
        vm_uuid = 'f4' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')
        summary_path = meta_path + '.summary'
        with open(summary_path, 'w') as f:
            f.write('{}')
        os.remove(summary_path)
        self.handler._do_cleanup(meta_path)
        self.assertFalse(os.path.exists(meta_path))

    def _test_cleanup_write_cleanup_read_returns_none(self):
        vm_uuid = 'f5' * 16
        meta_path = self._meta_path(vm_uuid)
        self.handler._do_write(
            meta_path, '{"data":1}',
            vmUuid=vm_uuid, vmName='vm1',
            vmCategory='', architecture='',
            schemaVersion='')
        self.assertTrue(os.path.isfile(meta_path))

        self.handler._do_cleanup(meta_path)

        result = self.handler._do_get(meta_path)
        self.assertIsNone(result['metadata'])
        self.assertFalse(os.path.exists(meta_path + '.summary'))

    def _test_cleanup_does_not_remove_parent_directory(self):
        vm_uuid = 'f6' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')
        self.handler._do_cleanup(meta_path)
        self.assertTrue(os.path.isdir(self.tmpdir))

    # == cleanup_all_vm_metadata ================================================

    def _test_cleanup_all_returns_error_on_directory_fsync_failure(self):
        vm_uuid = 'f7' * 16
        meta_path = self._meta_path(vm_uuid)
        with open(meta_path, 'w') as f:
            f.write('data')

        original_fsync_directory = file_metadata_handler._fsync_directory

        def _fsync_that_fails(path):
            raise OSError("fsync failed")

        file_metadata_handler._fsync_directory = _fsync_that_fails
        try:
            result = self.handler._do_cleanup_all(self.tmpdir)
        finally:
            file_metadata_handler._fsync_directory = original_fsync_directory

        self.assertIn('error', result)
        self.assertIn('fsync', result['error'])

    def test_cleanup_all_generation_fences_delayed_cleanup(self):
        vm_uuid = 'f8' * 16
        meta_path = self._meta_path(vm_uuid)

        self.handler._do_write(
            meta_path, '{"generation":1}',
            vmUuid=vm_uuid, vmName='vm1',
            vmCategory='', architecture='',
            schemaVersion='', metadataGeneration=1)
        self.handler._do_cleanup_all(
            self.tmpdir, metadataGeneration=2)
        self.assertFalse(os.path.exists(meta_path))

        self.handler._do_write(
            meta_path, '{"generation":3}',
            vmUuid=vm_uuid, vmName='vm1',
            vmCategory='', architecture='',
            schemaVersion='', metadataGeneration=3)

        restarted_handler = FileBasedMetadataHandler()
        restarted_handler._do_cleanup_all(
            self.tmpdir, metadataGeneration=2)

        self.assertTrue(os.path.isfile(meta_path))
        self.assertEqual(
            restarted_handler._do_get(meta_path)['metadata'],
            '{"generation":3}')
        with self.assertRaises(StaleMetadataGeneration):
            restarted_handler._do_write(
                meta_path, '{"generation":1}',
                vmUuid=vm_uuid, vmName='vm1',
                vmCategory='', architecture='',
                schemaVersion='', metadataGeneration=1)

        missing_dir = os.path.join(self.tmpdir, 'missing')
        missing_meta_path = os.path.join(
            missing_dir, vm_uuid + '.vmmeta')
        restarted_handler._do_cleanup_all(
            missing_dir, metadataGeneration=4)
        with self.assertRaises(StaleMetadataGeneration):
            restarted_handler._do_write(
                missing_meta_path, '{"generation":3}',
                vmUuid=vm_uuid, vmName='vm1',
                vmCategory='', architecture='',
                schemaVersion='', metadataGeneration=3)

    def test_cleanup_all_serializes_generation_with_write(self):
        vm_uuid = 'f9' * 16
        meta_path = self._meta_path(vm_uuid)
        self.handler._do_write(
            meta_path, '{"generation":1}',
            vmUuid=vm_uuid, vmName='vm1',
            vmCategory='', architecture='',
            schemaVersion='', metadataGeneration=1)

        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        writer_started = threading.Event()
        writer_finished = threading.Event()
        errors = []
        original_cleanup = self.handler._cleanup_all_metadata_files

        def paused_cleanup(metadata_dir):
            cleanup_entered.set()
            if not release_cleanup.wait(5):
                raise RuntimeError("timed out waiting to release cleanup")
            return original_cleanup(metadata_dir)

        def cleanup():
            try:
                self.handler._do_cleanup_all(
                    self.tmpdir, metadataGeneration=2)
            except Exception as e:
                errors.append(e)

        def write():
            writer_started.set()
            try:
                self.handler._do_write(
                    meta_path, '{"generation":3}',
                    vmUuid=vm_uuid, vmName='vm1',
                    vmCategory='', architecture='',
                    schemaVersion='', metadataGeneration=3)
            except Exception as e:
                errors.append(e)
            finally:
                writer_finished.set()

        self.handler._cleanup_all_metadata_files = paused_cleanup
        cleanup_thread = threading.Thread(target=cleanup)
        writer_thread = threading.Thread(target=write)
        try:
            cleanup_thread.start()
            self.assertTrue(cleanup_entered.wait(5))
            writer_thread.start()
            self.assertTrue(writer_started.wait(5))
            self.assertFalse(
                writer_finished.wait(0.2),
                "metadata write entered while cleanup held the generation lock")
        finally:
            release_cleanup.set()
            cleanup_thread.join(5)
            writer_thread.join(5)
            self.handler._cleanup_all_metadata_files = original_cleanup

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(
            '{"generation":3}',
            self.handler._do_get(meta_path)['metadata'])

    # == prefix_rebase_backing_files ==========================================

    def _test_rebase_is_imagecache_path_positive(self):
        self.assertTrue(_is_imagecache_path(
            '/opt/zstack/nfs/prim-uuid/imagecache/template/img-uuid/img-uuid.qcow2'))

    def _test_rebase_is_imagecache_path_negative(self):
        self.assertFalse(_is_imagecache_path(
            '/opt/zstack/nfs/prim-uuid/rootVolumes/vol-uuid/vol-uuid.qcow2'))

    def _test_rebase_get_rebase_lock_path(self):
        self.assertEqual(
            _get_rebase_lock_path('/a/imagecache/template/uuid/uuid.qcow2'),
            '/a/imagecache/template/uuid/uuid.vmmeta-lck')

    def _test_rebase_get_rebase_lock_path_no_extension(self):
        self.assertEqual(
            _get_rebase_lock_path('/a/imagecache/img'),
            '/a/imagecache/img.vmmeta-lck')

    def _test_rebase_empty_old_prefix_raises(self):
        vol = os.path.join(self.tmpdir, 'vol.qcow2')
        _qemu_img_create(vol, '64M')
        with self.assertRaises(Exception):
            qcow2_prefix_rebase_backing_files([vol], '', '/new/')

    def _test_rebase_empty_new_prefix_raises(self):
        vol = os.path.join(self.tmpdir, 'vol.qcow2')
        _qemu_img_create(vol, '64M')
        with self.assertRaises(Exception):
            qcow2_prefix_rebase_backing_files([vol], '/old/', '')

    def _test_rebase_no_backing_returns_zero(self):
        vol = os.path.join(self.tmpdir, 'vol.qcow2')
        _qemu_img_create(vol, '64M')
        count = qcow2_prefix_rebase_backing_files([vol], '/old', '/new')
        self.assertEqual(count, 0)

    def _test_rebase_single_backing(self):
        old_base = os.path.join(self.tmpdir, 'old', 'base.qcow2')
        new_base = os.path.join(self.tmpdir, 'new', 'base.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(old_base, '64M')
        os.makedirs(os.path.dirname(new_base))
        shutil.copy2(old_base, new_base)
        _qemu_img_create(vol, backing=old_base)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)

        self.assertEqual(count, 1)
        self.assertEqual(_get_backing(vol), new_base)

    def _test_rebase_multi_level_chain(self):
        """vol -> /old/snap -> /old/base   all get rebased."""
        old_dir = os.path.join(self.tmpdir, 'old', 'rootVolumes')
        new_dir = os.path.join(self.tmpdir, 'new', 'rootVolumes')
        old_base = os.path.join(old_dir, 'base.qcow2')
        old_snap = os.path.join(old_dir, 'snap.qcow2')
        new_base = os.path.join(new_dir, 'base.qcow2')
        new_snap = os.path.join(new_dir, 'snap.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(old_base, '64M')
        _qemu_img_create(old_snap, backing=old_base)
        os.makedirs(new_dir)
        shutil.copy2(old_base, new_base)
        shutil.copy2(old_snap, new_snap)
        _qemu_img_create(vol, backing=old_snap)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)

        self.assertEqual(count, 2)
        self.assertEqual(_get_backing(vol), new_snap)
        self.assertEqual(_get_backing(new_snap), new_base)

    def _test_rebase_imagecache_backing_acquires_flock(self):
        """Backing in /imagecache/ triggers flock -- lock file should be
        created."""
        img_uuid = 'a' * 32
        old_cache = os.path.join(
            self.tmpdir, 'old', 'imagecache', 'template', img_uuid,
            img_uuid + '.qcow2')
        new_cache = os.path.join(
            self.tmpdir, 'new', 'imagecache', 'template', img_uuid,
            img_uuid + '.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(old_cache, '64M')
        os.makedirs(os.path.dirname(new_cache))
        shutil.copy2(old_cache, new_cache)
        _qemu_img_create(vol, backing=old_cache)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)

        self.assertEqual(count, 1)
        self.assertEqual(_get_backing(vol), new_cache)
        lock_path = os.path.splitext(new_cache)[0] + '.vmmeta-lck'
        self.assertTrue(os.path.exists(lock_path),
                        "lock file %s should exist after rebase" % lock_path)

    def _test_rebase_no_imagecache_no_locks(self):
        """No /imagecache/ in path -> no flock at all."""
        old_base = os.path.join(self.tmpdir, 'old', 'rootVolumes', 'base.qcow2')
        new_base = os.path.join(self.tmpdir, 'new', 'rootVolumes', 'base.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(old_base, '64M')
        os.makedirs(os.path.dirname(new_base))
        shutil.copy2(old_base, new_base)
        _qemu_img_create(vol, backing=old_base)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)
        self.assertEqual(count, 1)

        r, lck_files = bash.bash_ro(
            'find %s -name "*.vmmeta-lck"' % self.tmpdir)
        self.assertEqual(lck_files.strip(), '',
                         "no lock files expected, found: %s" % lck_files)

    def _test_rebase_new_backing_missing_skips_chain(self):
        old_base = os.path.join(self.tmpdir, 'old', 'base.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(old_base, '64M')
        _qemu_img_create(vol, backing=old_base)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)
        self.assertEqual(count, 0)
        self.assertEqual(_get_backing(vol), old_base)

    def _test_rebase_backing_outside_old_prefix_not_rebased(self):
        other_base = os.path.join(self.tmpdir, 'other', 'base.qcow2')
        vol = os.path.join(self.tmpdir, 'vol.qcow2')

        _qemu_img_create(other_base, '64M')
        _qemu_img_create(vol, backing=other_base)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol], old_prefix, new_prefix)
        self.assertEqual(count, 0)

    def _test_rebase_multiple_file_paths_independent_chains(self):
        old_a = os.path.join(self.tmpdir, 'old', 'a.qcow2')
        old_b = os.path.join(self.tmpdir, 'old', 'b.qcow2')
        new_a = os.path.join(self.tmpdir, 'new', 'a.qcow2')
        new_b = os.path.join(self.tmpdir, 'new', 'b.qcow2')
        vol_a = os.path.join(self.tmpdir, 'vol-a.qcow2')
        vol_b = os.path.join(self.tmpdir, 'vol-b.qcow2')

        _qemu_img_create(old_a, '64M')
        _qemu_img_create(old_b, '64M')
        os.makedirs(os.path.join(self.tmpdir, 'new'))
        shutil.copy2(old_a, new_a)
        shutil.copy2(old_b, new_b)
        _qemu_img_create(vol_a, backing=old_a)
        _qemu_img_create(vol_b, backing=old_b)

        old_prefix = os.path.join(self.tmpdir, 'old')
        new_prefix = os.path.join(self.tmpdir, 'new')
        count = qcow2_prefix_rebase_backing_files(
            [vol_a, vol_b], old_prefix, new_prefix)
        self.assertEqual(count, 2)
        self.assertEqual(_get_backing(vol_a), new_a)
        self.assertEqual(_get_backing(vol_b), new_b)

    def _test_rebase_empty_file_paths_returns_zero(self):
        count = qcow2_prefix_rebase_backing_files([], '/old', '/new')
        self.assertEqual(count, 0)

    # == validate_metadata_path (F1) ==========================================

    def _test_validate_relative_path_raises(self):
        with self.assertRaises(ValueError):
            _validate_metadata_path('relative/path.vmmeta')

    def _test_validate_none_raises(self):
        with self.assertRaises(ValueError):
            _validate_metadata_path(None)

    def _test_validate_empty_string_raises(self):
        with self.assertRaises(ValueError):
            _validate_metadata_path('')

    def _test_validate_no_suffix_raises(self):
        with self.assertRaises(ValueError):
            _validate_metadata_path('/tmp/somefile.json')

    def _test_validate_valid_path_passes(self):
        # should not raise
        _validate_metadata_path('/tmp/' + '11223344' * 4 + '.vmmeta')

    def _test_validate_non_uuid_basename_raises(self):
        """Basename must be <32hex>.vmmeta to stay consistent with scan()."""
        with self.assertRaises(ValueError):
            _validate_metadata_path('/tmp/foo.vmmeta')
        with self.assertRaises(ValueError):
            _validate_metadata_path('/tmp/abc123.vmmeta')
        with self.assertRaises(ValueError):
            # 31 chars - too short
            _validate_metadata_path('/tmp/' + 'a' * 31 + '.vmmeta')
        with self.assertRaises(ValueError):
            # 33 chars - too long
            _validate_metadata_path('/tmp/' + 'a' * 33 + '.vmmeta')
        with self.assertRaises(ValueError):
            # uppercase - _UUID_HEX_RE is lowercase only
            _validate_metadata_path('/tmp/' + 'A' * 32 + '.vmmeta')

    def _test_write_rejects_relative_path(self):
        with self.assertRaises(ValueError):
            self.handler._do_write(
                'relative.vmmeta', '{}',
                vmUuid='a' * 32, vmName='',
                vmCategory='', architecture='',
                schemaVersion='')

    def _test_get_rejects_no_suffix(self):
        with self.assertRaises(ValueError):
            self.handler._do_get('/tmp/invalid_path.json')

    def _test_cleanup_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.handler._do_cleanup('')

    # == lock pool eviction (F2) ==============================================

    def _test_evict_reduces_lock_map_size(self):
        """Writing to > HIGH_WATER distinct paths triggers eviction,
        shrinking the lock map back toward LOW_WATER."""
        handler = FileBasedMetadataHandler()
        # Lower the thresholds for testing
        handler._LOCK_MAP_HIGH_WATER = 50
        handler._LOCK_MAP_LOW_WATER = 10

        tmpdir = tempfile.mkdtemp(prefix='vmmeta_evict_')
        try:
            for i in range(60):
                vm_uuid = '%032x' % i
                meta_path = os.path.join(tmpdir, vm_uuid + '.vmmeta')
                handler._do_write(
                    meta_path, '{"i":%d}' % i,
                    vmUuid=vm_uuid, vmName='',
                    vmCategory='', architecture='',
                    schemaVersion='')

            # After 60 writes with HIGH_WATER=50, eviction should have fired
            self.assertLessEqual(len(handler._lock_map), 55,
                                 "lock map should have been evicted")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # == G3: _do_get ENOENT race (file deleted between isfile and open) ======

    def _test_get_race_file_deleted_during_read(self):
        """G3: file disappears between os.path.isfile() returning True and
        open() -- _do_get should catch the ENOENT race and return None."""
        vm_uuid = 'a9' * 16
        meta_path = self._meta_path(vm_uuid)

        # Write a real file so path validation and isfile pass
        self.handler._do_write(
            meta_path, '{"race":true}',
            vmUuid=vm_uuid, vmName='',
            vmCategory='', architecture='',
            schemaVersion='')
        self.assertTrue(os.path.isfile(meta_path))

        # Monkey-patch os.path.isfile to return True, but delete the file
        # before _do_get calls open(), simulating a race condition
        import errno as errno_mod
        original_isfile = os.path.isfile

        def _isfile_then_delete(path):
            result = original_isfile(path)
            # After isfile returns True for our target, delete the file
            # so the subsequent open() hits ENOENT
            if path == meta_path and result:
                try:
                    os.remove(meta_path)
                except OSError:
                    pass
            return result

        os.path.isfile = _isfile_then_delete
        try:
            result = self.handler._do_get(meta_path)
        finally:
            os.path.isfile = original_isfile

        self.assertIsNone(result['metadata'],
                          "get should return None on ENOENT race")

        self.assertIsNone(result['metadata'],
                          "get should return None on ENOENT race")

    # == G5: _do_cleanup main file removal fails (mock) ====================

    def _test_cleanup_main_file_removal_fails_via_mock(self):
        """G5: when os.remove fails on the primary .vmmeta file,
        _do_cleanup must raise an Exception with 'failed to cleanup'."""
        vm_uuid = 'b9' * 16
        meta_path = self._meta_path(vm_uuid)

        # Create the file
        with open(meta_path, 'w') as f:
            f.write('data')
        self.assertTrue(os.path.isfile(meta_path))

        # Monkey-patch os.remove to fail only for the target file
        original_remove = os.remove

        def _remove_that_fails(path):
            if path == meta_path:
                raise OSError(13, "Permission denied", path)
            return original_remove(path)

        os.remove = _remove_that_fails
        try:
            with self.assertRaises(Exception) as ctx:
                self.handler._do_cleanup(meta_path)
            self.assertIn('failed to cleanup', str(ctx.exception))
        finally:
            os.remove = original_remove
            # Clean up the file that couldn't be removed
            try:
                original_remove(meta_path)
            except OSError:
                pass

    # -- single entry point ---------------------------------------------------

    @pytest_utils.ztest_decorater
    def test_file_metadata_api(self):
        # write
        self._test_write_creates_file_and_stores_data()
        self._clean_tmpdir()
        self._test_write_creates_parent_dir()
        self._clean_tmpdir()
        self._test_write_with_vm_summary()
        self._clean_tmpdir()
        self._test_write_without_vm_uuid_uses_path_derived_uuid_for_summary()
        self._clean_tmpdir()
        self._test_write_mismatched_vm_uuid_raises()
        self._clean_tmpdir()
        self._test_write_empty_vm_uuid_derives_from_path_for_summary()
        self._clean_tmpdir()
        self._test_write_does_not_leave_tmp_file()
        self._clean_tmpdir()
        self._test_write_overwrite_updates_payload()
        self._clean_tmpdir()
        self._test_write_concurrent_writes_do_not_corrupt()
        self._clean_tmpdir()
        self._test_write_unicode_content()
        self._clean_tmpdir()

        # get
        self._test_get_nonexistent_returns_none()
        self._clean_tmpdir()
        self._test_get_orphan_tmp_returns_none()
        self._clean_tmpdir()
        self._test_get_after_write_returns_payload()
        self._clean_tmpdir()

        # scan
        self._test_scan_finds_written_metadata()
        self._clean_tmpdir()
        self._test_scan_ignores_non_uuid_filenames()
        self._clean_tmpdir()
        self._test_scan_ignores_unrelated_files()
        self._clean_tmpdir()
        self._test_scan_tmp_only_entry_marked_incomplete()
        self._clean_tmpdir()
        self._test_scan_tmp_ignored_when_final_exists()
        self._clean_tmpdir()
        self._test_scan_reads_summary_sidecar()
        self._clean_tmpdir()
        self._test_scan_tmp_entry_does_not_read_summary()
        self._clean_tmpdir()
        self._test_scan_tolerates_corrupt_summary()
        self._clean_tmpdir()
        self._test_scan_empty_dir_returns_empty_list()
        self._test_scan_missing_directory()
        self._test_scan_relative_path_returns_empty()
        self._test_scan_none_returns_empty()
        self._clean_tmpdir()
        self._test_scan_populates_last_update_time()
        self._clean_tmpdir()

        # cleanup
        self._test_cleanup_removes_all_related_files()
        self._clean_tmpdir()
        self._test_cleanup_only_main_file()
        self._clean_tmpdir()
        self._test_cleanup_main_plus_summary()
        self._clean_tmpdir()
        self._test_cleanup_nonexistent_is_noop()
        self._test_cleanup_raises_if_main_file_cannot_be_removed()
        self._clean_tmpdir()
        self._test_cleanup_tolerates_sidecar_removal_failure()
        self._clean_tmpdir()
        self._test_cleanup_write_cleanup_read_returns_none()
        self._clean_tmpdir()
        self._test_cleanup_does_not_remove_parent_directory()
        self._clean_tmpdir()
        self._test_cleanup_all_returns_error_on_directory_fsync_failure()
        self._clean_tmpdir()

        # prefix_rebase - helper functions
        self._test_rebase_is_imagecache_path_positive()
        self._test_rebase_is_imagecache_path_negative()
        self._test_rebase_get_rebase_lock_path()
        self._test_rebase_get_rebase_lock_path_no_extension()

        # prefix_rebase - rebase operations
        self._test_rebase_empty_old_prefix_raises()
        self._clean_tmpdir()
        self._test_rebase_empty_new_prefix_raises()
        self._clean_tmpdir()
        self._test_rebase_no_backing_returns_zero()
        self._clean_tmpdir()
        self._test_rebase_single_backing()
        self._clean_tmpdir()
        self._test_rebase_multi_level_chain()
        self._clean_tmpdir()
        self._test_rebase_imagecache_backing_acquires_flock()
        self._clean_tmpdir()
        self._test_rebase_no_imagecache_no_locks()
        self._clean_tmpdir()
        self._test_rebase_new_backing_missing_skips_chain()
        self._clean_tmpdir()
        self._test_rebase_backing_outside_old_prefix_not_rebased()
        self._clean_tmpdir()
        self._test_rebase_multiple_file_paths_independent_chains()
        self._clean_tmpdir()
        self._test_rebase_empty_file_paths_returns_zero()

        # validate_metadata_path (F1)
        self._test_validate_relative_path_raises()
        self._test_validate_none_raises()
        self._test_validate_empty_string_raises()
        self._test_validate_no_suffix_raises()
        self._test_validate_valid_path_passes()
        self._test_validate_non_uuid_basename_raises()
        self._test_write_rejects_relative_path()
        self._test_get_rejects_no_suffix()
        self._test_cleanup_rejects_empty()

        # lock pool eviction (F2)
        self._test_evict_reduces_lock_map_size()

        # G3: ENOENT race during read
        self._clean_tmpdir()
        self._test_get_race_file_deleted_during_read()

        # G5: main file removal failure
        self._clean_tmpdir()
        self._test_cleanup_main_file_removal_fails_via_mock()
