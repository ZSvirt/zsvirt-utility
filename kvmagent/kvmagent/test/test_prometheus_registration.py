#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import unittest

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugins"))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import prometheus_registration


class Registry(object):
    def __init__(self, fail=False):
        self.fail = fail
        self.collectors = []

    def register(self, collector):
        if self.fail:
            raise RuntimeError("registration failed")
        self.collectors.append(collector)


class PrometheusRegistrationTest(unittest.TestCase):
    def setUp(self):
        prometheus_registration._reset_for_test()

    def test_same_key_is_registered_only_once(self):
        registry = Registry()
        first, second = object(), object()
        self.assertTrue(prometheus_registration.register_once(registry, first, "exporter"))
        self.assertFalse(prometheus_registration.register_once(registry, second, "exporter"))
        self.assertEqual([first], registry.collectors)

    def test_failed_registration_can_be_retried(self):
        with self.assertRaises(RuntimeError):
            prometheus_registration.register_once(Registry(fail=True), object(), "exporter")
        registry = Registry()
        self.assertTrue(prometheus_registration.register_once(registry, object(), "exporter"))
        self.assertEqual(1, len(registry.collectors))

    def test_same_source_callback_loaded_by_aliases_is_registered_once(self):
        callbacks = []
        code = compile("def collect(): pass", "/plugins/prometheus.py", "exec")
        first, second = {}, {}
        exec(code, first)
        exec(code, second)
        self.assertTrue(prometheus_registration.register_callback_once(
            callbacks.append, first["collect"]))
        self.assertFalse(prometheus_registration.register_callback_once(
            callbacks.append, second["collect"]))
        self.assertEqual(1, len(callbacks))


if __name__ == "__main__":
    unittest.main()
