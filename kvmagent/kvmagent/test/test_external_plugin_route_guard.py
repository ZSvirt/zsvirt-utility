# -*- coding: utf-8 -*-
from __future__ import absolute_import

import json
import unittest

from kvmagent.external_plugin_route_guard import guard_mutation


class ExternalPluginRouteGuardTest(unittest.TestCase):
    def test_compatible_mutation_is_delegated(self):
        guarded = guard_mutation(
            lambda value: "changed-" + value,
            lambda: True, lambda: "COMPATIBLE")
        self.assertEqual("changed-value", guarded("value"))

    def test_incompatible_runtime_is_fenced_with_stable_code(self):
        guarded = guard_mutation(
            lambda: "changed", lambda: False,
            lambda: "INCOMPATIBLE_RUNTIME")
        response = json.loads(guarded())
        self.assertFalse(response["success"])
        self.assertEqual("PLUGIN_RUNTIME_INCOMPATIBLE", response["errorCode"])

    def test_next_start_drift_has_distinct_repair_signal(self):
        guarded = guard_mutation(
            lambda: "changed", lambda: False,
            lambda: "DRIFTED_NEXT_START")
        response = json.loads(guarded())
        self.assertEqual("PLUGIN_NEXT_START_DRIFT", response["errorCode"])


if __name__ == "__main__":
    unittest.main()
