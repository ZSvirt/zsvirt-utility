# encoding: utf-8

import unittest
import sys
import types
import time
import threading
import errno


_MISSING = object()
_STUBBED_MODULES = [
    "xxhash",
    "zstacklib.utils.linux",
    "zstacklib.utils.lock",
    "zstacklib.utils.upload_task",
    "cherrypy",
    "cherrypy._cpreqbody",
]
_ORIGINAL_MODULES = dict((name, sys.modules.get(name, _MISSING)) for name in _STUBBED_MODULES)

if "xxhash" not in sys.modules:
    xxhash = types.ModuleType("xxhash")

    class FakeXxh3(object):
        def __init__(self):
            self.value = 0

        def update(self, data):
            for b in data:
                if not isinstance(b, int):
                    b = ord(b)
                self.value = (self.value + b) & 0xffffffffffffffff

        def hexdigest(self):
            return "%016x" % self.value

    xxhash.xxh3_64 = FakeXxh3
    sys.modules["xxhash"] = xxhash

linux = types.ModuleType("zstacklib.utils.linux")
linux.get_current_timestamp = lambda: int(time.time())
sys.modules["zstacklib.utils.linux"] = linux

lock = types.ModuleType("zstacklib.utils.lock")
lock.lock = lambda name: lambda func: func


class NamedLock(object):
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


lock.NamedLock = NamedLock
sys.modules["zstacklib.utils.lock"] = lock

import zstacklib.utils
_ORIGINAL_UTILS_ATTRS = {
    "linux": getattr(zstacklib.utils, "linux", _MISSING),
    "lock": getattr(zstacklib.utils, "lock", _MISSING),
    "upload_task": getattr(zstacklib.utils, "upload_task", _MISSING),
}
zstacklib.utils.linux = linux
zstacklib.utils.lock = lock

if "cherrypy._cpreqbody" not in sys.modules:
    cherrypy = types.ModuleType("cherrypy")
    cpreqbody = types.ModuleType("cherrypy._cpreqbody")
    cpreqbody.Entity = object
    cpreqbody.Part = object
    cpreqbody.SizedReader = object
    sys.modules["cherrypy"] = cherrypy
    sys.modules["cherrypy._cpreqbody"] = cpreqbody

from zstacklib.utils import upload_task as upload_task_module
from zstacklib.utils.upload_task import RetryableUploadError, UploadHandler, UploadParam, UploadTask, \
    UPLOAD_SLICE_FAILURE_TOLERANCE


def tearDownModule():
    for name, module in _ORIGINAL_MODULES.items():
        if module is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module

    for name, value in _ORIGINAL_UTILS_ATTRS.items():
        if value is _MISSING:
            try:
                delattr(zstacklib.utils, name)
            except AttributeError:
                pass
        else:
            setattr(zstacklib.utils, name, value)


class TestUploadTask(UploadTask):
    def check_capacity(self, expected_size):
        return None

    def complete_upload(self):
        pass

    def create_object(self, slice_offset):
        return None


class TestStorageObject(object):
    def __init__(self):
        self.contents = []
        self.closed = False

    def write(self, content):
        self.contents.append(content)

    def close(self):
        self.closed = True


class StreamUploadTask(TestUploadTask):
    def __init__(self, task_uuid, install_path):
        super(StreamUploadTask, self).__init__(task_uuid, install_path)
        self.storage_object = TestStorageObject()
        self.renewed_at = []

    def create_object(self, slice_offset):
        return self.storage_object

    def renew(self):
        super(StreamUploadTask, self).renew()
        self.renewed_at.append(self.lastOpTime)


class TestUploadTasks(object):
    def __init__(self, task):
        self.task = task

    def get_task(self, task_uuid):
        return self.task if task_uuid == self.task.taskUuid else None


def make_upload_param(task_uuid="task-1", total_size=100, slice_offset=0, slice_size=4):
    param = UploadParam()
    param.task_uuid = task_uuid
    param.total_size = total_size
    param.slice_offset = slice_offset
    param.slice_size = slice_size
    return param


def make_upload_headers(prefix="FILE", task_uuid="task-1", total_size=100, slice_offset=0, slice_size=4):
    return {
        'X-%s-UUID' % prefix: task_uuid,
        'X-%s-SIZE' % prefix: str(total_size),
        'X-SLICE-OFFSET': str(slice_offset),
        'X-SLICE-SIZE': str(slice_size),
    }


