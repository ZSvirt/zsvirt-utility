import unittest
import sys
import types

def _stub_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class FakeLogger(object):
    def debug(self, *args, **kwargs):
        pass

    def warn(self, *args, **kwargs):
        pass


form = _stub_module('zstacklib.utils.form')
report = _stub_module('zstacklib.utils.report')
shell = _stub_module('zstacklib.utils.shell')
bash = _stub_module('zstacklib.utils.bash')
lock = _stub_module('zstacklib.utils.lock')
log = _stub_module('zstacklib.utils.log')
linux = _stub_module('zstacklib.utils.linux')
thread = _stub_module('zstacklib.utils.thread')
sanlock = _stub_module('zstacklib.utils.sanlock')
remote_storage = _stub_module('zstacklib.utils.remoteStorage')

log.get_logger = lambda name: FakeLogger()
shell.call = lambda *args, **kwargs: None
bash.in_bash = lambda func: func
lock.file_lock = lambda *args, **kwargs: (lambda func: func)
linux.retry = lambda *args, **kwargs: (lambda func: func)
linux.retry_with_check = lambda *args, **kwargs: (lambda func: func)
linux.get_fs_type = lambda path: None
remote_storage.RemoteStorage = object

import zstacklib.utils

zstacklib.utils.form = form
zstacklib.utils.report = report
zstacklib.utils.shell = shell
zstacklib.utils.bash = bash
zstacklib.utils.lock = lock
zstacklib.utils.log = log
zstacklib.utils.linux = linux
zstacklib.utils.thread = thread
zstacklib.utils.sanlock = sanlock
zstacklib.utils.remoteStorage = remote_storage

from zstacklib.utils import lvm


class AttributePatch(object):
    def __init__(self, obj, name, value):
        self.obj = obj
        self.name = name
        self.value = value
        self.old_value = None

    def __enter__(self):
        self.old_value = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, exc_type, exc_value, traceback):
        setattr(self.obj, self.name, self.old_value)


class MultiPatch(object):
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for patch in self.patches:
            patch.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        for patch in reversed(self.patches):
            patch.__exit__(exc_type, exc_value, traceback)


class CallRecorder(object):
    def __init__(self, return_value=None, side_effect=None):
        self.return_value = return_value
        self.side_effect = side_effect
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        if self.side_effect:
            raise self.side_effect
        return self.return_value

    def assert_not_called(self):
        assert self.call_count == 0


class FakeOperateLv(object):
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class TestDiscardLv(unittest.TestCase):
    def test_discard_probe_failure_is_best_effort(self):
        operate_lv = CallRecorder()
        shell_call = CallRecorder()
        with MultiPatch(
                AttributePatch(lvm, 'lv_exists', CallRecorder(return_value=True)),
                AttributePatch(lvm, 'get_lv_discard_max_bytes', CallRecorder(side_effect=Exception('probe failed'))),
                AttributePatch(lvm, 'OperateLv', operate_lv),
                AttributePatch(lvm.shell, 'call', shell_call)):
            self.assertFalse(lvm.discard_lv('/dev/vg/lv', lvm.LvDiscardStrategy.ALWAYS))
            operate_lv.assert_not_called()
            shell_call.assert_not_called()

    def test_lv_in_use_probe_failure_is_best_effort(self):
        shell_call = CallRecorder()
        with MultiPatch(
                AttributePatch(lvm, 'lv_exists', CallRecorder(return_value=True)),
                AttributePatch(lvm, 'get_lv_discard_max_bytes',
                               CallRecorder(return_value=lvm.PV_DISCARD_MIN_SIZE_IN_BYTES)),
                AttributePatch(lvm, 'OperateLv', FakeOperateLv),
                AttributePatch(lvm, 'lv_in_use', CallRecorder(side_effect=Exception('lvs failed'))),
                AttributePatch(lvm.shell, 'call', shell_call)):
            self.assertFalse(lvm.discard_lv('/dev/vg/lv', lvm.LvDiscardStrategy.ALWAYS))
            shell_call.assert_not_called()

    def test_expired_deadline_skips_discard(self):
        shell_call = CallRecorder()
        with MultiPatch(
                AttributePatch(lvm, 'lv_exists', CallRecorder(return_value=True)),
                AttributePatch(lvm, 'get_lv_discard_max_bytes',
                               CallRecorder(return_value=lvm.PV_DISCARD_MIN_SIZE_IN_BYTES)),
                AttributePatch(lvm, 'OperateLv', FakeOperateLv),
                AttributePatch(lvm, 'lv_in_use', CallRecorder(return_value=False)),
                AttributePatch(lvm, 'get_lv_discard_granularity', CallRecorder(return_value=512)),
                AttributePatch(lvm.shell, 'call', shell_call)):
            self.assertFalse(lvm.discard_lv('/dev/vg/lv', lvm.LvDiscardStrategy.ALWAYS, deadline=0))
            shell_call.assert_not_called()


class TestCreateLv(unittest.TestCase):
    def test_failed_concurrent_creator_does_not_zero_existing_lv(self):
        lv_exists_results = iter([False, True])
        dd_zero = CallRecorder()
        deactive_lv = CallRecorder()

        with MultiPatch(
                AttributePatch(lvm, 'lv_exists', lambda path: next(lv_exists_results)),
                AttributePatch(lvm, 'get_allocated_pvs', CallRecorder(return_value=[])),
                AttributePatch(lvm, 'subcmd', CallRecorder(return_value='lvcreate')),
                AttributePatch(lvm.bash, 'bash_roe',
                               CallRecorder(return_value=(5, '', 'already exists'))),
                AttributePatch(lvm, 'dd_zero', dd_zero),
                AttributePatch(lvm, 'deactive_lv', deactive_lv)):
            self.assertFalse(lvm.create_lv_from_absolute_path(
                '/dev/test-vg/test-lv', 4096, lock=True, exact_size=True))

        dd_zero.assert_not_called()
        deactive_lv.assert_not_called()


if __name__ == '__main__':
    unittest.main()
