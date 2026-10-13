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
        other._cleanup_zs_tools_net = mock.Mock(side_effect=RuntimeError('QGA disconnected'))
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
        self.assertEqual([mock.call(41), mock.call(42)], self.vm._cleanup_zs_tools_net.call_args_list)
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
        self.vm._query_zs_tools_net_process_state = mock.Mock(side_effect=['running', 'absent'])
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.vm._cleanup_zs_tools_net(42)
        args = self.vm.guest_exec.call_args[0][0]
        self.assertEqual(['/PID', '42', '/T', '/F'], args['arg'])
        self.assertTrue(args['path'].endswith('taskkill.exe'))

    def test_failed_taskkill_is_not_confirmed_cleanup(self):
        del self.vm._cleanup_zs_tools_net
        self.vm._query_zs_tools_net_process_state = mock.Mock(side_effect=['running', 'running'])
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 1}
        with self.assertRaises(Exception):
            self.vm._cleanup_zs_tools_net(42)

    def test_lost_exited_response_recovers_across_instances(self):
        del self.vm._cleanup_zs_tools_net
        # Exit response consumed the QGA record and was lost. During this
        # first call the channel also prevents an independent OS query.
        self.vm.guest_exec_status_with_timeout.side_effect = RuntimeError('PID record not found')
        self.vm._query_zs_tools_net_process_state = mock.Mock(side_effect=RuntimeError('channel lost'))
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.assertEqual(42, self.qga._zs_tools_net_pending['vm-a'])
        # A new instance after channel recovery still cannot read the consumed
        # PID record, but Windows confirms the entire old tree is absent.
        other = self.qga.VmQga.__new__(self.qga.VmQga)
        other.vm_uuid = 'vm-a'
        other.guest_exec = mock.Mock(return_value={'pid': 99})
        other.guest_exec_status_with_timeout = mock.Mock(return_value={'exited': True, 'exitcode': 0})
        other._query_zs_tools_net_process_state = mock.Mock(return_value='absent')
        self.assertEqual((0, ''), other.guest_exec_zs_tools('net', '{}', wait=1, retry=3))
        self.assertEqual([mock.call(42), mock.call(99)], other._query_zs_tools_net_process_state.call_args_list)
        other.guest_exec.assert_called_once()
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)

    def test_taskkill_not_found_is_resolved_by_os_absence(self):
        del self.vm._cleanup_zs_tools_net
        self.vm._query_zs_tools_net_process_state = mock.Mock(side_effect=['running', 'absent'])
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.side_effect = [
            {'exited': True, 'exitcode': 128}, RuntimeError('PID record not found')]
        self.vm._cleanup_zs_tools_net(42)
        self.assertEqual(2, self.vm._query_zs_tools_net_process_state.call_count)

    def test_reused_pid_or_orphaned_tree_is_never_killed(self):
        del self.vm._cleanup_zs_tools_net
        for state in ('identity_mismatch', 'orphaned'):
            self.vm._query_zs_tools_net_process_state = mock.Mock(return_value=state)
            with self.assertRaises(Exception):
                self.vm._cleanup_zs_tools_net(42)
        self.vm.guest_exec.assert_not_called()

    def test_lost_taskkill_response_requires_os_confirmation(self):
        del self.vm._cleanup_zs_tools_net
        self.vm._query_zs_tools_net_process_state = mock.Mock(side_effect=['running', 'absent'])
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.side_effect = RuntimeError('response lost')
        self.vm._cleanup_zs_tools_net(42)

    def test_os_query_parses_independent_process_state(self):
        self.vm.ZS_TOOLS_PATN_WIN = r'C:\\Program Files\\GuestTools\\zs-tools\\zs-tools.exe'
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {
            'exited': True, 'exitcode': 0,
            'out-data': b'ZS_TOOLS_NET_STATE|absent|42|0\r\n'}
        self.assertEqual('absent', self.vm._query_zs_tools_net_process_state(42))
        self.assertTrue(self.vm.guest_exec.call_args[0][0]['path'].endswith('powershell.exe'))

    def test_os_query_cannot_confirm_absence_with_descendants(self):
        self.vm.ZS_TOOLS_PATN_WIN = r'C:\\Program Files\\GuestTools\\zs-tools\\zs-tools.exe'
        self.vm.guest_exec.return_value = {'pid': 99}
        self.vm.guest_exec_status_with_timeout.return_value = {
            'exited': True, 'exitcode': 0,
            'out-data': b'ZS_TOOLS_NET_STATE|absent|42|1\r\n'}
        with self.assertRaises(Exception):
            self.vm._query_zs_tools_net_process_state(42)

    def test_pending_exited_parent_with_live_children_blocks_every_retry(self):
        del self.vm._cleanup_zs_tools_net
        self.qga._zs_tools_net_pending['vm-a'] = 41
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.vm._query_zs_tools_net_process_state = mock.Mock(return_value='orphaned')
        for unused in range(3):
            code, message = self.run_net()
            self.assertEqual(1, code)
            self.assertIn('new execution blocked', message)
            self.assertEqual(41, self.qga._zs_tools_net_pending['vm-a'])
        self.vm.guest_exec.assert_not_called()
        self.assertEqual([mock.call(41)] * 3, self.vm._query_zs_tools_net_process_state.call_args_list)
        self.vm.guest_exec_status_with_timeout.assert_not_called()

    def test_normal_parent_exit_with_live_children_also_retains_pending(self):
        del self.vm._cleanup_zs_tools_net
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.vm._query_zs_tools_net_process_state = mock.Mock(return_value='orphaned')
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.assertEqual(42, self.qga._zs_tools_net_pending['vm-a'])
        self.assertIn('orphaned', message)

    def test_ps2_query_and_lost_exit_record_allow_three_cross_instance_retries(self):
        import base64
        self.qga._zs_tools_net_pending['vm-a'] = 41
        scripts = []
        launches = []
        for unused in range(3):
            vm = self.qga.VmQga.__new__(self.qga.VmQga)
            vm.vm_uuid = 'vm-a'
            vm.ZS_TOOLS_PATN_WIN = r'C:\Program Files\GuestTools\zs-tools\zs-tools.exe'
            def execute(args):
                if args['path'].endswith('powershell.exe'):
                    script = base64.b64decode(args['arg'][-1]).decode('utf-16le')
                    # Model the PS2 compatibility failure rather than silently
                    # returning a successful mocked response for a PS3 script.
                    if 'ConvertTo-Json' in script or '[PSCustomObject]' in script:
                        raise RuntimeError('PowerShell 2.0 unsupported command')
                    scripts.append(script)
                    return {'pid': 100}
                launches.append(args)
                return {'pid': 42}
            def status(pid, timeout):
                if pid == 41:
                    raise RuntimeError('consumed exit record')
                if pid == 100:
                    target = 41 if len(scripts) == 1 else 42
                    return {'exited': True, 'exitcode': 0,
                            'out-data': ('ZS_TOOLS_NET_STATE|absent|%s|0' % target).encode('ascii')}
                return {'exited': True, 'exitcode': 0}
            vm.guest_exec = mock.Mock(side_effect=execute)
            vm.guest_exec_status_with_timeout = mock.Mock(side_effect=status)
            self.assertEqual((0, ''), vm.guest_exec_zs_tools('net', '{}', wait=1, retry=3))
            self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)
        self.assertEqual(3, len(launches))
        self.assertEqual(4, len(scripts))

if __name__ == '__main__':
    unittest.main()
