import unittest

try:
    import mock
except ImportError:
    from unittest import mock

from zstacklib.utils import file_downloader
from zstacklib.utils import linux
from zstacklib.utils import plugin


class ThreadContext(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __getitem__(self, name):
        return self.get(name)


class ProgressReporter(object):
    def progress_report(self, *args):
        pass


class FakeProcess(object):
    pid = 10001

    def __init__(self, poll_results=None, returncode=None):
        self.killed = False
        self.poll_results = list(poll_results or [None])
        self.returncode = returncode

    def poll(self):
        if len(self.poll_results) > 1:
            return self.poll_results.pop(0)
        return self.poll_results[0]

    def kill(self):
        self.killed = True


def _download_cmd(api_id):
    return type('Cmd', (), {
        'installPath': '/tmp/%s' % api_id,
        'timeout': 30,
        'url': 'http://example.com/image.qcow2',
        'urlScheme': 'http',
        'threadContext': ThreadContext(api=api_id),
        'taskContext': None,
    })()


def _cancel_cmd(api_id, allow_task_not_found=True):
    return type('CancelCmd', (), {
        'cancellationApiId': api_id,
        'allowTaskNotFound': allow_task_not_found,
        'times': 1,
        'interval': 1,
    })()


class TestFileDownloader(unittest.TestCase):
    def setUp(self):
        with plugin.task_operator_lock:
            plugin.task_daemons.clear()
            plugin.cancelled_task_tombstones.clear()

    def tearDown(self):
        with plugin.task_operator_lock:
            plugin.task_daemons.clear()
            plugin.cancelled_task_tombstones.clear()

    def test_download_rejects_every_retry_after_missing_task_cancel(self):
        api_id = 'pending-file-download-api'
        plugin.TaskManager.remember_cancellation(api_id)

        cmd = _download_cmd(api_id)
        downloader = file_downloader.FileDownloader(None, cmd)

        with mock.patch.object(downloader, 'check_capacity') as check_capacity:
            for _ in range(2):
                success, error = downloader.download()
                self.assertFalse(success)
                self.assertEqual('download canceled before start', error)

        check_capacity.assert_not_called()

    def test_download_rejects_cancel_that_arrives_during_preflight(self):
        api_id = 'preflight-file-download-api'
        cmd = _download_cmd(api_id)
        downloader = file_downloader.FileDownloader(ProgressReporter(), cmd)

        def cancel_during_preflight():
            rsp = plugin.CancelJobResponse()
            with mock.patch('zstacklib.utils.plugin.time.sleep'):
                with mock.patch('zstacklib.utils.plugin.traceable_shell.cancel_job', return_value=False):
                    plugin.cancel_job(_cancel_cmd(api_id), rsp)
            self.assertTrue(rsp.success)
            self.assertEqual('TASK_NOT_FOUND', rsp.cancelResult)
            return True, None

        with mock.patch.object(downloader, 'check_capacity', side_effect=cancel_during_preflight):
            with mock.patch.object(downloader, 'use_wget') as use_wget:
                success, error = downloader.download()

        self.assertFalse(success)
        self.assertEqual('download canceled before start', error)
        use_wget.assert_not_called()

    def test_http_download_cancel_while_running_hits_active_task(self):
        api_id = 'running-file-download-api'
        cmd = _download_cmd(api_id)
        downloader = file_downloader.FileDownloader(ProgressReporter(), cmd)
        cancel_results = []
        checker_results = []

        def cancel_while_running(*args, **kwargs):
            rsp = plugin.CancelJobResponse()
            with mock.patch('zstacklib.utils.plugin.traceable_shell.cancel_job', return_value=False):
                plugin.cancel_job(_cancel_cmd(api_id, allow_task_not_found=False), rsp)
            cancel_results.append(rsp.cancelResult)
            checker_results.append(kwargs['cancellation_checker']())
            return 1

        with mock.patch.object(downloader, 'check_capacity', return_value=(True, None)):
            with mock.patch('zstacklib.utils.file_downloader.linux.wget', side_effect=cancel_while_running):
                success, error = downloader.download()

        self.assertFalse(success)
        self.assertEqual(['CANCEL_SIGNALLED'], cancel_results)
        self.assertEqual([True], checker_results)

    def test_use_wget_wraps_command_with_traceable_shell(self):
        api_id = 'traceable-file-download-api'
        cmd = _download_cmd(api_id)
        downloader = file_downloader.FileDownloader(ProgressReporter(), cmd)

        with mock.patch('zstacklib.utils.file_downloader.linux.wget', return_value=0) as wget:
            downloader.use_wget(cmd.url, 'traceable-file-download-api', '/tmp', 30)

        cmd_wrapper = wget.call_args[1]['cmd_wrapper']
        wrapped_cmd = cmd_wrapper('wget http://example.com/image.qcow2')
        self.assertIn(api_id, wrapped_cmd)
        self.assertTrue(callable(wget.call_args[1]['cancellation_checker']))

    def test_linux_wget_rechecks_cancellation_after_file_size_probe(self):
        shell_calls = []

        def shell_call(cmd, workdir=None):
            shell_calls.append(cmd)
            return 'HTTP/1.1 200 OK\nContent-Length: 1\n'

        with mock.patch('zstacklib.utils.linux.os.path.exists', return_value=False):
            with mock.patch('zstacklib.utils.linux.shell.call', side_effect=shell_call):
                with mock.patch('zstacklib.utils.linux.shell.get_process') as get_process:
                    with self.assertRaises(linux.LinuxError):
                        linux.wget(
                            'http://example.com/file-downloader-cancel-test-do-not-exist',
                            '/tmp',
                            cancellation_checker=lambda: len(shell_calls) > 0
                        )

        get_process.assert_not_called()

    def test_linux_wget_kills_running_process_when_canceled(self):
        process = FakeProcess()
        cancellation_checks = []

        def cancellation_checker():
            cancellation_checks.append(True)
            return len(cancellation_checks) >= 3

        with mock.patch('zstacklib.utils.linux.os.path.exists', return_value=False):
            with mock.patch('zstacklib.utils.linux.shell.call', return_value='HTTP/1.1 200 OK\nContent-Length: 1\n'):
                with mock.patch('zstacklib.utils.linux.shell.get_process', return_value=process):
                    with mock.patch('zstacklib.utils.linux.kill_all_child_process') as kill_children:
                        with self.assertRaises(linux.LinuxError):
                            linux.wget(
                                'http://example.com/file-downloader-running-cancel-test-do-not-exist',
                                '/tmp',
                                cancellation_checker=cancellation_checker
                            )

        kill_children.assert_called_once_with(process.pid)
        self.assertTrue(process.killed)

    def test_linux_wget_polls_unsized_download_for_cancellation(self):
        process = FakeProcess()
        cancellation_checks = []

        def cancellation_checker():
            cancellation_checks.append(True)
            return len(cancellation_checks) >= 4

        with mock.patch('zstacklib.utils.linux.os.path.exists', return_value=False):
            with mock.patch('zstacklib.utils.linux.shell.call', return_value='HTTP/1.1 200 OK\n'):
                with mock.patch('zstacklib.utils.linux.shell.get_process', return_value=process) as get_process:
                    with mock.patch('zstacklib.utils.linux.kill_all_child_process') as kill_children:
                        with self.assertRaises(linux.LinuxError):
                            linux.wget(
                                'http://example.com/file-downloader-unsized-cancel-test-do-not-exist',
                                '/tmp',
                                cancellation_checker=cancellation_checker
                            )

        get_process.assert_called_once()
        kill_children.assert_called_once_with(process.pid)
        self.assertTrue(process.killed)

    def test_linux_wget_unsized_download_returns_success_code(self):
        process = FakeProcess(poll_results=[None, 0], returncode=0)

        with mock.patch('zstacklib.utils.linux.os.path.exists', return_value=False):
            with mock.patch('zstacklib.utils.linux.shell.call', return_value='HTTP/1.1 200 OK\n'):
                with mock.patch('zstacklib.utils.linux.shell.get_process', return_value=process) as get_process:
                    with mock.patch('zstacklib.utils.linux.kill_all_child_process') as kill_children:
                        with mock.patch('zstacklib.utils.linux.time.sleep'):
                            ret = linux.wget(
                                'http://example.com/file-downloader-unsized-success-test-do-not-exist',
                                '/tmp',
                            )

        self.assertEqual(0, ret)
        get_process.assert_called_once()
        self.assertEqual(0, kill_children.call_count)
        self.assertFalse(process.killed)

    def test_linux_wget_unsized_download_returns_process_failure_code(self):
        process = FakeProcess(poll_results=[7], returncode=7)

        with mock.patch('zstacklib.utils.linux.os.path.exists', return_value=False):
            with mock.patch('zstacklib.utils.linux.shell.call', return_value='HTTP/1.1 200 OK\n'):
                with mock.patch('zstacklib.utils.linux.shell.get_process', return_value=process) as get_process:
                    with mock.patch('zstacklib.utils.linux.kill_all_child_process') as kill_children:
                        ret = linux.wget(
                            'http://example.com/file-downloader-unsized-failure-test-do-not-exist',
                            '/tmp',
                        )

        self.assertEqual(7, ret)
        get_process.assert_called_once()
        self.assertEqual(0, kill_children.call_count)
        self.assertFalse(process.killed)


if __name__ == '__main__':
    unittest.main()
