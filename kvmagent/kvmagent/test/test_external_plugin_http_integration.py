# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import runpy
import sys
import threading
import types
import unittest

from zstacklib.utils.restart_fence import AgentRestartFence


class _Namespace(object):
    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class _AsyncThread(object):
    def __init__(self, function):
        self.func = function

    def __get__(self, instance, owner=None):
        return self.__class__(self.func.__get__(instance, owner))

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


class _Logger(object):
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warn(self, *args, **kwargs):
        pass


class _HttpError(Exception):
    def __init__(self, status, message):
        super(_HttpError, self).__init__(message)
        self.status = status


class _Headers(dict):
    def has_key(self, key):
        return key in self


class _Route(object):
    def __init__(self, name, route, controller, action):
        self.name = name
        self.route = route
        self.controller = controller
        self.action = action
        self.maxkeys = ()


class _Mapper(object):
    def __init__(self):
        self.matchlist = []
        self.maxkeys = {(): []}
        self._routenames = {}
        self.routes = {}
        self.create_count = 0
        self.fail_next_create = False

    def connect(self, name, route, controller, action):
        item = _Route(name, route, controller, action)
        self.matchlist.append(item)
        self.maxkeys[()].append(item)
        self._routenames[name] = item

    def create_regs(self):
        self.create_count += 1
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError("route refresh failed")
        self.routes = dict(
            (item.name, (item.route, item.controller, item.action))
            for item in self.matchlist)


class _RoutesDispatcher(object):
    def __init__(self):
        self.controllers = {}
        self.mapper = _Mapper()

    @property
    def routes(self):
        return self.mapper.routes

    def connect(self, name, route, controller, action):
        self.controllers[name] = controller
        self.mapper.connect(name, route, name, action)


def _module(name, **attributes):
    result = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(result, key, value)
    return result


def _load_http_module():
    cherrypy = _module("cherrypy")
    cherrypy.config = lambda **unused: (lambda function: function)
    cherrypy.expose = lambda function: function
    cherrypy.request = _Namespace(headers={})
    cherrypy.dispatch = _Namespace(
        RoutesDispatcher=_RoutesDispatcher)
    cherrypy.HTTPError = _HttpError
    cherrypy._cpreqbody = _Namespace(SizedReader=object)
    cherrypy._cpcompat = _Namespace(
        ntob=lambda value: value.encode("utf-8"))

    thread_module = _module("thread", AsyncThread=_AsyncThread)
    log_module = _module(
        "zstacklib.utils.log",
        get_logger=lambda unused_name: _Logger(),
        get_logfile_path=lambda: None,
        mask_sensitive_field=lambda unused_cmd, body: body)
    debug_module = _module(
        "zstacklib.utils.debug", install_runtime_tracedumper=lambda: None)

    replacements = {
        "cherrypy": cherrypy,
        "thread": thread_module,
        "zstacklib.utils.jsonobject": _module("zstacklib.utils.jsonobject"),
        "zstacklib.utils.log": log_module,
        "zstacklib.utils.linux": _module("zstacklib.utils.linux"),
        "zstacklib.utils.debug": debug_module,
    }
    previous = dict((name, sys.modules.get(name)) for name in replacements)
    missing_string_type = not hasattr(types, "StringType")
    if missing_string_type:
        types.StringType = str
    try:
        sys.modules.update(replacements)
        source_path = os.path.realpath(os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "zstacklib",
            "zstacklib", "utils", "http.py"))
        return _Namespace(**runpy.run_path(source_path))
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if missing_string_type:
            delattr(types, "StringType")


