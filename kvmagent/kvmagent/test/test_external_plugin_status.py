# -*- coding: utf-8 -*-
from __future__ import absolute_import

import unittest

from kvmagent.external_plugin_registry import ExternalPluginRecord
from kvmagent.external_plugin_status import status_envelope


class ExternalPluginStatusTest(unittest.TestCase):
    def test_projection_is_detached_and_schema_versioned(self):
        record = ExternalPluginRecord("sample")
        record.failure = {"code": "PLUGIN_RUNTIME_VERSION_UNAVAILABLE"}
        record.capabilities = {"incremental": True}
        record.registered_routes = ["/example/z", "/example/a"]

        result = status_envelope([record], "2026-08-20T00:00:00Z")
        projected = result["plugins"][0]
        projected["failure"]["code"] = "changed"
        projected["capabilities"]["incremental"] = False

        self.assertEqual("1", result["schemaVersion"])
        self.assertEqual(["/example/a", "/example/z"],
                         projected["registeredRoutes"])
        self.assertEqual("PLUGIN_RUNTIME_VERSION_UNAVAILABLE",
                         record.failure["code"])
        self.assertTrue(record.capabilities["incremental"])


if __name__ == "__main__":
    unittest.main()
