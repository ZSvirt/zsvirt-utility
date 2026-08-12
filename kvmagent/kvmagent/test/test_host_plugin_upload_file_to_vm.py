# encoding: utf-8

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest

try:
    import mock
except ImportError:
    from unittest import mock

import kvmagent.plugins
import zstacklib.utils


def identity_decorator(func):
    return func


qemu = types.ModuleType('zstacklib.utils.qemu')
sys.modules['zstacklib.utils.qemu'] = qemu
zstacklib.utils.qemu = qemu

vm_plugin = types.ModuleType('kvmagent.plugins.vm_plugin')
vm_plugin.LibvirtAutoReconnect = identity_decorator
vm_plugin.VirtualizerInfoTO = object
sys.modules['kvmagent.plugins.vm_plugin'] = vm_plugin
kvmagent.plugins.vm_plugin = vm_plugin

imagestore = types.ModuleType('kvmagent.plugins.imagestore')
imagestore.ImageStoreClient = object
sys.modules['kvmagent.plugins.imagestore'] = imagestore
kvmagent.plugins.imagestore = imagestore

prometheus = types.ModuleType('kvmagent.plugins.prometheus')
prometheus.get_service_type_map = lambda: {}
prometheus.register_service_type = lambda *args, **kwargs: None
sys.modules['kvmagent.plugins.prometheus'] = prometheus
kvmagent.plugins.prometheus = prometheus

from kvmagent.plugins import host_plugin
from kvmagent.plugins.vm_file_uploader import VmFileUploader, VmFileUploadError
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import log
from zstacklib.utils import plugin as task_plugin


