"""Verify the bounded openEuler transaction at the remote-command boundary."""
import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'ansible')))
try:
    import lvm_package_transition as transition
except ImportError:
    transition = None


OLD = ('lvm2|8|2.03.14|7.oe2203sp1|aarch64\n'
       'lvm2-lockd|8|2.03.14|7.oe2203sp1|aarch64\n'
       'device-mapper|8|1.02.181|7.oe2203sp1|aarch64\n'
       'device-mapper-event|8|1.02.181|7.oe2203sp1|aarch64\n')
EXPECTED = {
    'lvm2-0:2.03.12-13.oe2203sp1.aarch64',
    'lvm2-libs-0:2.03.12-13.oe2203sp1.aarch64',
    'lvm2-lockd-0:2.03.12-13.oe2203sp1.aarch64',
    'device-mapper-0:1.02.177-13.oe2203sp1.aarch64',
    'device-mapper-libs-0:1.02.177-13.oe2203sp1.aarch64',
    'device-mapper-event-0:1.02.177-13.oe2203sp1.aarch64',
    'device-mapper-event-libs-0:1.02.177-13.oe2203sp1.aarch64',
}


class PackageTransitionTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(transition, 'openEuler legacy package transition is missing')
        self.installed = OLD
        self.calls = []
        self.fail_query = False
        self.fail_install = False
        self.keep_old = False

    def remote(self, command, host, **kwargs):
        self.calls.append(shlex.split(command))
        if self.calls[-1][0] == 'rpm':
            return not self.fail_query, self.installed
        if self.fail_install:
            return False
        if self.keep_old:
            return True
        # Emulate only the package boundary, not dependency resolution:
        # install the requested exact set after one successful transaction.
        self.installed = '\n'.join(
            '%s|0|%s|13.oe2203sp1|aarch64' %
            (name, '2.03.12' if name.startswith('lvm2') else '1.02.177')
            for name in ('lvm2', 'lvm2-libs', 'lvm2-lockd', 'device-mapper',
                         'device-mapper-libs', 'device-mapper-event', 'device-mapper-event-libs'))
        return True

    def apply(self, release='h2203sp1o', arch='aarch64', repo='zstack-local', skipped=()):
        return transition.migrate_legacy_packages(release, arch, repo, skipped, object(), self.remote)

    def test_legacy_packages_and_split_libraries_use_one_exact_transaction(self):
        self.assertTrue(self.apply())
        transactions = [c for c in self.calls if c[0] == 'yum']
        self.assertEqual(1, len(transactions))
        command = transactions[0]
        self.assertEqual(EXPECTED, set(command[command.index('install') + 2:]))
        self.assertIn('--disablerepo=*', command)
        self.assertIn('--enablerepo=zstack-local', command)
        self.assertNotIn('--allowerasing', command)
        self.assertNotIn('--skip-broken', command)

    def test_repeated_deploy_does_not_downgrade_a_second_time(self):
        self.apply()
        self.calls = []
        self.assertFalse(self.apply())
        self.assertEqual(['rpm'], [c[0] for c in self.calls])

    def test_new_install_uses_the_normal_dependency_path(self):
        self.installed = 'bash|0|5.1|1|aarch64\n'
        self.assertFalse(self.apply())
        self.assertEqual(['rpm'], [c[0] for c in self.calls])

    def test_other_platforms_and_online_repository_do_not_run(self):
        for args in [('ky10sp3', 'aarch64', 'zstack-local'),
                     ('h2203sp1o', 'x86_64', 'zstack-local'),
                     ('h2203sp1o', 'aarch64', 'false')]:
            self.assertFalse(self.apply(*args))
        self.assertEqual([], self.calls)

    def test_skipped_family_member_prevents_partial_transition(self):
        with self.assertRaises(transition.PackageTransitionError):
            self.apply(skipped=['device-mapper-libs'])
        self.assertEqual(['rpm'], [c[0] for c in self.calls])

    def test_unknown_mixed_family_is_not_overwritten(self):
        self.installed += 'lvm2-libs|8|9.9.9|custom|aarch64\n'
        with self.assertRaises(transition.PackageTransitionError):
            self.apply()
        self.assertEqual(['rpm'], [c[0] for c in self.calls])

    def test_query_failure_stops_deployment(self):
        self.fail_query = True
        with self.assertRaises(transition.PackageTransitionError):
            self.apply()
        self.assertEqual(['rpm'], [c[0] for c in self.calls])

    def test_transaction_failure_is_not_reported_as_success(self):
        self.fail_install = True
        with self.assertRaises(transition.PackageTransitionError):
            self.apply()
        self.assertEqual(OLD, self.installed)

    def test_success_exit_without_replacement_stops_deployment(self):
        self.keep_old = True
        with self.assertRaises(transition.PackageTransitionError):
            self.apply()


if __name__ == '__main__':
    unittest.main()
