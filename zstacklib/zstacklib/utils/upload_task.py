# encoding: utf-8

import re
import errno
import hashlib
import threading
import logging
import xxhash
from zstacklib.utils import linux, lock
from zstacklib.utils.rangeset import RangeSet
from cherrypy._cpreqbody import Entity, Part, SizedReader

try:
    long
except NameError:
    long = int

logger = logging.getLogger(__name__)
BUFFER_SIZE = 16 * 1024 ** 2  # 16MB
# Per-slice retry budget chosen to tolerate repeated weak-network read/EOF/hash
# failures without keeping a broken upload task alive indefinitely.
UPLOAD_SLICE_FAILURE_TOLERANCE = 20
SUPPORTED_SLICE_HASH_ALGORITHMS = set(['md5', 'sha1', 'sha256', 'xxh3'])


class RetryableUploadError(Exception):
    def __init__(self, message, cause=None):
        Exception.__init__(self, message)
        self.cause = cause
        self.errno = getattr(cause, 'errno', None)


def is_no_space_error(reason):
    checked = set()
    depth = 0
    max_depth = 20
    while reason is not None and id(reason) not in checked and depth < max_depth:
        checked.add(id(reason))
        depth += 1

        if getattr(reason, 'errno', None) == errno.ENOSPC:
            return True

        msg = str(reason).lower()
        if 'no space left on device' in msg or \
                'no enough storage' in msg or \
                'not enough storage' in msg or \
                'not enough space' in msg:
            return True

        next_reason = None
        for attr in ['cause', '__cause__', '__context__']:
            next_reason = getattr(reason, attr, None)
            if next_reason is not None:
                break
        reason = next_reason

    return False


def get_hasher(algorithm, default="md5"):
    if algorithm == "xxh3":
        return xxhash.xxh3_64()

    if algorithm not in hashlib.algorithms_available:
        algorithm = default
    return getattr(hashlib, algorithm)()


def normalize_slice_hash_algorithm(algorithm):
    if algorithm is None or str(algorithm).strip() == '':
        raise Exception('missing X-HASH-ALGORITHM for X-SLICE-HASH')

    algorithm = str(algorithm).strip().lower()
    if algorithm not in SUPPORTED_SLICE_HASH_ALGORITHMS:
        raise Exception('unsupported hash algorithm: %s' % algorithm)
    return algorithm


class UploadParam(object):
    def __init__(self):
        self.task_uuid = None
        self.total_size = 0

        self.slice_offset = 0
        self.slice_size = 0
        self.slice_hash = None
        self.hash_algorithm = None


class UploadTasks(object):
    MAX_RECORDS = 80

    def __init__(self):
        self.tasks = {}

    def _expunge_oldest_task(self):
        key, ts = '', linux.get_current_timestamp()
        for k in self.tasks:
            task = self.tasks[k]

            if task.is_running():
                continue

            if task.lastOpTime < ts:
                key, ts = k, task.lastOpTime

        if key != '': del (self.tasks[key])

    @lock.lock('upload-task')
    def add_task(self, task):
        # Lock ensures check-then-act atomicity for duplicate detection.
        if task.taskUuid in self.tasks:
            if self.tasks[task.taskUuid].completed:
                self.tasks.pop(task.taskUuid, None)
            else:
                raise Exception("upload task %s is running" % task.taskUuid)

        if len(self.tasks) >= self.MAX_RECORDS:
            self._expunge_oldest_task()

        self.tasks[task.taskUuid] = task

    @lock.lock('upload-task')
    def get_task(self, task_uuid):
        return self.tasks.get(task_uuid)


