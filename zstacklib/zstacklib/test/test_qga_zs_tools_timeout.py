# -*- coding: utf-8 -*-
"""Regression coverage for orphaned Windows network configuration tasks."""
import unittest
import test_qga_guest_exec_program as loader

mock = loader.mock

class ZsToolsTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.modules = loader.sys.modules.copy()
        self.qga = loader._load_qga_module()
        self.qga.log.mask_sensitive_field = lambda obj, value: '<masked>'
        self.vm = self.qga.VmQga.__new__(self.qga.VmQga)
        self.vm.vm_uuid = 'vm-a'
        self.vm.guest_exec = mock.Mock(return_value={'pid': 42})
        self.vm.guest_exec_status_with_timeout = mock.Mock(return_value={'exited': False})
        self.vm._cleanup_zs_tools_net = mock.Mock()
        self.now = 100.0
        def sleep(seconds):
            self.now += seconds
        self.patches = [mock.patch.object(self.qga.time, 'time', side_effect=lambda: self.now),
                        mock.patch.object(self.qga.time, 'sleep', side_effect=sleep)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        loader._restore_sys_modules(self.modules)

    def run_net(self):
        return self.vm.guest_exec_zs_tools('net', 'secret-config', wait=1, retry=3)

    def test_timeout_kills_tree_and_reports_pid_elapsed(self):
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.vm._cleanup_zs_tools_net.assert_called_once_with(42)
        self.assertIn('pid=42', message)
        self.assertIn('elapsed=3.000s', message)
        self.assertIn('exceeded deadline', message)
        self.assertNotIn('secret-config', message)
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)

    def test_failed_cleanup_blocks_next_instance(self):
        self.vm._cleanup_zs_tools_net.side_effect = RuntimeError('QGA disconnected')
        self.run_net()
        other = self.qga.VmQga.__new__(self.qga.VmQga)
        other.vm_uuid = 'vm-a'
        other.guest_exec = mock.Mock()
        other.guest_exec_status_with_timeout = mock.Mock(side_effect=RuntimeError('QGA disconnected'))
        code, message = other.guest_exec_zs_tools('net', '{}', wait=1, retry=3)
        self.assertEqual(1, code)
        self.assertIn('new execution blocked', message)
        other.guest_exec.assert_not_called()
        self.assertEqual(42, self.qga._zs_tools_net_pending['vm-a'])

    def test_exited_previous_pid_allows_next_request(self):
        self.qga._zs_tools_net_pending['vm-a'] = 41
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.assertEqual((0, ''), self.run_net())
        self.vm.guest_exec.assert_called_once()
        self.vm._cleanup_zs_tools_net.assert_not_called()
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)

    def test_status_error_also_cleans_tree(self):
        self.vm.guest_exec_status_with_timeout.side_effect = RuntimeError('channel lost')
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.assertIn('channel lost', message)
        self.vm._cleanup_zs_tools_net.assert_called_once_with(42)

    def test_wall_clock_budget_includes_slow_status_calls(self):
        def status(pid, timeout):
            self.now += 2
            return {'exited': False}
        self.vm.guest_exec_status_with_timeout.side_effect = status
        self.run_net()
        self.assertEqual(1, self.vm.guest_exec_status_with_timeout.call_count)

    def test_other_vm_can_run_while_previous_cleanup_pending(self):
        self.qga._zs_tools_net_pending['vm-b'] = 43
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.assertEqual((0, ''), self.run_net())
        self.assertEqual(43, self.qga._zs_tools_net_pending['vm-b'])

    def test_cleanup_uses_exact_pid_and_checks_taskkill_exit(self):
        del self.vm._cleanup_zs_tools_net
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.vm._cleanup_zs_tools_net(42)
        args = self.vm.guest_exec.call_args[0][0]
        self.assertEqual(['/PID', '42', '/T', '/F'], args['arg'])
        self.assertTrue(args['path'].endswith('taskkill.exe'))

    def test_failed_taskkill_is_not_confirmed_cleanup(self):
        del self.vm._cleanup_zs_tools_net
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 1}
        with self.assertRaises(Exception):
            self.vm._cleanup_zs_tools_net(42)

if __name__ == '__main__':
    unittest.main()
