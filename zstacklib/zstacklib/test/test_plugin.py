'''

@author: frank
'''
import unittest
import os.path
import time
from zstacklib.utils import plugin

class TestPlugin(unittest.TestCase):
    def setUp(self):
        with plugin.task_operator_lock:
            plugin.task_daemons.clear()
            plugin.cancelled_task_tombstones.clear()

    def tearDown(self):
        with plugin.task_operator_lock:
            plugin.task_daemons.clear()
            plugin.cancelled_task_tombstones.clear()

    def test_plugin_start(self):
        plugin_rgty = plugin.PluginRegistry(os.path.abspath('zstacklib/test/plugin/plugins.cfg'))
        config = {'key':'value'}
        plugin_rgty.configure_plugins(config)
        plugin_rgty.start_plugins()
        plugin1 = plugin_rgty.get_plugin('Plugin1')
        self.assertTrue(plugin1.start_called)
        self.assertEqual(config['key'], plugin1.config['key'])
        
    def test_plugin_stop(self):
        plugin_rgty = plugin.PluginRegistry(os.path.abspath('zstacklib/test/plugin/plugins.cfg'))
        plugin_rgty.start_plugins()
        plugin_rgty.stop_plugins()
        plugin1 = plugin_rgty.get_plugin('Plugin1')
        self.assertTrue(plugin1.stop_called)

    def test_cancel_job_keeps_missing_task_failure_by_default(self):
        cmd = type('Cmd', (), {})()
        cmd.cancellationApiId = 'missing-api'
        rsp = plugin.CancelJobResponse()

        rsp = plugin._cancel_job(cmd, rsp, times=0, interval=0)

        self.assertFalse(rsp.success)
        self.assertEqual('no matched job to cancel', rsp.error)
        self.assertEqual('CANCEL_SIGNALLED', rsp.cancelResult)

    def test_cancel_job_can_report_missing_task(self):
        cmd = type('Cmd', (), {})()
        cmd.cancellationApiId = 'missing-api'
        cmd.allowTaskNotFound = True
        rsp = plugin.CancelJobResponse()

        rsp = plugin._cancel_job(cmd, rsp, times=0, interval=0)

        self.assertTrue(rsp.success)
        self.assertEqual('TASK_NOT_FOUND', rsp.cancelResult)

    def test_cancel_before_registration_cancels_late_task(self):
        class FakeTask(object):
            def __init__(self):
                self.canceled = False

            def cancel(self):
                self.canceled = True

        cmd = type('Cmd', (), {})()
        cmd.cancellationApiId = 'late-api'
        cmd.allowTaskNotFound = True
        rsp = plugin._cancel_job(cmd, plugin.CancelJobResponse(), times=0, interval=0)
        self.assertEqual('TASK_NOT_FOUND', rsp.cancelResult)

        task = FakeTask()
        self.assertFalse(plugin.TaskManager.add_task(cmd.cancellationApiId, task))
        self.assertTrue(task.canceled)
        self.assertNotIn(cmd.cancellationApiId, plugin.task_daemons)
        self.assertTrue(plugin.TaskManager.cancel_tombstone_matched(cmd.cancellationApiId))

        retry_task = FakeTask()
        self.assertFalse(plugin.TaskManager.add_task(cmd.cancellationApiId, retry_task))
        self.assertTrue(retry_task.canceled)

    def test_task_daemon_start_rejects_pending_cancellation(self):
        cmd = type('Cmd', (), {})()
        cmd.cancellationApiId = 'late-daemon-api'
        cmd.allowTaskNotFound = True
        rsp = plugin._cancel_job(cmd, plugin.CancelJobResponse(), times=0, interval=0)
        self.assertEqual('TASK_NOT_FOUND', rsp.cancelResult)

        daemon = FakeTaskDaemon(_task_spec(cmd.cancellationApiId))

        self.assertFalse(daemon.start())
        self.assertTrue(daemon.canceled)
        self.assertTrue(daemon.closed)
        self.assertNotIn(cmd.cancellationApiId, plugin.task_daemons)
        self.assertTrue(plugin.TaskManager.cancel_tombstone_matched(cmd.cancellationApiId))

    def test_task_daemon_context_rejects_pending_cancellation(self):
        cmd = type('Cmd', (), {})()
        cmd.cancellationApiId = 'late-context-api'
        cmd.allowTaskNotFound = True
        rsp = plugin._cancel_job(cmd, plugin.CancelJobResponse(), times=0, interval=0)
        self.assertEqual('TASK_NOT_FOUND', rsp.cancelResult)

        daemon = FakeTaskDaemon(_task_spec(cmd.cancellationApiId))
        body_entered = [False]

        with self.assertRaises(plugin.TaskCanceledByPendingCancellation):
            with daemon:
                body_entered[0] = True

        self.assertFalse(body_entered[0])
        self.assertTrue(daemon.canceled)
        self.assertTrue(daemon.closed)
        self.assertNotIn(cmd.cancellationApiId, plugin.task_daemons)
        self.assertTrue(plugin.TaskManager.cancel_tombstone_matched(cmd.cancellationApiId))

    def test_expired_cancel_tombstones_are_pruned(self):
        plugin.cancelled_task_tombstones['expired-api'] = {
            'expiresAt': time.time() - 1,
            'matched': False,
        }

        plugin.TaskManager.remember_cancellation('current-api')

        self.assertNotIn('expired-api', plugin.cancelled_task_tombstones)
        self.assertIn('current-api', plugin.cancelled_task_tombstones)

    def test_live_task_cancel_keeps_tombstone_matched(self):
        class FakeTask(object):
            def __init__(self):
                self.canceled = False

            def cancel(self):
                self.canceled = True

        task = FakeTask()
        self.assertTrue(plugin.TaskManager.add_task('live-api', task))

        plugin.TaskManager.remember_cancellation('live-api')
        self.assertEqual(1, plugin.TaskManager.cancel_task('live-api'))
        self.assertTrue(task.canceled)
        self.assertTrue(plugin.TaskManager.cancel_tombstone_matched('live-api'))

        plugin.TaskManager.remember_cancellation('live-api')
        self.assertTrue(plugin.TaskManager.cancel_tombstone_matched('live-api'))


class ThreadContext(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __getitem__(self, name):
        return self.get(name)


def _task_spec(api_id):
    spec = type('Spec', (), {})()
    spec.threadContext = ThreadContext(api=api_id)
    spec.taskContext = None
    return spec


class FakeTaskDaemon(plugin.TaskDaemon):
    def __init__(self, spec):
        super(FakeTaskDaemon, self).__init__(spec, 'fakeTask')
        self.canceled = False

    def _cancel(self):
        self.canceled = True


if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()
