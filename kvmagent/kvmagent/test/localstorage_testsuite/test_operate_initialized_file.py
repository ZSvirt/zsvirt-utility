from kvmagent.test.utils import localstorage_utils,pytest_utils
from kvmagent.test.utils.stub import *
from zstacklib.test.utils import remote,misc
from zstacklib.utils import linux, jsonobject, bash
from kvmagent import kvmagent
from unittest import TestCase
import os
import shutil
import tempfile
localstorage_utils.init_localstorage_plugin()

PKG_NAME = __name__

__ENV_SETUP__ = {
    'self': {}
}


## describe: case will manage by ztest
class TestLocalStoragePlugin(TestCase):

    @classmethod
    def setUpClass(cls):
        return
    @pytest_utils.ztest_decorater
    def test_create_initialized_file(self):
        rsp = localstorage_utils.localstorage_init(
            "/local_ps",
            "/local_ps/local-ps-uuid-initialized-file"
        )
        self.assertGreater(rsp.totalCapacity, 0, rsp.error)
        self.assertGreater(rsp.availableCapacity, 0, rsp.error)
        self.assertTrue(os.path.exists("/local_ps/local-ps-uuid-initialized-file"))

        rsp = localstorage_utils.create_initialized_file(
            filePath = "/local_ps/test",
            storagePath = "/local_ps"
        )

        self.assertEqual(True, os.path.exists("/local_ps/test"), "[check] cannot create  initialized file in host")

        rsp = localstorage_utils.check_initialized_file(
            filePath="/local_ps/test",
            storagePath="/local_ps"
        )

        self.assertGreater(rsp.totalCapacity, 0, rsp.error)
        self.assertGreater(rsp.availableCapacity, 0, rsp.error)
        bash.bash_ro("rm -rf /local_ps")

    @pytest_utils.ztest_decorater
    def test_initialized_file_guard(self):
        storage_path = tempfile.mkdtemp()
        try:
            cmd = jsonobject.loads(jsonobject.dumps({
                'uuid': 'local-ps-uuid',
                'storagePath': storage_path,
            }))
            with self.assertRaises(kvmagent.KvmError) as context:
                localstorage_utils.LOCALSTORAGE_PLUGIN._check_initialized_file('/localstorage/future', cmd)
            for expected in ['local-ps-uuid', 'lsblk -f', 'findmnt', '/etc/fstab']:
                self.assertIn(expected, str(context.exception))

            initialized_file = os.path.join(storage_path, 'local-ps-uuid-initialized-file')
            open(initialized_file, 'w').close()
            localstorage_utils.LOCALSTORAGE_PLUGIN._check_initialized_file('/localstorage/future', cmd)

            missing_fields = jsonobject.loads('{}')
            with self.assertRaises(kvmagent.KvmError):
                localstorage_utils.LOCALSTORAGE_PLUGIN._check_initialized_file('/localstorage/future', missing_fields)
        finally:
            shutil.rmtree(storage_path)

    @pytest_utils.ztest_decorater
    def test_initialized_file_guard_wraps_future_routes(self):
        class FakeHttpServer(object):
            def __init__(self):
                self.handlers = {}

            def register_async_uri(self, path, handler, *args, **kwargs):
                self.handlers[path] = handler

        plugin = localstorage_utils.LOCALSTORAGE_PLUGIN
        fake_server = FakeHttpServer()
        guarded_server = plugin._local_storage_guarded_http_server(fake_server)
        called = []

        def handler(req):
            called.append(True)
            return '{"success": true}'

        guarded_server.register_async_uri('/localstorage/future', handler)
        guarded_server.register_async_uri(plugin.INIT_PATH, handler)
        storage_path = tempfile.mkdtemp()
        try:
            request = misc.make_a_request({
                'primaryStorageUuid': 'local-ps-uuid',
                'storagePath': storage_path,
            })
            response = jsonobject.loads(fake_server.handlers['/localstorage/future'](request))
            self.assertFalse(response.success)
            self.assertEqual([], called)

            open(os.path.join(storage_path, 'local-ps-uuid-initialized-file'), 'w').close()
            response = jsonobject.loads(fake_server.handlers['/localstorage/future'](request))
            self.assertTrue(response.success)
            self.assertEqual([True], called)

            fake_server.handlers[plugin.INIT_PATH](misc.make_a_request({}))
            self.assertEqual([True, True], called)
        finally:
            shutil.rmtree(storage_path)
