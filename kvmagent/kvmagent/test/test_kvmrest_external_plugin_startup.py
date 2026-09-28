# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import runpy
import sys
import threading
import types
import unittest

try:
    import builtins
except ImportError:
    import __builtin__ as builtins


class _Logger(object):
    def debug(self, *unused_args, **unused_kwargs):
        pass

    def warn(self, *unused_args, **unused_kwargs):
        pass


class _ClassHttpServer(object):
    def __init__(self):
        self.logfile_path = None


class _ConstructorExternalRegistry(object):
    def __init__(self, *unused_args, **unused_kwargs):
        self.status_endpoint_registered = False

    def discover(self):
        raise AssertionError(
            "external plugin discovery must not run in service construction")

    def register_status_endpoint(self):
        self.status_endpoint_registered = True


def _module(name, **attributes):
    result = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(result, key, value)
    return result


def _load_service_class():
    plugin_module = _module(
        "zstacklib.utils.plugin", Plugin=object,
        PluginRegistry=lambda unused_path: None)
    http_module = _module(
        "zstacklib.utils.http", HttpServer=_ClassHttpServer,
        UriBuilder=lambda unused_uri: None)
    log_module = _module(
        "zstacklib.utils.log",
        get_logger=lambda unused_name: _Logger(),
        get_logfile_path=lambda: None)
    linux_module = _module("zstacklib.utils.linux", HOST_ARCH="x86_64")
    replacements = {
        "zstacklib.utils.plugin": plugin_module,
        "zstacklib.utils.http": http_module,
        "zstacklib.utils.log": log_module,
        "zstacklib.utils.jsonobject": _module(
            "zstacklib.utils.jsonobject", dumps=lambda value: value),
        "zstacklib.utils.daemon": _module(
            "zstacklib.utils.daemon", Daemon=object),
        "zstacklib.utils.linux": linux_module,
        "zstacklib.utils.qemu": _module("zstacklib.utils.qemu"),
    }
    previous = dict((name, sys.modules.get(name)) for name in replacements)
    previous_reload = getattr(builtins, "reload", None)
    previous_encoding = getattr(sys, "setdefaultencoding", None)
    kvmagent_package_root = os.path.realpath(os.path.join(
        os.path.dirname(__file__), "..", ".."))
    zstacklib_package_root = os.path.realpath(os.path.join(
        kvmagent_package_root, "..", "zstacklib"))
    previous_sys_path = list(sys.path)
    builtins.reload = lambda module: module
    sys.setdefaultencoding = lambda unused_encoding: None
    try:
        sys.modules.update(replacements)
        sys.path[0:0] = [kvmagent_package_root, zstacklib_package_root]
        source_path = os.path.realpath(os.path.join(
            os.path.dirname(__file__), "..", "kvmagent.py"))
        namespace = runpy.run_path(
            source_path, run_name="kvmagent._startup_test_entrypoint")
        return namespace["KvmRESTService"]
    finally:
        sys.path[:] = previous_sys_path
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if previous_reload is None:
            delattr(builtins, "reload")
        else:
            builtins.reload = previous_reload
        if previous_encoding is None:
            delattr(sys, "setdefaultencoding")
        else:
            sys.setdefaultencoding = previous_encoding


class _BuiltInRegistry(object):
    def __init__(self):
        self.stopped = threading.Event()

    def configure_plugins(self, unused_config):
        pass

    def start_plugins(self):
        pass

    def stop_plugins(self):
        self.stopped.set()


class _BlockingExternalRegistry(object):
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release

    def load_and_start(self, unused_config):
        self.entered.set()
        self.release.wait(2)


class _OrderingExternalRegistry(_BlockingExternalRegistry):
    def __init__(self, entered, release, http_started, observations):
        super(_OrderingExternalRegistry, self).__init__(entered, release)
        self.http_started = http_started
        self.observations = observations

    def load_and_start(self, unused_config):
        self.observations.append(self.http_started.is_set())
        super(_OrderingExternalRegistry, self).load_and_start(unused_config)