class UploadTask(object):
    def __init__(self, task_uuid, install_path):
        self.completed = False
        self.taskUuid = task_uuid
        self.installPath = install_path
        self.expectedSize = 0
        self.downloadSize = 0
        self.lastError = None
        self.lastOpTime = linux.get_current_timestamp()
        self.failureTimes = {}
        self.lastTransientError = None
        self.close = None

        self.slice_uploaded = RangeSet()
        self.upload_lock = threading.Lock()

        self.task_created = False
        self.task_completing = False

    def fail(self, reason):
        self._fail(reason, True)

    def _fail_without_renew(self, reason):
        self._fail(reason, False)

    def _fail(self, reason, renew):
        close = None
        with self.upload_lock:
            if self.completed:
                return
            self.completed = True
            self.lastError = reason
            self.failureTimes.clear()
            if renew:
                self.lastOpTime = linux.get_current_timestamp()
            close = self.close
        if close:
            close()
        logger.info('task failed for %s: %s' % (self.taskUuid, reason))

    @staticmethod
    def upload_slice_failure_key(offset, size):
        return "%d:%d" % (offset, size)

    def mark_upload_slice_error(self, offset, size, reason):
        close = None
        failure_times = 0
        terminal_failed = False
        with self.upload_lock:
            if self.completed:
                return self.lastError is not None

            key = self.upload_slice_failure_key(offset, size)
            self.failureTimes[key] = self.failureTimes.get(key, 0) + 1
            failure_times = self.failureTimes[key]
            self.lastTransientError = reason
            terminal_failed = failure_times > UPLOAD_SLICE_FAILURE_TOLERANCE
            if terminal_failed:
                self.completed = True
                self.lastError = reason
                self.failureTimes.clear()
                close = self.close
        logger.warn('upload slice failed for %s, offset: %d, size: %d: %s, retryable failure count: %d/%d' %
                    (self.taskUuid, offset, size, reason, failure_times,
                     UPLOAD_SLICE_FAILURE_TOLERANCE))
        if terminal_failed:
            if close:
                close()
            logger.info('task failed for %s: %s' % (self.taskUuid, reason))
            return True

        return False

    def clear_upload_slice_error(self, offset, size):
        with self.upload_lock:
            self.failureTimes.pop(self.upload_slice_failure_key(offset, size), None)

    def handle_upload_slice_error(self, offset, size, reason):
        if is_no_space_error(reason):
            self._fail_without_renew(reason)
            return True

        return self.mark_upload_slice_error(offset, size, str(reason))

    def success(self):
        close = None
        with self.upload_lock:
            if self.completed:
                return
            self.completed = True
            self.failureTimes.clear()
            self.lastOpTime = linux.get_current_timestamp()
            close = self.close
        if close:
            close()

    def is_started(self):
        with self.upload_lock:
            return len(self.slice_uploaded.iv) > 0

    # Used by task-table expunge: "running" means allocated but no slice has started yet.
    def is_running(self):
        with self.upload_lock:
            return not (self.completed or len(self.slice_uploaded.iv) > 0)

    def renew(self):
        with self.upload_lock:
            self.lastOpTime = linux.get_current_timestamp()

    def all_slice_uploaded(self):
        with self.upload_lock:
            return self.task_completing

    def checked_download_size(self):
        with self.upload_lock:
            missing = self.slice_uploaded.missing(self.expectedSize, 1)
            if missing:
                return missing[0][0]

            return self.expectedSize

    def remaining_upload_size(self, total_size):
        if total_size <= 0:
            return 0

        with self.upload_lock:
            uploaded = len(self.slice_uploaded)

        if uploaded >= total_size:
            return 0
        return total_size - uploaded

    def record_slice_uploaded(self, offset, length):
        with self.upload_lock:
            end = offset + length
            newly_covered = length
            for start, stop in self.slice_uploaded:
                overlap_start = max(offset, start)
                overlap_end = min(end, stop)
                if overlap_start < overlap_end:
                    newly_covered -= overlap_end - overlap_start
            newly_covered = max(0, newly_covered)

            self.slice_uploaded.add(offset, end)
            # all slice uploaded
            if self.expectedSize > 0 and self.slice_uploaded.covered(0, self.expectedSize):
                self.task_completing = True
            return newly_covered

    def add_download_size(self, delta):
        with lock.NamedLock("upload-task-%s" % self.taskUuid):
            self.downloadSize += delta

    def complete_upload(self):
        raise NotImplementedError()

    def create_object(self, slice_offset):
        raise NotImplementedError()


