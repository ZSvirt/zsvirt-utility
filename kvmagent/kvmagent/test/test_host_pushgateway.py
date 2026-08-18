import os
import shutil
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

from kvmagent.plugins import host_pushgateway


class TestHostPushgateway(unittest.TestCase):
    def test_get_auth_header_reads_lighttpd_config(self):
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, b'setenv.add-request-header = ( "Authorization" => "Basic custom" )')
            os.close(fd)

            self.assertEqual("Basic custom", host_pushgateway.get_auth_header([path]))
        finally:
            if fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
            os.remove(path)

    def test_get_auth_header_falls_back_to_product_default(self):
        self.assertEqual(
            host_pushgateway.DEFAULT_HOST_PUSHGATEWAY_AUTH_HEADER,
            host_pushgateway.get_auth_header([])
        )

    def test_get_auth_header_uses_deterministic_glob_priority(self):
        temp_dir = tempfile.mkdtemp()
        try:
            low_priority_dir = os.path.join(temp_dir, "low")
            high_priority_dir = os.path.join(temp_dir, "high")
            os.makedirs(low_priority_dir)
            os.makedirs(high_priority_dir)
            low_priority_conf = os.path.join(low_priority_dir, "lighttpd.conf")
            high_priority_conf = os.path.join(high_priority_dir, "lighttpd.conf")
            with open(low_priority_conf, "w") as fd:
                fd.write('setenv.add-request-header = ( "Authorization" => "Basic low" )')
            with open(high_priority_conf, "w") as fd:
                fd.write('setenv.add-request-header = ( "Authorization" => "Basic high" )')

            with mock.patch.object(host_pushgateway, "LIGHTTPD_CONF_GLOBS", [
                os.path.join(high_priority_dir, "lighttpd.conf"),
                os.path.join(low_priority_dir, "lighttpd.conf"),
            ]):
                self.assertEqual("Basic high", host_pushgateway.get_auth_header())
        finally:
            shutil.rmtree(temp_dir)

    def test_make_push_metrics_headers_adds_basic_auth(self):
        with mock.patch.object(host_pushgateway, "get_auth_header", return_value="Basic test"):
            headers = host_pushgateway.make_push_metrics_headers()

        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("Basic test", headers["Authorization"])

    def test_make_get_metrics_headers_adds_basic_auth(self):
        with mock.patch.object(host_pushgateway, "get_auth_header", return_value="Basic test"):
            headers = host_pushgateway.make_get_metrics_headers()

        self.assertEqual("text/plain", headers["Content-Type"])
        self.assertEqual("Basic test", headers["Authorization"])

    def test_make_delete_metric_headers_adds_basic_auth(self):
        with mock.patch.object(host_pushgateway, "get_auth_header", return_value="Basic test"):
            headers = host_pushgateway.make_delete_metric_headers()

        self.assertEqual("Basic test", headers["Authorization"])


if __name__ == "__main__":
    unittest.main()
