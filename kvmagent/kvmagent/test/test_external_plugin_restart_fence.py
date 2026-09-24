# -*- coding: utf-8 -*-
from __future__ import absolute_import

import io
import json
import sys
import threading
import time
import types
import unittest

try:
    from unittest import mock
except ImportError:
    import mock

from kvmagent import external_plugin_restart_fence
from zstacklib.utils import restart_fence
from zstacklib.utils.restart_fence import AgentRestartFence


class _Namespace(object):
    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class _HttpError(Exception):
    def __init__(self, status, message):
        super(_HttpError, self).__init__(message)
        self.status = status


def _request(remote_ip="127.0.0.1", method="POST", body=None):
    body = body or "{}"
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return _Namespace(
        remote=_Namespace(ip=remote_ip),
        method=method,
        body=_Namespace(fp=io.StringIO(body)))


class AgentRestartFenceTest(unittest.TestCase):
    def setUp(self):
        AgentRestartFence.reset_for_test()

    def tearDown(self):
        AgentRestartFence.reset_for_test()

    def _call_restart_handler(self, request):
        cherrypy = types.ModuleType("cherrypy")
        cherrypy.HTTPError = _HttpError
        http = types.ModuleType("zstacklib.utils.http")
        http.AsyncUirHandler = type(
            "AsyncUirHandler", (), {"STOP_WORLD": False})
        replacements = {
            "cherrypy": cherrypy,
            "zstacklib.utils.http": http,
        }
        previous = dict((name, sys.modules.get(name)) for name in replacements)
        try:
            sys.modules.update(replacements)
            return external_plugin_restart_fence.restart_fence_handler(request)
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_request_helper_accepts_utf8_bytes_for_text_stream(self):
        request = _request(body=b'{"operationId":"restart-1"}')

        self.assertEqual(
            '{"operationId":"restart-1"}', request.body.fp.read())

    def test_restart_endpoint_rejects_non_loopback_request(self):
        with self.assertRaises(_HttpError) as raised:
            self._call_restart_handler(_request(remote_ip="192.0.2.10"))

        self.assertEqual(403, raised.exception.status)

    def test_restart_endpoint_rejects_non_post_request(self):
        with self.assertRaises(_HttpError) as raised:
            self._call_restart_handler(_request(method="GET"))

        self.assertEqual(405, raised.exception.status)

    def test_restart_endpoint_returns_fenced_snapshot(self):
        response = json.loads(self._call_restart_handler(_request(body=json.dumps({
            "operationId": "restart-1",
            "drainTimeoutSeconds": 1,
            "leaseSeconds": 2,
        }))))

        self.assertTrue(response["success"])
        self.assertEqual("restart-1", response["operationId"])
        self.assertEqual("FENCED", response["state"])
        self.assertFalse(response["acceptingNewRequests"])

    def test_restart_endpoint_rejects_non_object_json(self):
        response = json.loads(self._call_restart_handler(
            _request(body='["restart-1", 1, 2]')))

        self.assertFalse(response["success"])
        self.assertEqual("RESTART_FENCE_REQUEST_INVALID",
                         response["errorCode"])

    def test_acquire_drains_existing_request_and_rejects_new_request(self):
        self.assertTrue(AgentRestartFence.enter_request())
        result = []

        def acquire():
            result.append(AgentRestartFence.acquire(1, 2))

        worker = threading.Thread(target=acquire)
        worker.start()
        deadline = time.time() + 1
        while AgentRestartFence.snapshot()["state"] != "FENCED":
            self.assertLess(time.time(), deadline)
            time.sleep(0.01)
        self.assertFalse(AgentRestartFence.enter_request())
        AgentRestartFence.leave_request()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0][0])
        self.assertEqual("FENCED", result[0][1]["state"])

    def test_timeout_reopens_async_request_admission(self):
        self.assertTrue(AgentRestartFence.enter_request())
        acquired, snapshot = AgentRestartFence.acquire(0.02, 1)

        self.assertFalse(acquired)
        self.assertEqual("BUSY", snapshot["state"])
        self.assertTrue(snapshot["acceptingNewRequests"])
        AgentRestartFence.leave_request()

    def test_wall_clock_jump_does_not_expire_drain_deadline(self):
        self.assertTrue(AgentRestartFence.enter_request())
        release_request = threading.Timer(
            0.02, AgentRestartFence.leave_request)
        release_request.daemon = True
        release_request.start()
        wall_clock_calls = []

        def jumped_wall_clock():
            wall_clock_calls.append(True)
            return 1000.0 if len(wall_clock_calls) == 1 else 1100.0

        with mock.patch.object(
                restart_fence.time, "time", side_effect=jumped_wall_clock):
            acquired, snapshot = AgentRestartFence.acquire(30, 60)

        self.assertTrue(acquired)
        self.assertEqual("FENCED", snapshot["state"])

    def test_monotonic_clock_falls_back_to_os_elapsed_time(self):
        with mock.patch.object(
                restart_fence.time, "monotonic", None, create=True), \
             mock.patch.object(
                 restart_fence.os, "times",
                 return_value=(0, 0, 0, 0, 123.5)):
            current = restart_fence.monotonic_time()

        self.assertEqual(123.5, current)

    def test_lease_releases_fence_when_restart_does_not_happen(self):
        acquired, unused_snapshot = AgentRestartFence.acquire(0.02, 0.05)
        self.assertTrue(acquired)
        deadline = time.time() + 1
        while AgentRestartFence.snapshot()["state"] == "FENCED":
            self.assertLess(time.time(), deadline)
            time.sleep(0.01)

        self.assertTrue(AgentRestartFence.enter_request())
        AgentRestartFence.leave_request()


if __name__ == "__main__":
    unittest.main()