def make_hash(algorithm, data):
    hasher = upload_task_module.get_hasher(algorithm)
    hasher.update(data)
    return hasher.hexdigest()


class TestEntity(object):
    fp = object()


class TestRequest(object):
    def __init__(self, headers, body=None):
        self.headers = headers
        self.body = body


class TestPart(object):
    @staticmethod
    def read_headers(_):
        return {}

    def __init__(self, fp, headers, boundary):
        self.filename = "slice"
        self.fp = object()


class TestSizedReader(object):
    def __init__(self, fp, unused, size):
        self.chunks = [b'ab', b'cd', b'']

    def read(self, size):
        return self.chunks.pop(0)


class SequenceSizedReader(object):
    chunks = []

    def __init__(self, fp, unused, size):
        self.chunks = list(SequenceSizedReader.chunks)

    def read(self, size):
        if not self.chunks:
            return b''
        return self.chunks.pop(0)


class NonStandardNoSpaceError(Exception):
    errno = errno.ENOSPC

    def __str__(self):
        return "backend write rejected"


class CyclicError(Exception):
    def __init__(self):
        Exception.__init__(self, "cyclic error")
        self.cause = self


class TestUploadSliceFailureCounter(unittest.TestCase):
    def test_retryable_upload_error_has_errno_attr_without_cause(self):
        err = RetryableUploadError("unexpected EOF")

        self.assertTrue(hasattr(err, 'errno'))
        self.assertIsNone(err.errno)

    def test_no_space_error_handles_cyclic_cause_chain(self):
        self.assertFalse(upload_task_module.is_no_space_error(CyclicError()))

    def test_mark_upload_slice_error_keeps_retryable_until_tolerance(self):
        task = TestUploadTask("task-1", "/tmp/image")
        last_op_time = task.lastOpTime

        for i in range(UPLOAD_SLICE_FAILURE_TOLERANCE):
            reason = "unexpected EOF %d" % i
            self.assertFalse(task.mark_upload_slice_error(11, 4, reason))
            self.assertFalse(task.completed)
            self.assertEqual(reason, task.lastTransientError)
            self.assertEqual(last_op_time, task.lastOpTime)

        self.assertTrue(task.mark_upload_slice_error(11, 4, "unexpected EOF terminal"))
        self.assertTrue(task.completed)
        self.assertEqual("unexpected EOF terminal", task.lastError)
        self.assertEqual("unexpected EOF terminal", task.lastTransientError)
        self.assertEqual(last_op_time, task.lastOpTime)
        self.assertEqual({}, task.failureTimes)

    def test_mark_upload_slice_error_is_idempotent_after_terminal_failure(self):
        task = TestUploadTask("task-1", "/tmp/image")

        for i in range(UPLOAD_SLICE_FAILURE_TOLERANCE + 1):
            task.mark_upload_slice_error(11, 4, "first error")

        self.assertTrue(task.completed)
        self.assertEqual("first error", task.lastError)
        self.assertTrue(task.mark_upload_slice_error(11, 4, "new error"))
        self.assertEqual("first error", task.lastError)

    def test_mark_upload_slice_error_is_thread_safe(self):
        task = TestUploadTask("task-1", "/tmp/image")
        threads = []

        for i in range(UPLOAD_SLICE_FAILURE_TOLERANCE):
            t = threading.Thread(target=task.mark_upload_slice_error, args=(11, 4, "unexpected EOF"))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        key = task.upload_slice_failure_key(11, 4)
        self.assertEqual(UPLOAD_SLICE_FAILURE_TOLERANCE, task.failureTimes[key])
        self.assertFalse(task.completed)

    def test_mark_upload_slice_error_counts_per_slice(self):
        task = TestUploadTask("task-1", "/tmp/image")

        for i in range(UPLOAD_SLICE_FAILURE_TOLERANCE):
            self.assertFalse(task.mark_upload_slice_error(11, 4, "unexpected EOF"))

        self.assertFalse(task.mark_upload_slice_error(12, 4, "unexpected EOF"))
        self.assertFalse(task.completed)

    def test_clear_upload_slice_error_after_successful_slice(self):
        task = TestUploadTask("task-1", "/tmp/image")

        for i in range(UPLOAD_SLICE_FAILURE_TOLERANCE):
            self.assertFalse(task.mark_upload_slice_error(11, 4, "unexpected EOF"))

        task.clear_upload_slice_error(11, 4)
        self.assertFalse(task.mark_upload_slice_error(11, 4, "unexpected EOF"))
        self.assertFalse(task.completed)

    def test_is_running_only_before_any_slice_starts(self):
        task = TestUploadTask("task-1", "/tmp/image")
        task.expectedSize = 100

        self.assertTrue(task.is_running())
        task.record_slice_uploaded(0, 4)
        self.assertFalse(task.is_running())
        task.success()
        self.assertFalse(task.is_running())

    def test_success_clears_slice_failure_counter(self):
        task = TestUploadTask("task-1", "/tmp/image")

        task.mark_upload_slice_error(11, 4, "unexpected EOF")
        self.assertNotEqual({}, task.failureTimes)

        task.success()
        self.assertEqual({}, task.failureTimes)

    def test_get_upload_task_rejects_changed_total_size(self):
        task = TestUploadTask("task-1", "/tmp/image")
        handler = UploadHandler(None, TestUploadTasks(task))

        handler.get_upload_task(make_upload_param(total_size=100))
        with self.assertRaises(Exception):
            handler.get_upload_task(make_upload_param(total_size=200))

    def test_get_upload_task_no_space_fails_without_renew(self):
        task = TestUploadTask("task-1", "/tmp/image")
        task.lastOpTime = 100
        task.check_capacity = lambda expected_size: "no enough storage"
        handler = UploadHandler(None, TestUploadTasks(task))

        with self.assertRaises(Exception):
            handler.get_upload_task(make_upload_param(total_size=100, slice_offset=0))

        self.assertTrue(task.completed)
        self.assertEqual("no enough storage", task.lastError)
        self.assertEqual(100, task.lastOpTime)
        self.assertEqual({}, task.failureTimes)

    def test_get_upload_task_checks_remaining_unique_file_bytes(self):
        task = TestUploadTask("task-1", "/tmp/image")
        task.expectedSize = 10
        task.downloadSize = 12
        task.record_slice_uploaded(0, 6)
        checked_sizes = []
        task.check_capacity = lambda required_size: checked_sizes.append(required_size) or None
        handler = UploadHandler(None, TestUploadTasks(task))

        handler.get_upload_task(make_upload_param(total_size=10, slice_offset=0))

        self.assertEqual([4], checked_sizes)
        self.assertFalse(task.completed)

    def test_upload_slice_general_error_is_retryable(self):
        task = TestUploadTask("task-1", "/tmp/image")
        handler = UploadHandler(None, TestUploadTasks(task))
        handler.get_boundary = lambda entity: b'--boundary'

        def fail_stream_body(entity, boundary, param, upload_task):
            raise Exception("multipart reader failed")

        handler.stream_body = fail_stream_body

        with self.assertRaises(Exception):
            handler.upload_slice(TestEntity(), make_upload_param(slice_offset=11, slice_size=4), task)

        key = task.upload_slice_failure_key(11, 4)
        self.assertFalse(task.completed)
        self.assertEqual(1, task.failureTimes[key])
        self.assertEqual("multipart reader failed", task.lastTransientError)

    def test_upload_slice_no_space_error_fails_without_renew(self):
        task = TestUploadTask("task-1", "/tmp/image")
        task.lastOpTime = 100
        handler = UploadHandler(None, TestUploadTasks(task))
        handler.get_boundary = lambda entity: b'--boundary'

        def fail_stream_body(entity, boundary, param, upload_task):
            raise OSError(errno.ENOSPC, "No space left on device")

        handler.stream_body = fail_stream_body

        with self.assertRaises(Exception):
            handler.upload_slice(TestEntity(), make_upload_param(slice_offset=11, slice_size=4), task)

        self.assertTrue(task.completed)
        self.assertEqual(100, task.lastOpTime)
        self.assertEqual({}, task.failureTimes)

    def test_wrapped_no_space_error_fails_without_renew(self):
        task = TestUploadTask("task-1", "/tmp/image")
        task.lastOpTime = 100

        err = RetryableUploadError("failed to read upload body", NonStandardNoSpaceError())
        self.assertTrue(task.handle_upload_slice_error(11, 4, err))

        self.assertTrue(task.completed)
        self.assertEqual(100, task.lastOpTime)
        self.assertEqual({}, task.failureTimes)

    def test_handle_upload_renews_last_op_time_for_valid_request(self):
        old_get_current_timestamp = linux.get_current_timestamp
        linux.get_current_timestamp = lambda: 200
        try:
            task = TestUploadTask("task-1", "/tmp/image")
            task.lastOpTime = 100
            req = TestRequest({
                'X-IMAGE-UUID': 'task-1',
                'X-IMAGE-SIZE': '4',
                'X-SLICE-OFFSET': '0',
                'X-SLICE-SIZE': '4',
            }, TestEntity())
            handler = UploadHandler(req, TestUploadTasks(task))

            def fail_upload_slice(entity, param, upload_task):
                raise Exception("stop after renew")

            handler.upload_slice = fail_upload_slice
            with self.assertRaises(Exception):
                handler.handle_upload()

            self.assertEqual(200, task.lastOpTime)
            self.assertFalse(task.completed)
        finally:
            linux.get_current_timestamp = old_get_current_timestamp

    def test_last_op_time_renews_when_reading_slice_data(self):
        old_get_current_timestamp = linux.get_current_timestamp
        old_part = upload_task_module.Part
        old_sized_reader = upload_task_module.SizedReader
        timestamps = [100, 200, 300, 400]
        linux.get_current_timestamp = lambda: timestamps.pop(0)
        upload_task_module.Part = TestPart
        upload_task_module.SizedReader = TestSizedReader
        try:
            task = StreamUploadTask("task-1", "/tmp/image")
            param = make_upload_param(total_size=4, slice_size=4)

            UploadHandler.stream_body(TestEntity(), b'--boundary', param, task)

            self.assertEqual(400, task.lastOpTime)
            self.assertEqual([200, 300, 400], task.renewed_at)
            self.assertEqual([], timestamps)
            self.assertEqual([b'abcd'], task.storage_object.contents)
        finally:
            linux.get_current_timestamp = old_get_current_timestamp
            upload_task_module.Part = old_part
            upload_task_module.SizedReader = old_sized_reader

    def test_get_upload_param_accepts_file_headers_with_hash(self):
        param = UploadHandler.get_upload_param({
            'X-FILE-UUID': 'file-task-1',
            'X-FILE-SIZE': '8',
            'X-SLICE-OFFSET': '3',
            'X-SLICE-SIZE': '5',
            'X-SLICE-HASH': 'hash-value',
            'X-HASH-ALGORITHM': 'xxh3',
        })

        self.assertEqual('file-task-1', param.task_uuid)
        self.assertEqual(8, param.total_size)
        self.assertEqual(3, param.slice_offset)
        self.assertEqual(5, param.slice_size)
        self.assertEqual('hash-value', param.slice_hash)
        self.assertEqual('xxh3', param.hash_algorithm)

    def test_get_upload_param_rejects_slice_hash_without_algorithm(self):
        with self.assertRaises(Exception) as ctx:
            UploadHandler.get_upload_param({
                'X-FILE-UUID': 'file-task-1',
                'X-FILE-SIZE': '8',
                'X-SLICE-OFFSET': '0',
                'X-SLICE-SIZE': '4',
                'X-SLICE-HASH': 'hash-value',
            })

        self.assertIn('missing X-HASH-ALGORITHM', str(ctx.exception))

    def test_get_upload_param_rejects_unsupported_slice_hash_algorithm(self):
        with self.assertRaises(Exception) as ctx:
            UploadHandler.get_upload_param({
                'X-FILE-UUID': 'file-task-1',
                'X-FILE-SIZE': '8',
                'X-SLICE-OFFSET': '0',
                'X-SLICE-SIZE': '4',
                'X-SLICE-HASH': 'hash-value',
                'X-HASH-ALGORITHM': 'sha512',
            })

        self.assertIn('unsupported hash algorithm', str(ctx.exception))

    def test_get_upload_param_keeps_legacy_slice_md5_default(self):
        param = UploadHandler.get_upload_param({
            'X-FILE-UUID': 'file-task-1',
            'X-FILE-SIZE': '8',
            'X-SLICE-OFFSET': '0',
            'X-SLICE-SIZE': '4',
            'X-SLICE-MD5': 'legacy-md5',
        })

        self.assertEqual('legacy-md5', param.slice_hash)
        self.assertEqual('md5', param.hash_algorithm)

    def test_file_upload_variable_slices_complete_without_slice_index(self):
        old_part = upload_task_module.Part
        old_sized_reader = upload_task_module.SizedReader
        upload_task_module.Part = TestPart
        upload_task_module.SizedReader = SequenceSizedReader
        try:
            task = StreamUploadTask("file-task-1", "/tmp/file")

            SequenceSizedReader.chunks = [b'abc', b'']
            param = UploadHandler.get_upload_param({
                'X-FILE-UUID': 'file-task-1',
                'X-FILE-SIZE': '8',
                'X-SLICE-OFFSET': '0',
                'X-SLICE-SIZE': '3',
                'X-SLICE-HASH': make_hash('xxh3', b'abc'),
                'X-HASH-ALGORITHM': 'xxh3',
            })
            task.expectedSize = param.total_size
            UploadHandler.stream_body(TestEntity(), b'--boundary', param, task)
            self.assertFalse(task.all_slice_uploaded())
            self.assertEqual(3, task.checked_download_size())

            SequenceSizedReader.chunks = [b'defgh', b'']
            param = UploadHandler.get_upload_param({
                'X-FILE-UUID': 'file-task-1',
                'X-FILE-SIZE': '8',
                'X-SLICE-OFFSET': '3',
                'X-SLICE-SIZE': '5',
                'X-SLICE-HASH': make_hash('xxh3', b'defgh'),
                'X-HASH-ALGORITHM': 'xxh3',
            })
            UploadHandler.stream_body(TestEntity(), b'--boundary', param, task)

            self.assertTrue(task.all_slice_uploaded())
            self.assertEqual(8, task.checked_download_size())
        finally:
            upload_task_module.Part = old_part
            upload_task_module.SizedReader = old_sized_reader

    def assert_duplicate_and_overlap_slices_are_counted_once(self, header_prefix):
        old_part = upload_task_module.Part
        old_sized_reader = upload_task_module.SizedReader
        upload_task_module.Part = TestPart
        upload_task_module.SizedReader = SequenceSizedReader
        try:
            task = StreamUploadTask("%s-task-1" % header_prefix.lower(), "/tmp/upload")
            task.expectedSize = 8

            SequenceSizedReader.chunks = [b'abcd', b'']
            UploadHandler.stream_body(TestEntity(), b'--boundary', UploadHandler.get_upload_param(
                make_upload_headers(header_prefix, task.taskUuid, 8, 0, 4)), task)

            SequenceSizedReader.chunks = [b'abcd', b'']
            UploadHandler.stream_body(TestEntity(), b'--boundary', UploadHandler.get_upload_param(
                make_upload_headers(header_prefix, task.taskUuid, 8, 0, 4)), task)

            SequenceSizedReader.chunks = [b'cdef', b'']
            UploadHandler.stream_body(TestEntity(), b'--boundary', UploadHandler.get_upload_param(
                make_upload_headers(header_prefix, task.taskUuid, 8, 2, 4)), task)

            self.assertEqual(6, task.downloadSize)
            self.assertEqual(6, task.checked_download_size())
        finally:
            upload_task_module.Part = old_part
            upload_task_module.SizedReader = old_sized_reader

    def test_image_upload_stream_body_does_not_double_count_duplicate_or_overlap_slices(self):
        self.assert_duplicate_and_overlap_slices_are_counted_once('IMAGE')

    def test_file_upload_stream_body_does_not_double_count_duplicate_or_overlap_slices(self):
        self.assert_duplicate_and_overlap_slices_are_counted_once('FILE')

    def test_file_upload_hash_mismatch_is_retryable(self):
        old_part = upload_task_module.Part
        old_sized_reader = upload_task_module.SizedReader
        upload_task_module.Part = TestPart
        upload_task_module.SizedReader = SequenceSizedReader
        try:
            task = StreamUploadTask("file-task-1", "/tmp/file")
            task.expectedSize = 4
            SequenceSizedReader.chunks = [b'abcd', b'']
            param = UploadHandler.get_upload_param({
                'X-FILE-UUID': 'file-task-1',
                'X-FILE-SIZE': '4',
                'X-SLICE-OFFSET': '0',
                'X-SLICE-SIZE': '4',
                'X-SLICE-HASH': 'bad-hash',
                'X-HASH-ALGORITHM': 'xxh3',
            })

            with self.assertRaises(RetryableUploadError):
                UploadHandler.stream_body(TestEntity(), b'--boundary', param, task)

            self.assertFalse(task.completed)
            self.assertEqual(0, task.downloadSize)
        finally:
            upload_task_module.Part = old_part
            upload_task_module.SizedReader = old_sized_reader


if __name__ == "__main__":
    unittest.main()