class _BlockingExternalStopRegistry(_BlockingExternalRegistry):
    def __init__(self, entered, release, stop_entered, release_stop):
        super(_BlockingExternalStopRegistry, self).__init__(entered, release)
        self.stop_entered = stop_entered
        self.release_stop = release_stop
        self.stop_requested = threading.Event()

    def request_stop(self):
        self.stop_requested.set()

    def stop(self):
        self.stop_entered.set()
        self.release_stop.wait(2)


class _HttpServer(object):
    def __init__(self, started, stopped=None):
        self.started = started
        self.stopped = stopped or threading.Event()

    def start_in_thread(self, on_started=None):
        self.started.set()
        if on_started is not None:
            on_started()

    def start(self, on_started=None):
        self.started.set()
        if on_started is not None:
            on_started()

    def stop(self):
        self.stopped.set()


class KvmRestExternalPluginStartupTest(unittest.TestCase):
    def setUp(self):
        self.service_class = _load_service_class()

    def _service(self, entered, release, http_started):
        service = self.service_class.__new__(self.service_class)
        service.plugin_rgty = _BuiltInRegistry()
        service.external_plugin_rgty = _BlockingExternalRegistry(
            entered, release)
        service.http_server = _HttpServer(http_started)
        return service

    def test_constructor_does_not_discover_external_plugins_before_http_start(self):
        initializer = self.service_class.__init__
        initializer = getattr(initializer, "im_func", initializer)
        initializer_globals = getattr(initializer, "__globals__", None)
        if initializer_globals is None:
            initializer_globals = initializer.func_globals
        external_plugin = initializer_globals["external_plugin"]
        original_registry = external_plugin.ExternalPluginRegistry
        created = []

        def registry_factory(*args, **kwargs):
            registry = _ConstructorExternalRegistry(*args, **kwargs)
            created.append(registry)
            return registry

        external_plugin.ExternalPluginRegistry = registry_factory
        try:
            self.service_class({})
        finally:
            external_plugin.ExternalPluginRegistry = original_registry

        self.assertEqual(1, len(created))
        self.assertTrue(created[0].status_endpoint_registered)

    def _assert_http_starts_while_external_plugin_is_blocked(self, in_thread):
        entered = threading.Event()
        release = threading.Event()
        http_started = threading.Event()
        service = self._service(entered, release, http_started)
        worker = threading.Thread(target=lambda: service.start(in_thread))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(http_started.wait(0.1))
        finally:
            release.set()
            worker.join(1)

    def test_threaded_http_starts_while_external_plugin_is_blocked(self):
        self._assert_http_starts_while_external_plugin_is_blocked(True)

    def test_foreground_http_starts_while_external_plugin_is_blocked(self):
        self._assert_http_starts_while_external_plugin_is_blocked(False)

    def test_foreground_external_loader_observes_started_http_service(self):
        entered = threading.Event()
        release = threading.Event()
        http_started = threading.Event()
        observations = []
        service = self._service(entered, release, http_started)
        service.external_plugin_rgty = _OrderingExternalRegistry(
            entered, release, http_started, observations)
        worker = threading.Thread(target=lambda: service.start(False))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual([True], observations)
        finally:
            release.set()
            worker.join(1)

    def test_http_stops_before_a_blocked_external_plugin_stop_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        stop_entered = threading.Event()
        release_stop = threading.Event()
        http_stopped = threading.Event()
        service = self._service(entered, release, threading.Event())
        service.external_plugin_rgty = _BlockingExternalStopRegistry(
            entered, release, stop_entered, release_stop)
        service.http_server = _HttpServer(threading.Event(), http_stopped)
        worker = threading.Thread(target=service.stop)
        worker.start()
        try:
            self.assertTrue(service.external_plugin_rgty.stop_requested.wait(1))
            self.assertTrue(http_stopped.wait(1))
            self.assertTrue(stop_entered.wait(1))
            self.assertTrue(worker.is_alive())
        finally:
            release_stop.set()
            worker.join(1)


if __name__ == "__main__":
    unittest.main()
