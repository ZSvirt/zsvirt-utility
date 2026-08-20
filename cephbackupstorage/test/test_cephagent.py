# encoding: utf-8

import unittest

try:
    import mock
except ImportError:
    from unittest import mock

from cephbackupstorage import cephagent
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import plugin as task_plugin


def _cmd(**kwargs):
    cmd = type('Cmd', (), {})()
    for key, value in kwargs.items():
        setattr(cmd, key, value)
    return cmd


class ThreadContext(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __getitem__(self, name):
        return self.get(name)


class TestCephAgent(unittest.TestCase):
    def setUp(self):
        with task_plugin.task_operator_lock:
            task_plugin.task_daemons.clear()
            task_plugin.cancelled_task_tombstones.clear()

    def tearDown(self):
        with task_plugin.task_operator_lock:
            task_plugin.task_daemons.clear()
            task_plugin.cancelled_task_tombstones.clear()

    def test_init_creates_all_missing_pools_through_helper(self):
        agent = cephagent.CephAgent.__new__(cephagent.CephAgent)
        cmd = _cmd(pools=[
            _cmd(name='root', predefined=False),
            _cmd(name='data', predefined=False),
        ])
        req = {http.REQUEST_BODY: jsonobject.dumps(cmd)}

        with mock.patch(
                'cephbackupstorage.cephagent.shell.call',
                return_value=''), \
                mock.patch(
                    'cephbackupstorage.cephagent.ceph.is_xsky',
                    return_value=False), \
                mock.patch(
                    'cephbackupstorage.cephagent.ceph.is_sandstone',
                    return_value=False), \
                mock.patch(
                    'cephbackupstorage.cephagent.ceph.create_pools_with_compatible_pg_num') as create_pools, \
                mock.patch(
                    'cephbackupstorage.cephagent.ceph.get_fsid',
                    return_value='fsid'), \
                mock.patch.object(agent, '_set_capacity_to_response'):
            rsp = jsonobject.loads(agent.init(req))

        self.assertTrue(rsp.success)
        create_pools.assert_called_once_with(['root', 'data'])

    def test_upload_file_rejects_pending_cancellation(self):
        agent = cephagent.CephAgent.__new__(cephagent.CephAgent)
        agent.upload_file_tasks = mock.Mock()
        cmd = _cmd(
            installPath='/tmp/pending-cancel-upload',
            taskUuid='pending-cancel-api',
        )
        req = {
            http.REQUEST_BODY: jsonobject.dumps(cmd),
            http.REQUEST_HEADER: {'Host': '127.0.0.1:7761'},
        }

        with mock.patch(
                'cephbackupstorage.cephagent.validate_install_path',
                return_value=(cmd.installPath, None)), \
                mock.patch('cephbackupstorage.cephagent.FileSystemUploadTask'), \
                mock.patch.object(task_plugin.TaskDaemon, 'start', return_value=False):
            rsp = jsonobject.loads(agent.upload_file(req))

        self.assertFalse(rsp.success)
        self.assertEqual(
            'file[%s] upload canceled before start' % cmd.installPath,
            rsp.error)
        self.assertFalse(rsp.directUploadUrl)
        agent.upload_file_tasks.add_task.assert_called_once()

    def test_image_download_rejects_pending_cancellation_before_side_effect(self):
        api_id = 'pending-ceph-image-download-api'
        task_plugin.TaskManager.remember_cancellation(api_id)
        agent = cephagent.CephAgent.__new__(cephagent.CephAgent)
        cmd = _cmd(
            threadContext=ThreadContext(api=api_id),
            taskContext=None,
            url='http://example.com/image.qcow2',
            installPath='ceph://pool/image',
        )
        req = {http.REQUEST_BODY: jsonobject.dumps(cmd)}

        with mock.patch('cephbackupstorage.cephagent.traceable_shell.get_shell') as get_shell:
            rsp = jsonobject.loads(agent.download(req))

        self.assertFalse(rsp.success)
        self.assertEqual('image download canceled before start', rsp.error)
        get_shell.assert_not_called()

    def test_image_upload_rejects_task_daemon_start_failure(self):
        agent = cephagent.CephAgent.__new__(cephagent.CephAgent)
        cmd = _cmd(
            threadContext=ThreadContext(api='ceph-image-upload-api'),
            taskContext=None,
            url='upload://image',
            installPath='ceph://pool/image',
        )
        req = {http.REQUEST_BODY: jsonobject.dumps(cmd)}

        with mock.patch.object(agent, '_prepare_upload', return_value=False), \
                mock.patch('cephbackupstorage.cephagent.traceable_shell.get_shell'):
            rsp = jsonobject.loads(agent.download(req))

        self.assertFalse(rsp.success)
        self.assertEqual('image upload canceled before start', rsp.error)

    def test_image_upload_cancel_after_task_insert_allows_retry(self):
        image_uuid = 'a' * 32
        first_api_id = 'ceph-image-upload-first-api'
        agent = cephagent.CephAgent.__new__(cephagent.CephAgent)
        agent.upload_tasks = cephagent.UploadTasks()

        first_cmd = _cmd(
            imageUuid=image_uuid,
            threadContext=ThreadContext(api=first_api_id),
            taskContext=None,
            url='upload://%s' % image_uuid,
            installPath='ceph://pool/image',
        )
        second_cmd = _cmd(
            imageUuid=image_uuid,
            threadContext=ThreadContext(api='ceph-image-upload-second-api'),
            taskContext=None,
            url='upload://%s' % image_uuid,
            installPath='ceph://pool/image',
        )
        real_add_task = agent.upload_tasks.add_task

        def add_task_then_cancel(task):
            real_add_task(task)
            task_plugin.TaskManager.remember_cancellation(first_api_id)

        with mock.patch.object(agent, 'get_ioctx', return_value=mock.Mock()):
            with mock.patch('cephbackupstorage.cephagent.shell.run') as shell_run:
                with mock.patch.object(agent.upload_tasks, 'add_task', side_effect=add_task_then_cancel):
                    self.assertFalse(agent._prepare_upload(first_cmd))

                canceled_task = agent.upload_tasks.get_task(image_uuid)
                self.assertTrue(canceled_task.completed)
                self.assertEqual('image [uuid: %s] upload canceled' % image_uuid, canceled_task.lastError)
                shell_run.assert_called_once_with('rbd rm pool/tmp-image')

                self.assertTrue(agent._prepare_upload(second_cmd))

        retry_task = agent.upload_tasks.get_task(image_uuid)
        self.assertFalse(retry_task.completed)
        self.assertIsNone(retry_task.lastError)


if __name__ == '__main__':
    unittest.main()