class AsyncHandlerRestartFenceIntegrationTest(unittest.TestCase):
    def setUp(self):
        AgentRestartFence.reset_for_test()
        self.http = _load_http_module()
        self.http.AsyncUirHandler.HANDLER_DICT.clear()

    def tearDown(self):
        self.http.AsyncUirHandler.HANDLER_DICT.clear()
        AgentRestartFence.reset_for_test()

    def test_duplicate_worker_cannot_release_original_task_ownership(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def execute(request):
            calls.append(request)
            first_started.set()
            release_first.wait(2)
            return "ok"

        uri = self.http.AsyncUri()
        uri.func = execute
        handler = self.http.AsyncUirHandler(uri)
        handler._get_callback_uri = lambda unused_request: "http://callback"
        handler._check_response = lambda unused_content: None
        worker = handler._run_index.func
        worker_globals = getattr(worker, "__globals__", None)
        if worker_globals is None:
            worker_globals = worker.func_globals
        worker_globals["json_post"] = lambda *unused_args, **unused_kwargs: None
        request = _Namespace(headers={}, body=None)

        self.assertTrue(AgentRestartFence.enter_request())
        first = threading.Thread(
            target=lambda: worker("same-task", request))
        first.start()
        self.assertTrue(first_started.wait(1))

        self.assertTrue(AgentRestartFence.enter_request())
        worker("same-task", request)

        self.assertIn("same-task", self.http.AsyncUirHandler.HANDLER_DICT)

        self.assertTrue(AgentRestartFence.enter_request())
        worker("same-task", request)
        self.assertEqual(1, len(calls))

        release_first.set()
        first.join(1)
        self.assertFalse(first.is_alive())
        self.assertEqual(0, AgentRestartFence.snapshot()["activeRequestCount"])

    def test_async_index_returns_conflict_while_restart_fence_is_active(self):
        uri = self.http.AsyncUri()
        uri.func = lambda unused_request: "ok"
        handler = self.http.AsyncUirHandler(uri)
        headers = _Headers({self.http.TASK_UUID: "fenced-task"})
        self.http.cherrypy.request = _Namespace(
            headers=headers, body=None, method="POST", query_string=None)
        acquired, unused_snapshot = AgentRestartFence.acquire(0.01, 0.1)
        self.assertTrue(acquired)

        with self.assertRaises(_HttpError) as raised:
            handler.index()

        self.assertEqual(409, raised.exception.status)
        self.assertEqual(0, AgentRestartFence.snapshot()["activeRequestCount"])

    def test_route_registered_after_build_is_mapped_immediately(self):
        server = self.http.HttpServer()
        server.mapper = _RoutesDispatcher()

        server.register_sync_uri("/late", lambda unused_request: "ok")

        self.assertIn("/late", server.mapper.routes)
        self.assertIn("/late/", server.mapper.routes)

    def test_unregister_uri_removes_handler_and_dispatch_aliases(self):
        server = self.http.HttpServer()
        server.mapper = _RoutesDispatcher()
        server.register_sync_uri("/late", lambda unused_request: "ok")
        self.assertIn("/late", server.mapper.routes)

        server.unregister_uri("/late")

        self.assertNotIn("/late", server.sync_uri_handlers)
        self.assertNotIn("/late", server.mapper.routes)
        self.assertNotIn("/late/", server.mapper.routes)

    def test_batch_registration_publishes_all_routes_with_one_refresh(self):
        server = self.http.HttpServer()
        server.mapper = _RoutesDispatcher()

        server.register_uri_batch((
            ("sync", "/first", lambda unused_request: "first"),
            ("async", "/second", lambda unused_request: "second"),
        ))

        self.assertEqual(1, server.mapper.mapper.create_count)
        self.assertIn("/first", server.mapper.routes)
        self.assertIn("/second", server.mapper.routes)

    def test_batch_registration_rolls_back_a_failed_route_refresh(self):
        server = self.http.HttpServer()
        server.mapper = _RoutesDispatcher()
        server.mapper.mapper.fail_next_create = True

        with self.assertRaises(RuntimeError):
            server.register_uri_batch((
                ("sync", "/first", lambda unused_request: "first"),
                ("sync", "/second", lambda unused_request: "second"),
            ))

        self.assertNotIn("/first", server.sync_uri_handlers)
        self.assertNotIn("/second", server.sync_uri_handlers)
        self.assertNotIn("/first", server.mapper.routes)
        self.assertNotIn("/second", server.mapper.routes)

    def test_root_route_does_not_create_an_empty_dispatch_alias(self):
        server = self.http.HttpServer()
        server.mapper = _RoutesDispatcher()

        server.register_sync_uri("/", lambda unused_request: "root")

        self.assertIn("/", server.mapper.routes)
        self.assertNotIn("", server.mapper.routes)

    def test_http_start_runs_loader_callback_from_post_start_event(self):
        server = self.http.HttpServer()
        events = []
        subscriptions = []

        class Engine(object):
            def subscribe(self, channel, callback, priority=None):
                subscriptions.append((channel, callback, priority))

            def unsubscribe(self, channel, callback):
                item = (channel, callback, 100)
                if item in subscriptions:
                    subscriptions.remove(item)

            def publish(self, channel):
                for item in list(subscriptions):
                    if item[0] == channel:
                        item[1]()

        server._build = lambda: events.append('build')
        self.http.cherrypy.engine = Engine()

        def quickstart(unused_server):
            events.append('quickstart')
            self.assertEqual('start', subscriptions[0][0])
            self.assertEqual(100, subscriptions[0][2])
            self.http.cherrypy.engine.publish('start')
            self.http.cherrypy.engine.publish('start')

        self.http.cherrypy.quickstart = quickstart
        try:
            server.start(lambda: events.append('external-loader'))
        except TypeError as error:
            self.fail('HTTP start callback is not supported: %s' % error)

        self.assertEqual(
            ['build', 'quickstart', 'external-loader'], events)
        self.assertEqual([], subscriptions)

    def test_threaded_http_start_forwards_post_start_callback(self):
        server = self.http.HttpServer()
        events = []

        def start(on_started=None):
            events.append('http-thread')
            if on_started is not None:
                on_started()

        server.start = start
        try:
            server.start_in_thread(
                lambda: events.append('external-loader'))
        except TypeError as error:
            self.fail('threaded HTTP start dropped its callback: %s' % error)

        self.assertEqual(['http-thread', 'external-loader'], events)


if __name__ == "__main__":
    unittest.main()
