# encoding: utf-8

'''

@author: frank
'''
import subprocess
import time
import unittest

try:
    import mock
except ImportError:
    from unittest import mock

from kvmagent import kvmagent
from kvmagent.plugins import host_plugin
from zstacklib.utils import http
from zstacklib.utils import uuidhelper
from zstacklib.utils import jsonobject
from zstacklib.utils import log
from zstacklib.utils import plugin as task_plugin


logger = log.get_logger(__name__)

class ConnectCmd(kvmagent.AgentCommand):
    def __init__(self):
        self.hostUuid = uuidhelper.uuid()
        
class HostFactCmd(kvmagent.AgentCommand): pass


        
class TestHostPlugin(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        self.service = kvmagent.new_rest_service()
        self.service.start()
        time.sleep(1)

    @classmethod
    def tearDownClass(self):
        self.service.stop()


    def test_connect(self):
        url = kvmagent._build_url_for_test([host_plugin.HostPlugin.CONNECT_PATH])
        logger.debug('calling %s' % url)
        ret = http.json_dump_post(url, body=ConnectCmd())
        rsp = jsonobject.loads(ret)
        self.assertTrue(rsp.success)
        
    @mock.patch('subprocess.Popen')
    def test_hostfact(self, mock_popen):
        url = kvmagent._build_url_for_test([host_plugin.HostPlugin.FACT_PATH])
        cmd = HostFactCmd()
        ret = http.json_dump_post(url, body=cmd)
        rsp = jsonobject.loads(ret)
        self.assertTrue(rsp.success)
        self.assertEqual(host_plugin._get_cpu_num(), rsp.cpuNum)
        self.assertEqual(host_plugin._get_cpu_speed(), rsp.cpuSpeed)
        self.assertEqual(host_plugin._get_total_memory(), rsp.totalMemory)

    def test_direct_upload_file_propagates_upload_error(self):
        plugin = host_plugin.HostPlugin()

        with mock.patch('kvmagent.plugins.host_plugin.UploadHandler') as upload_handler:
            upload_handler.return_value.handle_upload.side_effect = Exception('incomplete slice')

            with self.assertRaises(Exception):
                plugin.direct_upload_file(object())

    def test_upload_file_rejects_pending_cancellation(self):
        host = host_plugin.HostPlugin()
        host.upload_tasks = mock.Mock()
        cmd = type('Cmd', (), {
            'installPath': '/tmp/pending-cancel-upload',
            'taskUuid': 'pending-cancel-api',
        })()
        req = {
            http.REQUEST_BODY: jsonobject.dumps(cmd),
            http.REQUEST_HEADER: {'Host': '127.0.0.1:7070'},
        }

        with mock.patch(
                'kvmagent.plugins.host_plugin.linux.validate_install_path',
                return_value=(cmd.installPath, None)), \
                mock.patch('kvmagent.plugins.host_plugin.FileSystemUploadTask'), \
                mock.patch.object(task_plugin.TaskDaemon, 'start', return_value=False):
            rsp = jsonobject.loads(host.upload_file(req))

        self.assertFalse(rsp.success)
        self.assertEqual(
            'file[%s] upload canceled before start' % cmd.installPath,
            rsp.error)
        self.assertFalse(rsp.directUploadUrl)
        host.upload_tasks.add_task.assert_called_once()

    if __name__ == "__main__":
        #import sys;sys.argv = ['', 'Test.testName']
        unittest.main()