class StorageObject(object):
    def seek(self, offset):
        raise NotImplementedError()

    def read(self, n):
        raise NotImplementedError()

    def write(self, content):
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()


class UploadHandler(object):
    def __init__(self, req, upload_tasks):
        self.req = req
        self.upload_tasks = upload_tasks

    @staticmethod
    def get_boundary(entity):
        if 'boundary' in entity.content_type.params:
            ib = entity.content_type.params['boundary'].strip('"')
        else:
            return None

        if not re.match("^[ -~]{0,200}[!-~]$", ib):
            raise ValueError('Invalid boundary in multipart form: %r' % (ib,))

        ib = ('--' + ib).encode('ascii')

        # Find the first marker
        while True:
            b = entity.readline()
            if not b:
                return None

            b = b.strip()
            if b == ib:
                break

        return ib

    @staticmethod
    def get_upload_param(headers):
        def get_long_field(key, default=None):
            v = headers.get(key, default)
            try:
                lv = long(v)
                if lv < 0:
                    raise ValueError
                return lv
            except ValueError:
                raise Exception('invalid header "%s": %s' % (key, v))

        up = UploadParam()

        if 'X-IMAGE-UUID' in headers:
            up.task_uuid = headers['X-IMAGE-UUID']
            up.total_size = get_long_field('X-IMAGE-SIZE')
        elif 'X-FILE-UUID' in headers:
            up.task_uuid = headers['X-FILE-UUID']
            up.total_size = get_long_field('X-FILE-SIZE')

        up.slice_offset = get_long_field('X-SLICE-OFFSET', default=0)
        up.slice_size = get_long_field('X-SLICE-SIZE', default=up.total_size)
        up.slice_hash = headers.get('X-SLICE-HASH', None)
        up.hash_algorithm = headers.get('X-HASH-ALGORITHM', None)
        if up.slice_hash is None and headers.get('X-SLICE-MD5', None):
            up.slice_hash = headers.get('X-SLICE-MD5', None)
            up.hash_algorithm = 'md5'
        elif up.slice_hash is not None:
            up.hash_algorithm = normalize_slice_hash_algorithm(up.hash_algorithm)

        if up.total_size <= 0:
            raise Exception('invalid total size header: %d' % up.total_size)

        if up.slice_size <= 0:
            raise Exception('invalid slice size header: %d' % up.slice_size)

        if up.slice_offset >= up.total_size:
            raise Exception('invalid slice offset header: %s, total_size: %d' %
                            (up.slice_offset, up.total_size))

        if up.slice_size > up.total_size - up.slice_offset:
            raise Exception('invalid slice range, offset=%d size=%d total_size=%d' %
                            (up.slice_offset, up.slice_size, up.total_size))
        return up

    def get_upload_task(self, param):
        task = self.upload_tasks.get_task(param.task_uuid)
        if task is None:
            raise Exception('upload task not found %s' % param.task_uuid)

        with task.upload_lock:
            last_error = task.lastError
            completed = task.completed
            if task.expectedSize == 0:
                task.expectedSize = param.total_size
            expected_size = task.expectedSize

        if last_error:
            raise Exception('upload task %s already failed: %s' % (param.task_uuid, last_error))

        if completed:
            raise Exception('upload task[uuid: %s] upload has completed' % param.task_uuid)

        if expected_size != param.total_size:
            raise Exception('upload task %s total size changed, expected: %d, actual: %d' %
                            (param.task_uuid, expected_size, param.total_size))

        if param.slice_offset == 0:
            remaining_size = task.remaining_upload_size(expected_size)
            err = task.check_capacity(remaining_size) if remaining_size > 0 else None
            if err:
                self._fail_task(task, err, renew=not is_no_space_error(err))

        return task

    @staticmethod
    def _fail_task(task, reason, renew=True):
        if renew:
            task.fail(reason)
        else:
            task._fail_without_renew(reason)
        raise Exception(reason)

    def upload_slice(self, entity, param, task):
        boundary = self.get_boundary(entity)
        if not boundary:
            err = 'unexpected post form'
            task.handle_upload_slice_error(param.slice_offset, param.slice_size, err)
            raise Exception(err)

        try:
            self.stream_body(entity, boundary, param, task)
        except Exception as e:
            task.handle_upload_slice_error(param.slice_offset, param.slice_size, e)
            raise Exception(str(e))

    @staticmethod
    def stream_body(entity, boundary, param, task):
        # type: (Entity, str, UploadParam, UploadTask) -> None

        image_obj = task.create_object(param.slice_offset)
        hasher = get_hasher(param.hash_algorithm) if param.slice_hash else None
        while True:
            headers = Part.read_headers(entity.fp)
            p = Part(entity.fp, headers, boundary)
            if not p.filename:
                continue

            logger.debug("uploading image %s: %s slice, offset: %d, content length: %d" %
                         (param.task_uuid, p.filename, param.slice_offset, param.slice_size))

            slice_downloaded_size = 0
            try:
                reader = SizedReader(p.fp, None, param.slice_size)
                remaining = param.slice_size
                bytes_read = 0
                chunks = []
                chunk_size = 32 * 1024
                while remaining > 0:
                    with task.upload_lock:
                        last_error = task.lastError
                    if last_error:
                        raise Exception(last_error)
                    try:
                        tmp = reader.read(min(chunk_size, remaining))
                    except Exception as e:
                        raise RetryableUploadError(
                            "failed to read upload body, taskUuid: %s, offset: %d, size: %d, completed: %d: %s" %
                            (param.task_uuid, param.slice_offset, param.slice_size, slice_downloaded_size, e), e)
                    datalen = len(tmp)
                    if datalen == 0:
                        break
                    task.renew()
                    chunks.append(tmp)
                    if hasher:
                        hasher.update(tmp)

                    remaining -= datalen
                    bytes_read += datalen
                    if bytes_read >= BUFFER_SIZE or remaining <= 0:
                        image_obj.write(b''.join(chunks))
                        task.add_download_size(bytes_read)
                        slice_downloaded_size += bytes_read
                        chunks = []
                        bytes_read = 0
            except Exception:
                if slice_downloaded_size:
                    task.add_download_size(-slice_downloaded_size)
                raise
            finally:
                image_obj.close()
            break

        if param.slice_size != slice_downloaded_size:
            task.add_download_size(-slice_downloaded_size)
            raise RetryableUploadError("incomplete image %s slice offset %d, completed %d, expected %d" %
                                       (param.task_uuid, param.slice_offset, slice_downloaded_size,
                                        param.slice_size))

        if param.slice_hash and param.slice_hash != hasher.hexdigest():
            actual_hash = hasher.hexdigest()
            task.add_download_size(-slice_downloaded_size)
            raise RetryableUploadError(
                "content %s hash not match, taskUuid: %s, offset: %d, size: %d, expected: %s, actual: %s" % (
                    param.hash_algorithm, param.task_uuid, param.slice_offset, param.slice_size, param.slice_hash,
                    actual_hash))

        newly_downloaded_size = task.record_slice_uploaded(param.slice_offset, param.slice_size)
        duplicate_size = slice_downloaded_size - newly_downloaded_size
        if duplicate_size > 0:
            task.add_download_size(-duplicate_size)
        task.clear_upload_slice_error(param.slice_offset, param.slice_size)
        task.renew()
        logger.debug("uploaded image %s slice offset: %d, content length: %d" %
                     (param.task_uuid, param.slice_offset, param.slice_size))

    def handle_upload(self):
        upload_param = self.get_upload_param(self.req.headers)
        task = self.get_upload_task(upload_param)
        task.renew()
        self.upload_slice(self.req.body, upload_param, task)
        if task.task_completing:
            task.complete_upload()
