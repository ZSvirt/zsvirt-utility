from __future__ import absolute_import

import sys
import unittest


@unittest.skipUnless(sys.version_info[0] == 2, "Python 2 startup compatibility")
class KvmDaemonExternalPluginImportTest(unittest.TestCase):
    def test_kdaemon_entrypoint_imports_external_plugin(self):
        from kvmagent import kdaemon

        self.assertIsNotNone(kdaemon.kvmagent.external_plugin)


if __name__ == "__main__":
    unittest.main()
