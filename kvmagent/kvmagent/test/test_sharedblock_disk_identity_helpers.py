"""Check the production disk identity used by connect/add-disk set unions."""
import os
import re
import unittest
from test_vm_persistent_definition_helpers import Mock


class SharedBlockDiskIdentityTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'plugins', 'shared_block_plugin.py')
        with open(path) as source:
            text = source.read()
        start = text.index('class CheckDisk(')
        line = text[:start].count('\n')
        tail = text[start:]
        end = re.search(r'^class ', tail[1:], re.M)
        body = tail[:end.start()+1] if end else tail
        namespace = dict(bash=Mock(in_bash=lambda function: function))
        exec(compile('\n' * line + body, path, 'exec'), namespace)
        self.disk = namespace['CheckDisk']

    def test_equal_identifiers_have_equal_hashes(self):
        first, second = self.disk('same-wwid'), self.disk('same-wwid')
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_selected_and_all_disks_union_deduplicates_physical_identity(self):
        selected = set([self.disk('lun-a')])
        all_disks = set([self.disk('lun-a'), self.disk('lun-b')])
        merged = selected.union(all_disks)
        self.assertEqual(2, len(merged))
        self.assertEqual(['lun-a', 'lun-b'], sorted(d.identifier for d in merged))

    def test_distinct_identifiers_stay_distinct(self):
        self.assertEqual(2, len(set([self.disk('lun-a'), self.disk('lun-b')])))

    def test_non_disk_is_not_equal(self):
        self.assertFalse(self.disk('lun-a') == 'lun-a')


if __name__ == '__main__':
    unittest.main()
