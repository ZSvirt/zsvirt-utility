import os
import shutil
import sys
import tempfile
import types
import unittest


def _load_file_metadata_handler():
    """Load the production module without importing host-only agent helpers."""
    module_names = [
        'zstacklib',
        'zstacklib.utils',
        'zstacklib.utils.lock',
        'zstacklib.utils.vm_metadata_handler',
    ]
    saved_modules = dict((name, sys.modules.get(name)) for name in module_names)

    zstacklib_module = types.ModuleType('zstacklib')
    utils_module = types.ModuleType('zstacklib.utils')
    lock_module = types.ModuleType('zstacklib.utils.lock')
    metadata_module = types.ModuleType('zstacklib.utils.vm_metadata_handler')

    class _NoopFlock(object):
        pass

    class _NoopFileLock(object):
        def __init__(self, _path, locker=None):
            self.locker = locker

        def lock(self):
            pass

        def unlock(self):
            pass

    lock_module.Flock = _NoopFlock
    lock_module.FileLock = _NoopFileLock
    metadata_module.StaleMetadataGeneration = type('StaleMetadataGeneration', (Exception,), {})
    metadata_module.VmMetadataHandler = type('VmMetadataHandler', (object,), {})
    metadata_module.VmMetadataScanEntry = type('VmMetadataScanEntry', (object,), {})
    utils_module.lock = lock_module

    sys.modules['zstacklib'] = zstacklib_module
    sys.modules['zstacklib.utils'] = utils_module
    sys.modules['zstacklib.utils.lock'] = lock_module
    sys.modules['zstacklib.utils.vm_metadata_handler'] = metadata_module

    source_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', 'utils', 'file_metadata_handler.py'))
    try:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'file_metadata_handler_under_test', source_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except ImportError:
            import imp
            module = imp.load_source('file_metadata_handler_under_test', source_path)
    finally:
        for name, saved in saved_modules.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
    return module