class TestUploadFileToVm(unittest.TestCase):
    TASK_UUID = 'a' * 32

    def setUp(self):
        self.plugin = host_plugin.HostPlugin()

    @staticmethod
    def make_cmd(target_ip='192.168.100.10', target_path='/root/package.tar.gz'):
        cmd = host_plugin.UploadFileToVmCmd()
        cmd.taskUuid = TestUploadFileToVm.TASK_UUID
        cmd.sourcePath = '/var/lib/zstack/software-package/vm-upload/%s/package.tar.gz' % cmd.taskUuid
        cmd.targetIp = target_ip
        cmd.targetPath = target_path
        cmd.username = 'root'
        cmd.sshPort = 22
        cmd.timeout = 600
        cmd.password = 'secret-password'
        cmd.threadContext = None
        return cmd

    @classmethod
    def make_request(cls, target_ip='192.168.100.10', target_path='/root/package.tar.gz'):
        return {http.REQUEST_BODY: jsonobject.dumps(cls.make_cmd(target_ip, target_path))}

    def test_endpoint_delegates_to_vm_file_uploader(self):
        with mock.patch('kvmagent.plugins.host_plugin.VmFileUploader') as uploader_class:
            rsp = jsonobject.loads(self.plugin.upload_file_to_vm(self.make_request()))

        self.assertTrue(rsp.success, 'endpoint must return success when uploader succeeds')
        uploader_class.assert_called_once()
        uploader_class.return_value.upload.assert_called_once_with()
        self.assertEqual('192.168.100.10', uploader_class.call_args[0][0].targetIp)

    def test_endpoint_returns_vm_file_upload_error(self):
        with mock.patch(
                'kvmagent.plugins.host_plugin.VmFileUploader.upload',
                side_effect=VmFileUploadError('sourcePath is invalid')):
            rsp = jsonobject.loads(self.plugin.upload_file_to_vm(self.make_request()))

        self.assertFalse(rsp.success, 'uploader validation error must fail the endpoint response')
        self.assertEqual('sourcePath is invalid', rsp.error)

    def test_cleanup_endpoint_delegates_to_validated_task_cleanup(self):
        cmd = host_plugin.CleanupUploadFileToVmCmd()
        cmd.taskUuid = self.TASK_UUID
        req = {http.REQUEST_BODY: jsonobject.dumps(cmd)}

        with mock.patch(
                'kvmagent.plugins.host_plugin.VmFileUploader.cleanup_task') as cleanup_task:
            rsp = jsonobject.loads(self.plugin.cleanup_upload_file_to_vm(req))

        self.assertTrue(rsp.success)
        cleanup_task.assert_called_once_with(self.TASK_UUID)

    def test_unexpected_upload_error_does_not_log_password(self):
        with mock.patch(
                'kvmagent.plugins.host_plugin.VmFileUploader.upload',
                side_effect=OSError('cannot create password file')), \
                mock.patch('kvmagent.kvmagent.logger.warn') as reply_logger, \
                mock.patch('kvmagent.plugins.host_plugin.logger.exception'):
            rsp = jsonobject.loads(self.plugin.upload_file_to_vm(self.make_request()))

        self.assertFalse(rsp.success)
        self.assertNotIn('secret-password', repr(reply_logger.call_args_list))

    def test_malformed_request_does_not_log_password(self):
        req = {
            http.REQUEST_BODY: '{"password":"secret-password",',
        }

        with mock.patch('kvmagent.kvmagent.logger.warn') as reply_logger, \
                mock.patch('kvmagent.plugins.host_plugin.logger.warn') as host_logger, \
                mock.patch('kvmagent.plugins.host_plugin.logger.exception') as exception_logger:
            rsp = jsonobject.loads(self.plugin.upload_file_to_vm(req))

        self.assertFalse(rsp.success)
        self.assertEqual('invalid request body', rsp.error)
        logged = repr([
            reply_logger.call_args_list,
            host_logger.call_args_list,
            exception_logger.call_args_list,
        ])
        self.assertNotIn('secret-password', logged)

    def test_http_download_is_tracked_for_management_node_resume(self):
        task_uuid = 'b' * 32
        install_path = '/var/lib/zstack/software-package/vm-upload/%s/package.tar.gz' % task_uuid
        cmd = kvmagent.AgentCommand()
        cmd.taskUuid = task_uuid
        cmd.installPath = install_path
        cmd.url = 'https://example.com/package.tar.gz'
        cmd.urlScheme = 'https'
        cmd.timeout = 600
        cmd.threadContext = None
        req = {http.REQUEST_BODY: jsonobject.dumps(cmd)}

        def complete_download():
            task = self.plugin.upload_tasks.get_task(task_uuid)
            self.assertIsNotNone(task, 'download must be discoverable while the MN request is detached')
            self.assertFalse(task.completed)
            return True, None

        with mock.patch(
                'kvmagent.plugins.host_plugin.linux.validate_install_path',
                return_value=(install_path, None)), \
                mock.patch('kvmagent.plugins.host_plugin.report.Report.from_spec'), \
                mock.patch('kvmagent.plugins.host_plugin.FileDownloader') as downloader_class, \
                mock.patch('kvmagent.plugins.host_plugin.os.path.exists', return_value=True), \
                mock.patch('kvmagent.plugins.host_plugin.os.path.getsize', return_value=4), \
                mock.patch(
                    'kvmagent.plugins.host_plugin.linux.get_file_md5sum_hashlib',
                    return_value='test-md5'):
            downloader_class.return_value.get_url_file_size.return_value = (True, 4)
            downloader_class.return_value.download.side_effect = complete_download
            rsp = jsonobject.loads(self.plugin.download_file(req))

            progress_cmd = kvmagent.AgentCommand()
            progress_cmd.taskUuid = task_uuid
            progress_req = {http.REQUEST_BODY: jsonobject.dumps(progress_cmd)}
            progress = jsonobject.loads(self.plugin.get_upload_progress(progress_req))

        self.assertTrue(rsp.success)
        self.assertTrue(progress.success)
        self.assertTrue(progress.completed)
        self.assertEqual(4, progress.downloadSize)
        self.assertEqual('test-md5', progress.md5sum)

    def test_cancel_stops_running_scp_and_preserves_source(self):
        api_id = 'cancel-upload-file-to-vm'
        cmd = self.make_cmd()
        cmd.threadContext = jsonobject.loads(jsonobject.dumps({'api': api_id}))
        uploader = VmFileUploader(cmd)
        password_file = mock.Mock()
        password_file.name = '/dev/shm/zstack-vm-upload-cancel'
        upload_errors = []

        class BlockingProcess(object):
            def __init__(self):
                self.pid = 12345
                self.returncode = None
                self.started = threading.Event()
                self.stopped = threading.Event()

            def communicate(self):
                self.started.set()
                self.stopped.wait(2)
                return b'', b'canceled'

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15
                self.stopped.set()

        process = BlockingProcess()

        def run_upload():
            try:
                uploader.upload()
            except Exception as error:
                upload_errors.append(error)

        with mock.patch.object(
                uploader, '_validate_source_path', return_value=cmd.sourcePath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch.object(uploader, '_validate_remote_path', return_value=cmd.targetPath), \
                mock.patch.object(uploader, '_validate_target'), \
                mock.patch.object(uploader, '_validate_limits', return_value=(22, 600)), \
                mock.patch('kvmagent.plugins.vm_file_uploader.tempfile.NamedTemporaryFile',
                           return_value=password_file), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.chmod'), \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen', return_value=process), \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.kill_all_child_process'), \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.rm_file_force'):
            upload_thread = threading.Thread(target=run_upload)
            upload_thread.start()
            self.assertTrue(process.started.wait(1), 'SCP process must start')
            self.assertEqual(1, task_plugin.TaskManager.cancel_task(api_id))
            upload_thread.join(2)

        self.assertFalse(upload_thread.is_alive(), 'cancel must stop the running SCP process')
        self.assertTrue(upload_errors)
        self.assertIsInstance(upload_errors[0], VmFileUploadError)

    def test_upload_uses_password_file_and_ipv6_scp_target(self):
        uploader = VmFileUploader(self.make_cmd('2001:db8::10'))
        password_file = mock.Mock()
        password_file.name = '/dev/shm/zstack-vm-upload-test'
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b'', b'')

        with mock.patch.object(
                uploader, '_validate_source_path',
                return_value=self.make_cmd().sourcePath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch('kvmagent.plugins.vm_file_uploader.tempfile.NamedTemporaryFile', return_value=password_file), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.chmod') as chmod, \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen', return_value=process) as popen, \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.rm_file_force') as remove_password_file:
            uploader.upload()

        password_file.write.assert_called_once_with(b'secret-password')
        chmod.assert_called_once_with(password_file.name, 0o600)
        remove_password_file.assert_called_once_with(password_file.name)

        argv = popen.call_args[0][0]
        self.assertIn('root@[2001:db8::10]:/root/package.tar.gz', argv,
                      'IPv6 SCP target must be enclosed in brackets: argv=%s' % argv)
        self.assertNotIn('secret-password', argv,
                         'password must not appear in process argv: argv=%s' % argv)
        self.assertEqual(['timeout', '600'], argv[:2],
                         'SCP must be bounded by the requested timeout: argv=%s' % argv)

    def test_invalid_source_path_is_rejected_before_scp(self):
        uploader = VmFileUploader(self.make_cmd())
        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                return_value=(None, 'sourcePath must be a file under staging root')), \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen') as popen, \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertIn('sourcePath', str(error.exception),
                      'path validation error must identify sourcePath: error=%s' % error.exception)
        popen.assert_not_called()

    def test_source_path_must_be_under_current_task_uuid(self):
        cmd = self.make_cmd()
        cmd.sourcePath = '/var/lib/zstack/software-package/vm-upload/%s/package.tar.gz' % ('b' * 32)
        uploader = VmFileUploader(cmd)

        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                side_effect=lambda path, param_name: (path, None)), \
                mock.patch(
                    'kvmagent.plugins.vm_file_uploader.os.path.realpath',
                    side_effect=lambda path: path), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch.object(uploader, '_validate_remote_path', return_value=cmd.targetPath), \
                mock.patch.object(uploader, '_validate_target'), \
                mock.patch.object(uploader, '_validate_limits', return_value=(22, 600)), \
                mock.patch.object(uploader, '_scp') as scp, \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertIn(cmd.taskUuid, str(error.exception),
                      'source path error must identify the current upload task: error=%s' % error.exception)
        scp.assert_not_called()

    def test_successful_scp_preserves_source_until_task_cleanup(self):
        source_root = tempfile.mkdtemp(prefix='zstack-vm-upload-test-')
        task_root = os.path.join(source_root, self.TASK_UUID)
        source_path = os.path.join(task_root, 'package.tar.gz')
        os.makedirs(task_root)
        with open(source_path, 'wb') as stream:
            stream.write(b'vddk')

        cmd = self.make_cmd()
        cmd.sourcePath = source_path
        uploader = VmFileUploader(cmd)
        try:
            with mock.patch.object(VmFileUploader, 'SOURCE_ROOT', source_root), \
                    mock.patch.object(uploader, '_scp') as scp:
                uploader.upload()

            scp.assert_called_once()
            self.assertTrue(os.path.isfile(source_path),
                            'the source must remain available for long-job resume')
            self.assertTrue(os.path.isdir(task_root),
                            'the task directory must remain until terminal cleanup')

            with mock.patch.object(VmFileUploader, 'SOURCE_ROOT', source_root):
                VmFileUploader.cleanup_task(self.TASK_UUID)

            self.assertFalse(os.path.exists(task_root))
        finally:
            shutil.rmtree(source_root, ignore_errors=True)

    def test_cleanup_task_removes_only_validated_task_directory(self):
        source_root = tempfile.mkdtemp(prefix='zstack-vm-upload-cleanup-test-')
        task_root = os.path.join(source_root, self.TASK_UUID)
        sibling_root = os.path.join(source_root, 'b' * 32)
        os.makedirs(task_root)
        os.makedirs(sibling_root)
        with open(os.path.join(task_root, 'package.tar.gz'), 'wb') as stream:
            stream.write(b'vddk')

        try:
            with mock.patch.object(VmFileUploader, 'SOURCE_ROOT', source_root):
                VmFileUploader.cleanup_task(self.TASK_UUID)

            self.assertFalse(os.path.exists(task_root))
            self.assertTrue(os.path.isdir(sibling_root))
        finally:
            shutil.rmtree(source_root, ignore_errors=True)

    def test_cleanup_task_rejects_invalid_uuid(self):
        with self.assertRaises(VmFileUploadError):
            VmFileUploader.cleanup_task('../invalid')

    def test_cleanup_task_does_not_follow_task_directory_symlink(self):
        source_root = tempfile.mkdtemp(prefix='zstack-vm-upload-cleanup-root-')
        outside_root = tempfile.mkdtemp(prefix='zstack-vm-upload-cleanup-outside-')
        outside_file = os.path.join(outside_root, 'keep-me')
        with open(outside_file, 'wb') as stream:
            stream.write(b'keep')
        os.symlink(outside_root, os.path.join(source_root, self.TASK_UUID))

        try:
            with mock.patch.object(VmFileUploader, 'SOURCE_ROOT', source_root), \
                    self.assertRaises(VmFileUploadError):
                VmFileUploader.cleanup_task(self.TASK_UUID)

            self.assertTrue(os.path.isfile(outside_file))
        finally:
            shutil.rmtree(source_root, ignore_errors=True)
            shutil.rmtree(outside_root, ignore_errors=True)

    def test_failed_scp_keeps_uploaded_source(self):
        source_root = tempfile.mkdtemp(prefix='zstack-vm-upload-test-')
        task_root = os.path.join(source_root, self.TASK_UUID)
        source_path = os.path.join(task_root, 'package.tar.gz')
        os.makedirs(task_root)
        with open(source_path, 'wb') as stream:
            stream.write(b'vddk')

        cmd = self.make_cmd()
        cmd.sourcePath = source_path
        uploader = VmFileUploader(cmd)
        try:
            with mock.patch.object(VmFileUploader, 'SOURCE_ROOT', source_root), \
                    mock.patch.object(
                        uploader, '_scp', side_effect=VmFileUploadError('SCP failed')), \
                    self.assertRaises(VmFileUploadError):
                uploader.upload()

            self.assertTrue(os.path.isfile(source_path),
                            'failed SCP must preserve the physical-host source file')
            self.assertTrue(os.path.isdir(task_root),
                            'failed SCP must preserve the upload task directory')
        finally:
            shutil.rmtree(source_root, ignore_errors=True)

    def test_invalid_task_uuid_is_rejected_before_scp(self):
        cmd = self.make_cmd()
        cmd.taskUuid = '../another-task'
        uploader = VmFileUploader(cmd)

        with mock.patch.object(uploader, '_validate_source_path', return_value=cmd.sourcePath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch.object(uploader, '_validate_remote_path', return_value=cmd.targetPath), \
                mock.patch.object(uploader, '_validate_target'), \
                mock.patch.object(uploader, '_validate_limits', return_value=(22, 600)), \
                mock.patch.object(uploader, '_scp') as scp, \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertIn('taskUuid', str(error.exception),
                      'invalid upload task error must identify taskUuid: error=%s' % error.exception)
        scp.assert_not_called()

    def test_task_directory_symlink_cannot_escape_staging_root(self):
        cmd = self.make_cmd()
        task_root = '/var/lib/zstack/software-package/vm-upload/%s' % cmd.taskUuid
        uploader = VmFileUploader(cmd)

        def escaped_task_realpath(path):
            if path == cmd.sourcePath:
                return '/outside/%s/package.tar.gz' % cmd.taskUuid
            if path == task_root:
                return '/outside/%s' % cmd.taskUuid
            return path

        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                side_effect=lambda path, param_name: (path, None)), \
                mock.patch(
                    'kvmagent.plugins.vm_file_uploader.os.path.realpath',
                    side_effect=escaped_task_realpath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch.object(uploader, '_scp') as scp, \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertIn(VmFileUploader.SOURCE_ROOT, str(error.exception),
                      'escaped task directory error must identify the staging root: error=%s' % error.exception)
        scp.assert_not_called()

    def test_task_directory_cannot_redirect_to_another_task(self):
        cmd = self.make_cmd()
        task_root = '/var/lib/zstack/software-package/vm-upload/%s' % cmd.taskUuid
        other_task_root = '/var/lib/zstack/software-package/vm-upload/%s' % ('b' * 32)
        uploader = VmFileUploader(cmd)

        def redirected_task_realpath(path):
            if path == task_root:
                return other_task_root
            if path == cmd.sourcePath:
                return '%s/package.tar.gz' % other_task_root
            return path

        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                side_effect=lambda path, param_name: (path, None)), \
                mock.patch(
                    'kvmagent.plugins.vm_file_uploader.os.path.realpath',
                    side_effect=redirected_task_realpath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch.object(uploader, '_scp') as scp, \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertIn(cmd.taskUuid, str(error.exception),
                      'redirected task directory error must identify the current task: error=%s' % error.exception)
        scp.assert_not_called()

    def test_tmp_target_path_is_allowed(self):
        uploader = VmFileUploader(self.make_cmd(target_path='/tmp/vddk.tar.gz'))
        password_file = mock.Mock()
        password_file.name = '/dev/shm/zstack-vm-upload-tmp-target'
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b'', b'')

        with mock.patch.object(
                uploader, '_validate_source_path',
                return_value=self.make_cmd().sourcePath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch('kvmagent.plugins.vm_file_uploader.tempfile.NamedTemporaryFile', return_value=password_file), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.chmod'), \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen', return_value=process) as popen, \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.rm_file_force'):
            uploader.upload()

        argv = popen.call_args[0][0]
        self.assertIn('root@192.168.100.10:/tmp/vddk.tar.gz', argv,
                      'SCP must use the validated /tmp target path: argv=%s' % argv)

    def test_remote_target_path_does_not_follow_host_realpath(self):
        uploader = VmFileUploader(self.make_cmd(target_path='/tmp/vddk.tar.gz'))
        password_file = mock.Mock()
        password_file.name = '/dev/shm/zstack-vm-upload-remote-target'
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b'', b'')

        def host_realpath(path):
            if path == '/tmp/vddk.tar.gz':
                return '/etc/passwd'
            return path

        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                side_effect=lambda path, param_name: (path, None)), \
                mock.patch(
                    'kvmagent.plugins.vm_file_uploader.os.path.realpath',
                    side_effect=host_realpath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch('kvmagent.plugins.vm_file_uploader.tempfile.NamedTemporaryFile', return_value=password_file), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.chmod'), \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen', return_value=process) as popen, \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.rm_file_force'):
            uploader.upload()

        argv = popen.call_args[0][0]
        self.assertIn('root@192.168.100.10:/tmp/vddk.tar.gz', argv,
                      'remote target must not be rewritten by host-local symlinks: argv=%s' % argv)

    def test_path_validator_rejects_realpath_escape(self):
        path = '/var/lib/zstack/software-package/vm-upload/task/../../outside.tar.gz'
        root = '/var/lib/zstack/software-package/vm-upload'
        with mock.patch(
                'kvmagent.plugins.vm_file_uploader.linux.validate_install_path',
                return_value=(path, None)), \
                mock.patch(
                    'kvmagent.plugins.vm_file_uploader.os.path.realpath',
                    side_effect=['/var/lib/zstack/software-package/outside.tar.gz', root]), \
                self.assertRaises(VmFileUploadError) as error:
            VmFileUploader._validate_source_path(path, root, 'sourcePath')

        self.assertIn(root, str(error.exception),
                      'escaped source path error must identify the allowed root: error=%s' % error.exception)

    def test_scp_timeout_is_reported(self):
        uploader = VmFileUploader(self.make_cmd())
        password_file = mock.Mock()
        password_file.name = '/dev/shm/zstack-vm-upload-timeout'
        process = mock.Mock()
        process.returncode = 124
        process.communicate.return_value = (b'', b'')

        with mock.patch.object(
                uploader, '_validate_source_path',
                return_value=self.make_cmd().sourcePath), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.path.isfile', return_value=True), \
                mock.patch('kvmagent.plugins.vm_file_uploader.tempfile.NamedTemporaryFile', return_value=password_file), \
                mock.patch('kvmagent.plugins.vm_file_uploader.os.chmod'), \
                mock.patch('kvmagent.plugins.vm_file_uploader.subprocess.Popen', return_value=process), \
                mock.patch('kvmagent.plugins.vm_file_uploader.linux.rm_file_force'), \
                self.assertRaises(VmFileUploadError) as error:
            uploader.upload()

        self.assertEqual('SCP timed out after 600 seconds', str(error.exception),
                         'timeout error must include configured deadline: error=%s' % error.exception)

    def test_password_is_masked_from_http_debug_log(self):
        body = self.make_request()[http.REQUEST_BODY]

        masked_body = log.mask_sensitive_field(host_plugin.UploadFileToVmCmd(), body)

        self.assertNotIn('secret-password', masked_body,
                         'HTTP request logging must not expose the VM password')
        self.assertEqual('*****', jsonobject.loads(masked_body).password)

    def test_http_endpoint_registers_sensitive_command(self):
        self.assertEqual('/host/file/uploadtovm', self.plugin.UPLOAD_FILE_TO_VM_PATH)
        self.assertEqual('/host/file/uploadtovm/cleanup', self.plugin.CLEANUP_UPLOAD_FILE_TO_VM_PATH)
        http_server = mock.Mock()
        with mock.patch('kvmagent.plugins.host_plugin.kvmagent.get_http_server',
                        return_value=http_server), \
                mock.patch('kvmagent.plugins.host_plugin.os.path.exists', return_value=False):
            self.plugin.start()

        registrations = [call for call in http_server.register_async_uri.call_args_list
                         if call[0][0] == self.plugin.UPLOAD_FILE_TO_VM_PATH]
        self.assertEqual(1, len(registrations))
        self.assertIsInstance(registrations[0][1]['cmd'], host_plugin.UploadFileToVmCmd)

        cleanup_registrations = [call for call in http_server.register_async_uri.call_args_list
                                 if call[0][0] == self.plugin.CLEANUP_UPLOAD_FILE_TO_VM_PATH]
        self.assertEqual(1, len(cleanup_registrations))
        self.assertIsInstance(
            cleanup_registrations[0][1]['cmd'], host_plugin.CleanupUploadFileToVmCmd)


if __name__ == '__main__':
    unittest.main()
