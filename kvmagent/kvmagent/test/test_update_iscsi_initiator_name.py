import mock
import tempfile
import time
from kvmagent import kvmagent
from kvmagent.plugins import host_plugin
from kvmagent.test.utils import pytest_utils
from unittest import TestCase
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import bash

__ENV_SETUP__ = {
    'self': {}
}


class TestHost(TestCase, pytest_utils.PytestExtension):
    @classmethod
    def setUpClass(self):
        self.service = kvmagent.new_rest_service()
        self.service.start()
        time.sleep(1)

    @pytest_utils.ztest_decorater
    def test_update_iscsi_initiator_name(self):
        temp_file = tempfile.NamedTemporaryFile(prefix='iqn', suffix='', dir='/tmp', mode='w+b', delete=True)
        host_plugin.ISCSI_INITIATOR_NAME_PATH = temp_file.name
        old_initiator_name = 'InitiatorName=iqn.2015-01.io.zstack:test'
        new_initiator_name = 'InitiatorName=iqn.2015-01.io.zstack:test1'
        with open(host_plugin.ISCSI_INITIATOR_NAME_PATH, 'w') as f:
            f.write(old_initiator_name)

        url = kvmagent._build_url_for_test([host_plugin.UPDATE_ISCSI_INITIATOR_NAME_PATH])
        cmd = host_plugin.UpdateHostIscsiInitiatorNameCmd()
        cmd.iscsiInitiatorName = new_initiator_name
        bash.bash_roe = mock.Mock(return_value=(-1,"","failed to restart iscsid"))
        ret = http.json_dump_post(url, body=cmd)
        rsp = jsonobject.loads(ret)
        self.assertFalse(rsp.success)
        self.assertTrue(rsp.error.find('failed to restart iscsid') >= 0)

        with open(host_plugin.ISCSI_INITIATOR_NAME_PATH, 'r') as f:
            content = f.read()
        self.assertEqual(content, old_initiator_name)

        bash.bash_roe = mock.Mock(return_value=(0,"",""))
        cmd.iscsiInitiatorName = new_initiator_name
        ret = http.json_dump_post(url, body=cmd)
        rsp = jsonobject.loads(ret)
        self.assertTrue(rsp.success)
        
        with open(host_plugin.ISCSI_INITIATOR_NAME_PATH, 'r') as f:
            content = f.read()
        self.assertEqual(content, new_initiator_name)