class TestNfsBackingRebase(unittest.TestCase):
    def setUp(self):
        self.module = _load_file_metadata_handler()
        self.tmpdir = tempfile.mkdtemp(prefix='nfs_rebase_')
        self.backing_by_path = {}
        self.rebased = []

        self.saved_linux = sys.modules.get('zstacklib.utils.linux')
        linux_module = types.ModuleType('zstacklib.utils.linux')
        linux_module.qcow2_get_backing_file = self._get_backing
        linux_module.qcow2_rebase_no_check = self._rebase
        sys.modules['zstacklib.utils.linux'] = linux_module

    def tearDown(self):
        if self.saved_linux is None:
            sys.modules.pop('zstacklib.utils.linux', None)
        else:
            sys.modules['zstacklib.utils.linux'] = self.saved_linux
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _touch(self, path):
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, 'wb'):
            pass

    def _get_backing(self, path):
        return self.backing_by_path.get(os.path.normpath(path), '')

    def _rebase(self, new_backing, current_path):
        current_path = os.path.normpath(current_path)
        new_backing = os.path.normpath(new_backing)
        self.rebased.append((current_path, new_backing))
        self.backing_by_path[current_path] = new_backing

    def test_rebases_historical_nfs_mount_backing_to_current_mount(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_cache = os.path.join(
            'imagecache', 'template', 'image-uuid', 'image-uuid.qcow2')
        historical_cache = os.path.join(historical_prefix, relative_cache)
        current_cache = os.path.join(current_prefix, relative_cache)
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'vol.qcow2')

        self._touch(current_cache)
        self._touch(volume)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(historical_cache)
        self.backing_by_path[os.path.normpath(current_cache)] = ''

        nfs_rebase = getattr(
            self.module, 'nfs_qcow2_prefix_rebase_backing_files', None)
        self.assertIsNotNone(
            nfs_rebase,
            'NFS needs a strict resolver for backing paths whose mount prefix differs from metadata')

        count = nfs_rebase(
            [volume], metadata_prefix, current_prefix)

        self.assertEqual(1, count)
        self.assertEqual(os.path.normpath(current_cache), self._get_backing(volume))
        self.assertEqual(
            [(os.path.normpath(volume), os.path.normpath(current_cache))],
            self.rebased)

    def test_missing_target_rejects_whole_plan_before_any_rebase(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')

        valid_relative = os.path.join(
            'imagecache', 'template', 'valid-image', 'valid-image.qcow2')
        missing_relative = os.path.join(
            'imagecache', 'template', 'missing-image', 'missing-image.qcow2')
        historical_valid = os.path.join(historical_prefix, valid_relative)
        historical_missing = os.path.join(historical_prefix, missing_relative)
        current_valid = os.path.join(current_prefix, valid_relative)
        current_missing = os.path.join(current_prefix, missing_relative)
        volume_one = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-one', 'vol-one.qcow2')
        volume_two = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-two', 'vol-two.qcow2')

        self._touch(current_valid)
        self._touch(volume_one)
        self._touch(volume_two)
        self.backing_by_path[os.path.normpath(volume_one)] = os.path.normpath(historical_valid)
        self.backing_by_path[os.path.normpath(volume_two)] = os.path.normpath(historical_missing)
        self.backing_by_path[os.path.normpath(current_valid)] = ''

        nfs_rebase = self.module.nfs_qcow2_prefix_rebase_backing_files
        with self.assertRaises(Exception) as error:
            nfs_rebase([volume_one, volume_two], metadata_prefix, current_prefix)

        self.assertIn(
            "new backing %s not exist" % os.path.normpath(current_missing),
            str(error.exception))
        self.assertEqual([], self.rebased)
        self.assertEqual(os.path.normpath(historical_valid), self._get_backing(volume_one))
        self.assertEqual(os.path.normpath(historical_missing), self._get_backing(volume_two))

    def test_rejects_backing_outside_managed_nfs_mounts(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        external_backing = os.path.join(self.tmpdir, 'external', 'base.qcow2')
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'vol.qcow2')

        self._touch(external_backing)
        self._touch(volume)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(external_backing)
        self.backing_by_path[os.path.normpath(external_backing)] = ''

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume], metadata_prefix, current_prefix)

        self.assertIn(
            "is outside the current or recognized historical storage path",
            str(error.exception))
        self.assertEqual([], self.rebased)
        self.assertEqual(os.path.normpath(external_backing), self._get_backing(volume))

    def test_target_disappearing_after_lock_rejects_plan_before_any_rebase(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_one = os.path.join(
            'imagecache', 'template', 'image-one', 'image-one.qcow2')
        relative_two = os.path.join(
            'imagecache', 'template', 'image-two', 'image-two.qcow2')
        historical_one = os.path.join(historical_prefix, relative_one)
        historical_two = os.path.join(historical_prefix, relative_two)
        current_one = os.path.join(current_prefix, relative_one)
        current_two = os.path.join(current_prefix, relative_two)
        volume_one = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-one', 'vol-one.qcow2')
        volume_two = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-two', 'vol-two.qcow2')

        for path in (current_one, current_two, volume_one, volume_two):
            self._touch(path)
        self.backing_by_path[os.path.normpath(volume_one)] = os.path.normpath(historical_one)
        self.backing_by_path[os.path.normpath(volume_two)] = os.path.normpath(historical_two)
        self.backing_by_path[os.path.normpath(current_one)] = ''
        self.backing_by_path[os.path.normpath(current_two)] = ''

        target_to_remove = current_two

        class _RemoveTargetAfterAllLocks(object):
            lock_count = 0

            def __init__(self, _path, locker=None):
                self.locker = locker

            def lock(self):
                _RemoveTargetAfterAllLocks.lock_count += 1
                if _RemoveTargetAfterAllLocks.lock_count == 2:
                    os.remove(target_to_remove)

            def unlock(self):
                pass

        self.module._is_imagecache_path = lambda path: (
            os.sep + 'imagecache' + os.sep) in path
        self.module.lock.FileLock = _RemoveTargetAfterAllLocks

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume_one, volume_two], metadata_prefix, current_prefix)

        self.assertIn(
            "target backing %s disappeared since discovery" %
            os.path.normpath(current_two),
            str(error.exception))
        self.assertEqual([], self.rebased)
        self.assertEqual(os.path.normpath(historical_one), self._get_backing(volume_one))
        self.assertEqual(os.path.normpath(historical_two), self._get_backing(volume_two))

    def test_shared_chain_edges_are_rebased_once(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        snapshot_relative = os.path.join(
            'rootVolumes', 'acct-uuid', 'vol-uuid', 'snapshots', 'snap.qcow2')
        cache_relative = os.path.join(
            'imagecache', 'template', 'image-uuid', 'image-uuid.qcow2')
        historical_snapshot = os.path.join(historical_prefix, snapshot_relative)
        current_snapshot = os.path.join(current_prefix, snapshot_relative)
        historical_cache = os.path.join(historical_prefix, cache_relative)
        current_cache = os.path.join(current_prefix, cache_relative)
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'volume.qcow2')

        for path in (current_snapshot, current_cache, volume):
            self._touch(path)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(historical_snapshot)
        self.backing_by_path[os.path.normpath(current_snapshot)] = os.path.normpath(historical_cache)
        self.backing_by_path[os.path.normpath(current_cache)] = ''

        count = self.module.nfs_qcow2_prefix_rebase_backing_files(
            [volume, current_snapshot], metadata_prefix, current_prefix)

        self.assertEqual(2, count)
        self.assertEqual(2, len(self.rebased))
        self.assertEqual(os.path.normpath(current_snapshot), self._get_backing(volume))
        self.assertEqual(os.path.normpath(current_cache), self._get_backing(current_snapshot))

    def test_rejects_backing_cycle(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'volume.qcow2')
        snapshot = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid',
            'snapshots', 'snapshot.qcow2')

        self._touch(volume)
        self._touch(snapshot)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(snapshot)
        self.backing_by_path[os.path.normpath(snapshot)] = os.path.normpath(volume)

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume], metadata_prefix, current_prefix)

        self.assertIn(
            "backing chain cycle detected at %s" % os.path.normpath(volume),
            str(error.exception))
        self.assertEqual([], self.rebased)

    def test_rejects_successful_command_when_backing_header_did_not_change(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_cache = os.path.join(
            'imagecache', 'template', 'image-uuid', 'image-uuid.qcow2')
        historical_cache = os.path.join(historical_prefix, relative_cache)
        current_cache = os.path.join(current_prefix, relative_cache)
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'volume.qcow2')

        self._touch(current_cache)
        self._touch(volume)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(historical_cache)
        self.backing_by_path[os.path.normpath(current_cache)] = ''

        def _record_without_changing_header(new_backing, current_path):
            self.rebased.append((os.path.normpath(current_path), os.path.normpath(new_backing)))

        sys.modules['zstacklib.utils.linux'].qcow2_rebase_no_check = (
            _record_without_changing_header)

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume], metadata_prefix, current_prefix)

        self.assertIn(
            "incomplete rebase plan: backing of %s was not rebased to %s" %
            (os.path.normpath(volume), os.path.normpath(current_cache)),
            str(error.exception))
        self.assertEqual(os.path.normpath(historical_cache), self._get_backing(volume))

    def test_rejects_missing_backing_already_using_current_prefix(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        missing_cache = os.path.join(
            current_prefix, 'imagecache', 'template', 'missing-image',
            'missing-image.qcow2')
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'volume.qcow2')

        self._touch(volume)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(missing_cache)

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume], metadata_prefix, current_prefix)

        self.assertIn(
            "current backing %s does not exist" % os.path.normpath(missing_cache),
            str(error.exception))
        self.assertEqual([], self.rebased)

    def test_final_scan_rejects_chain_changed_after_individual_rebase_check(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_one = os.path.join(
            'imagecache', 'template', 'image-one', 'image-one.qcow2')
        relative_two = os.path.join(
            'imagecache', 'template', 'image-two', 'image-two.qcow2')
        historical_one = os.path.join(historical_prefix, relative_one)
        historical_two = os.path.join(historical_prefix, relative_two)
        current_one = os.path.join(current_prefix, relative_one)
        current_two = os.path.join(current_prefix, relative_two)
        volume_one = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-one', 'vol-one.qcow2')
        volume_two = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-two', 'vol-two.qcow2')
        external_backing = os.path.join(self.tmpdir, 'external', 'base.qcow2')

        for path in (current_one, current_two, volume_one, volume_two, external_backing):
            self._touch(path)
        self.backing_by_path[os.path.normpath(volume_one)] = os.path.normpath(historical_one)
        self.backing_by_path[os.path.normpath(volume_two)] = os.path.normpath(historical_two)
        self.backing_by_path[os.path.normpath(current_one)] = ''
        self.backing_by_path[os.path.normpath(current_two)] = ''
        self.backing_by_path[os.path.normpath(external_backing)] = ''

        def _rebase_and_corrupt_first_chain(new_backing, current_path):
            self._rebase(new_backing, current_path)
            if len(self.rebased) == 2:
                self.backing_by_path[os.path.normpath(volume_one)] = os.path.normpath(
                    external_backing)

        sys.modules['zstacklib.utils.linux'].qcow2_rebase_no_check = (
            _rebase_and_corrupt_first_chain)

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume_one, volume_two], metadata_prefix, current_prefix)

        self.assertIn(
            "incomplete rebase plan: backing %s of %s is outside current storage prefix" %
            (os.path.normpath(external_backing), os.path.normpath(volume_one)),
            str(error.exception))

    def test_already_current_chain_is_idempotent(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        cache = os.path.join(
            current_prefix, 'imagecache', 'template', 'image-uuid',
            'image-uuid.qcow2')
        volume = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-uuid', 'volume.qcow2')

        self._touch(cache)
        self._touch(volume)
        self.backing_by_path[os.path.normpath(volume)] = os.path.normpath(cache)
        self.backing_by_path[os.path.normpath(cache)] = ''

        count = self.module.nfs_qcow2_prefix_rebase_backing_files(
            [volume], metadata_prefix, current_prefix)

        self.assertEqual(0, count)
        self.assertEqual([], self.rebased)
        self.assertEqual(os.path.normpath(cache), self._get_backing(volume))

    def test_retry_finishes_after_mid_execution_failure(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_one = os.path.join(
            'imagecache', 'template', 'image-one', 'image-one.qcow2')
        relative_two = os.path.join(
            'imagecache', 'template', 'image-two', 'image-two.qcow2')
        historical_one = os.path.join(historical_prefix, relative_one)
        historical_two = os.path.join(historical_prefix, relative_two)
        current_one = os.path.join(current_prefix, relative_one)
        current_two = os.path.join(current_prefix, relative_two)
        volume_one = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-one', 'vol-one.qcow2')
        volume_two = os.path.join(
            current_prefix, 'rootVolumes', 'acct-uuid', 'vol-two', 'vol-two.qcow2')

        for path in (current_one, current_two, volume_one, volume_two):
            self._touch(path)
        self.backing_by_path[os.path.normpath(volume_one)] = os.path.normpath(historical_one)
        self.backing_by_path[os.path.normpath(volume_two)] = os.path.normpath(historical_two)
        self.backing_by_path[os.path.normpath(current_one)] = ''
        self.backing_by_path[os.path.normpath(current_two)] = ''

        original_rebase = self._rebase

        def _fail_second_rebase(new_backing, current_path):
            if len(self.rebased) == 1:
                raise Exception('injected qemu-img failure')
            original_rebase(new_backing, current_path)

        sys.modules['zstacklib.utils.linux'].qcow2_rebase_no_check = _fail_second_rebase
        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [volume_one, volume_two], metadata_prefix, current_prefix)

        self.assertEqual('injected qemu-img failure', str(error.exception))

        sys.modules['zstacklib.utils.linux'].qcow2_rebase_no_check = original_rebase
        count = self.module.nfs_qcow2_prefix_rebase_backing_files(
            [volume_one, volume_two], metadata_prefix, current_prefix)

        self.assertEqual(1, count)
        self.assertEqual(os.path.normpath(current_one), self._get_backing(volume_one))
        self.assertEqual(os.path.normpath(current_two), self._get_backing(volume_two))

    def test_rejects_root_file_outside_current_nfs_mount(self):
        mount_parent = os.path.join(self.tmpdir, 'nfsprimarystorage')
        metadata_prefix = os.path.join(mount_parent, 'prim-metadata')
        historical_prefix = os.path.join(mount_parent, 'prim-history')
        current_prefix = os.path.join(mount_parent, 'prim-current')
        relative_cache = os.path.join(
            'imagecache', 'template', 'image-uuid', 'image-uuid.qcow2')
        historical_cache = os.path.join(historical_prefix, relative_cache)
        current_cache = os.path.join(current_prefix, relative_cache)
        external_volume = os.path.join(self.tmpdir, 'external', 'volume.qcow2')

        self._touch(current_cache)
        self._touch(external_volume)
        self.backing_by_path[os.path.normpath(external_volume)] = os.path.normpath(
            historical_cache)
        self.backing_by_path[os.path.normpath(current_cache)] = ''

        with self.assertRaises(Exception) as error:
            self.module.nfs_qcow2_prefix_rebase_backing_files(
                [external_volume], metadata_prefix, current_prefix)

        self.assertIn(
            "cannot rebase NFS backing chain: root file %s is outside current storage prefix" %
            os.path.normpath(external_volume),
            str(error.exception))
        self.assertEqual([], self.rebased)


if __name__ == '__main__':
    unittest.main()
