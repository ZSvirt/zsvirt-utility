import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading

from zstacklib.utils import linux
from zstacklib.utils import plugin


class VmFileUploadError(Exception):
    pass


class VmFileUploadDaemon(plugin.TaskDaemon):
    def __init__(self, cmd, timeout):
        super(VmFileUploadDaemon, self).__init__(cmd, 'uploadFileToVm', timeout=timeout)
        self.process = None
        self.canceled = False
        self.lock = threading.Lock()

    def set_process(self, process):
        with self.lock:
            self.process = process
            canceled = self.canceled
        if canceled:
            self._terminate(process)

    def _cancel(self):
        with self.lock:
            self.canceled = True
            process = self.process
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            linux.kill_all_child_process(process.pid)
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass


class VmFileUploader(object):
    SOURCE_ROOT = '/var/lib/zstack/software-package/vm-upload'
    TARGET_ROOTS = ['/root', '/tmp']
    MAX_TIMEOUT_SECONDS = 259200
    TASK_UUID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
    USERNAME_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,31}$')

    def __init__(self, cmd):
        self.cmd = cmd

    def upload(self):
        task_uuid = self._validate_task_uuid(getattr(self.cmd, 'taskUuid', None))
        task_root = self._validate_task_root(task_uuid)
        self._validate_source_path(
            self.cmd.sourcePath, self.SOURCE_ROOT, 'sourcePath')
        source_path = self._validate_source_path(
            self.cmd.sourcePath, task_root, 'sourcePath')
        if not os.path.isfile(source_path):
            raise VmFileUploadError("sourcePath does not exist or is not a file")

        target_path = self._validate_remote_path(
            self.cmd.targetPath, self.TARGET_ROOTS, 'targetPath')
        self._validate_target()
        ssh_port, timeout = self._validate_limits()
        self._scp(source_path, target_path, ssh_port, timeout)

    @classmethod
    def cleanup_task(cls, task_uuid):
        task_uuid = cls._validate_task_uuid(task_uuid)
        task_root = cls._validate_task_root(task_uuid)
        if not os.path.lexists(task_root):
            return
        if os.path.islink(task_root) or not os.path.isdir(task_root):
            raise VmFileUploadError("upload task path is not a directory: %s" % task_root)
        try:
            shutil.rmtree(task_root)
        except OSError as error:
            raise VmFileUploadError("failed to clean upload task directory: %s" % error)

    @classmethod
    def _validate_task_uuid(cls, task_uuid):
        if not cls.TASK_UUID_PATTERN.match(task_uuid or ''):
            raise VmFileUploadError("taskUuid must be a 32-character lowercase hexadecimal UUID")
        return task_uuid

    @classmethod
    def _validate_task_root(cls, task_uuid):
        task_root = os.path.join(cls.SOURCE_ROOT, task_uuid)
        expected_real_root = os.path.join(os.path.realpath(cls.SOURCE_ROOT), task_uuid)
        if os.path.realpath(task_root) != expected_real_root:
            raise VmFileUploadError("sourcePath task directory must be %s" % task_root)
        return task_root

    @staticmethod
    def _validate_source_path(path, root, param_name):
        normalized, err = linux.validate_install_path(path, param_name)
        if err:
            raise VmFileUploadError(err)

        real_path = os.path.realpath(normalized)
        roots = root if isinstance(root, (list, tuple)) else [root]
        for allowed_root in roots:
            real_root = os.path.realpath(allowed_root)
            if real_path != real_root and real_path.startswith(real_root + os.sep):
                return real_path
        raise VmFileUploadError(
            "%s must be a file under %s" % (param_name, " or ".join(roots)))

    @staticmethod
    def _validate_remote_path(path, roots, param_name):
        normalized, err = linux.validate_install_path(path, param_name)
        if err:
            raise VmFileUploadError(err)

        for allowed_root in roots:
            normalized_root = os.path.normpath(allowed_root)
            if normalized != normalized_root and normalized.startswith(normalized_root + os.sep):
                return normalized
        raise VmFileUploadError(
            "%s must be a file under %s" % (param_name, " or ".join(roots)))

    def _validate_target(self):
        if not self._is_valid_ip(self.cmd.targetIp):
            raise VmFileUploadError("targetIp is not a valid IP address")
        if not self.USERNAME_PATTERN.match(self.cmd.username or ''):
            raise VmFileUploadError("username is invalid")
        if self.cmd.password is None:
            raise VmFileUploadError("password cannot be empty")

    @staticmethod
    def _is_valid_ip(address):
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                socket.inet_pton(family, address)
                return True
            except (socket.error, TypeError):
                pass
        return False

    def _validate_limits(self):
        try:
            ssh_port = int(self.cmd.sshPort)
            timeout = int(self.cmd.timeout)
        except (TypeError, ValueError):
            raise VmFileUploadError("sshPort and timeout must be integers")
        if ssh_port < 1 or ssh_port > 65535:
            raise VmFileUploadError("sshPort is out of range")
        if timeout < 1 or timeout > self.MAX_TIMEOUT_SECONDS:
            raise VmFileUploadError("timeout must be between 1 and 259200 seconds")
        return ssh_port, timeout

    def _scp(self, source_path, target_path, ssh_port, timeout):
        password_file = None
        try:
            password_file = tempfile.NamedTemporaryFile(
                prefix='zstack-vm-upload-', dir='/dev/shm', delete=False)
            os.chmod(password_file.name, 0o600)
            password = self.cmd.password if isinstance(self.cmd.password, bytes) \
                else self.cmd.password.encode('utf-8')
            password_file.write(password)
            password_file.flush()
            password_file.close()

            target_ip = '[%s]' % self.cmd.targetIp if ':' in self.cmd.targetIp else self.cmd.targetIp
            argv = [
                'timeout', str(timeout),
                'sshpass', '-f', password_file.name,
                'scp', '-P', str(ssh_port),
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                source_path,
                '%s@%s:%s' % (self.cmd.username, target_ip, target_path)
            ]
            with VmFileUploadDaemon(self.cmd, timeout) as daemon:
                process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                daemon.set_process(process)
                stdout, stderr = process.communicate()
            if process.returncode == 124:
                raise VmFileUploadError("SCP timed out after %s seconds" % timeout)
            if process.returncode != 0:
                detail = (stderr or stdout or b'').decode('utf-8', 'replace').strip()
                raise VmFileUploadError(
                    "SCP failed with return code %s: %s" % (process.returncode, detail))
        finally:
            if password_file is not None:
                try:
                    password_file.close()
                except Exception:
                    pass
                linux.rm_file_force(password_file.name)
