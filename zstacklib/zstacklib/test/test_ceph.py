import os
import unittest

from zstacklib.utils import ceph_pg


class TestCephPoolPgNum(unittest.TestCase):
    def _read_agent(self, relative_path):
        utility_root = os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            '..',
            '..',
            '..',
        ))
        with open(os.path.join(utility_root, relative_path), 'r') as agent_file:
            return agent_file.read()

    def test_hammer_keeps_legacy_pg_num(self):
        self.assertEqual(
            128,
            ceph_pg.get_compatible_pg_num_for_pool_create('0.94.10'),
        )

    def test_infernalis_uses_ceph_default_pg_num(self):
        self.assertEqual(
            0,
            ceph_pg.get_compatible_pg_num_for_pool_create('9.2.0'),
        )

    def test_tentacle_uses_ceph_default_pg_num(self):
        self.assertEqual(
            0,
            ceph_pg.get_compatible_pg_num_for_pool_create('20.2.3-0.el9'),
        )

    def test_creates_all_pools_with_ceph_default_pg_num(self):
        commands = []

        def shell_call(command):
            commands.append(command)

        ceph_pg.create_pools_with_compatible_pg_num(
            shell_call,
            ['root', 'data', 'cache'],
            '20.2.3',
        )

        self.assertEqual(
            [
                'ceph osd pool create root 0',
                'ceph osd pool create data 0',
                'ceph osd pool create cache 0',
            ],
            commands,
        )

    def test_hammer_creates_pools_with_legacy_pg_num(self):
        commands = []

        def shell_call(command):
            commands.append(command)

        ceph_pg.create_pools_with_compatible_pg_num(
            shell_call,
            ['root'],
            '0.94.10',
        )

        self.assertEqual(['ceph osd pool create root 128'], commands)

    def test_empty_pool_list_does_not_call_shell(self):
        def shell_call(command):
            self.fail('unexpected command: %s' % command)

        ceph_pg.create_pools_with_compatible_pg_num(
            shell_call,
            [],
            '20.2.3',
        )

    def test_agents_use_compatible_pool_creation_helper(self):
        primary_agent = self._read_agent(os.path.join(
            'cephprimarystorage',
            'cephprimarystorage',
            'cephagent.py',
        ))
        backup_agent = self._read_agent(os.path.join(
            'cephbackupstorage',
            'cephbackupstorage',
            'cephagent.py',
        ))

        self.assertEqual(
            2,
            primary_agent.count('ceph.create_pools_with_compatible_pg_num('),
        )
        self.assertEqual(
            1,
            backup_agent.count('ceph.create_pools_with_compatible_pg_num('),
        )
        self.assertNotIn('ceph osd pool create %s 128', primary_agent)
        self.assertNotIn('ceph osd pool create %s 128', backup_agent)


if __name__ == '__main__':
    unittest.main()
