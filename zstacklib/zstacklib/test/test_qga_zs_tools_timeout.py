# -*- coding: utf-8 -*-
"""Task-bound cancellation must never turn stale PIDs into kill targets."""
import base64
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
        self.vm.ZS_TOOLS_PATN_WIN = r'C:\zs-tools.exe'
        self.vm.guest_exec = mock.Mock(return_value={'pid': 42})
        self.vm.guest_exec_status_with_timeout = mock.Mock(return_value={'exited': False})
        self.record = {'phase': 'running', 'pid': 42, 'wrapper_pid': 43}
        self.active = False
        self.finished = False
        self.vm._net_task_control = mock.Mock(side_effect=self.control)
        self.now = 100.0
        self.patches = [mock.patch.object(self.qga.time, 'time', side_effect=lambda: self.now),
                        mock.patch.object(self.qga.time, 'sleep', side_effect=self.sleep)]
        for patch in self.patches: patch.start()

    def control(self, task, action):
        if action in ('cancel', 'seal'):
            if self.active: raise RuntimeError('Job still active or cannot be observed')
            return dict(self.record, phase='cancelled')
        if action == 'inspect' and self.finished: return dict(self.record, phase='finished')
        if action == 'collect':
            if self.active: raise RuntimeError('Job still active')
            return dict(self.record, exitcode=0)
        return self.record.copy()

    def sleep(self, seconds): self.now += seconds

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()
        loader._restore_sys_modules(self.modules)

    def run_net(self):
        return self.vm.guest_exec_zs_tools('net', 'secret-config', wait=1, retry=3)

    def test_timeout_cancels_by_task_and_reports_safe_diagnostics(self):
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.assertIn('pid=42', message)
        self.assertIn('elapsed=3.000s', message)
        self.assertIn('exceeded deadline', message)
        self.assertNotIn('secret-config', message)
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)
        self.assertEqual('cancel', self.vm._net_task_control.call_args[0][1])

    def test_exited_parent_with_live_job_blocks_three_cross_instance_retries(self):
        self.active = True
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.assertEqual(1, self.run_net()[0])
        task = self.qga._zs_tools_net_pending['vm-a']
        for unused in range(3):
            other = self.qga.VmQga.__new__(self.qga.VmQga)
            other.__dict__.update(self.vm.__dict__)
            code, message = other.guest_exec_zs_tools('net', '{}', wait=1, retry=3)
            self.assertEqual(1, code)
            self.assertIn('new execution blocked', message)
            self.assertIs(task, self.qga._zs_tools_net_pending['vm-a'])
        self.assertEqual(1, self.vm.guest_exec.call_count)

    def test_lost_exited_status_recovers_from_job_absence(self):
        self.active = True
        self.vm.guest_exec_status_with_timeout.side_effect = RuntimeError('PID record gone')
        self.assertEqual(1, self.run_net()[0])
        self.active = False
        self.vm.guest_exec_status_with_timeout.side_effect = None
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.finished = True
        self.assertEqual((0, ''), self.run_net())
        self.assertEqual(2, self.vm.guest_exec.call_count)

    def test_legacy_numeric_pending_is_not_killed_or_released(self):
        self.qga._zs_tools_net_pending['vm-a'] = 42
        for unused in range(3): self.assertEqual(1, self.run_net()[0])
        self.vm.guest_exec.assert_not_called()
        self.assertEqual(42, self.qga._zs_tools_net_pending['vm-a'])

    def test_stale_root_and_wrapper_pid_reuse_do_not_change_cancellation_identity(self):
        task = {'task_id': 'a'*32, 'phase': 'submitted', 'pid': 9001, 'wrapper_pid': 9002}
        self.record.update(pid=9001, wrapper_pid=9002)
        self.vm._recover_net_task(task)
        script = self.vm._net_task_script(task, 'cancel')
        self.assertNotIn('9001', script)
        self.assertNotIn('9002', script)
        self.assertNotIn('taskkill', script.lower())
        self.assertNotIn('OpenProcess', script)
        self.assertNotIn('Get-WmiObject', script)
        self.assertIn("$recordPath + '.cancel'", script)
        self.assertIn('OpenJobObject(4, false, name)', script)
        self.assertIn('[ZStackNetJob]::Absent($jobName)', script)

    def test_job_binding_precedes_child_creation_and_survives_wrapper_crash(self):
        script = self.vm._net_task_script({'task_id': 'b'*32}, 'launch', '{}')
        self.assertLess(script.index('[ZStackNetJob]::Enter($jobName)'), script.index('Start-Process -FilePath'))
        self.assertIn('Marshal.WriteInt32(info, 16, 0x2000)', script)
        self.assertIn('AssignProcessToJobObject(job, GetCurrentProcess())', script)
        self.assertIn('[ZStackNetJob]::Stop($job)', script)
        self.assertIn('[ZStackNetJob]::Active($job) -ne 1', script)

    def test_job_query_access_error_cannot_release_pending(self):
        self.vm._net_task_control.side_effect = RuntimeError('access denied')
        task = {'task_id': 'c'*32, 'phase': 'unknown'}
        self.qga._zs_tools_net_pending['vm-a'] = task
        self.assertEqual(1, self.run_net()[0])
        self.assertIs(task, self.qga._zs_tools_net_pending['vm-a'])
        self.vm.guest_exec.assert_not_called()

    def test_control_rejects_old_receipt_protocol(self):
        del self.vm._net_task_control
        task = {'task_id': 'd'*32}
        self.vm.guest_exec_status_with_timeout.return_value = {
            'exited': True, 'exitcode': 0,
            'out-data': ('ZSN1|%s|finished|42|43' % task['task_id']).encode('ascii')}
        with self.assertRaises(Exception): self.vm._net_task_control(task, 'cancel')

    def test_finished_receipt_cannot_release_a_live_job(self):
        self.finished = True
        self.active = True
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0}
        self.assertEqual(1, self.run_net()[0])
        self.assertIn('vm-a', self.qga._zs_tools_net_pending)
        self.vm.guest_exec_status_with_timeout.assert_not_called()

    def test_lost_wrapper_exit_record_uses_bound_result_instead(self):
        self.finished = True
        self.vm.guest_exec_status_with_timeout.side_effect = RuntimeError('old PID record lost')
        self.assertEqual((0, ''), self.run_net())
        self.vm.guest_exec_status_with_timeout.assert_called_once()

    def test_failed_late_launcher_cannot_overwrite_cancelled_receipt(self):
        script = self.vm._net_task_script({'task_id': 'e'*32}, 'launch', '{}')
        catch = script.split("[Console]::Error.WriteLine('Net task launcher failed')", 1)[1]
        self.assertIn("$record[2] -eq 'reserved'", catch)
        self.assertNotIn("if ($locked -ne $null) { Write-Record 'failed'", catch)

    def test_stale_control_pid_reply_is_consumed_until_nonce_matches(self):
        del self.vm._net_task_control
        task = {'task_id': 'f'*32}
        control_id = '1'*32
        fixed = mock.Mock(hex=control_id)
        replies = [
            {'exited': True, 'exitcode': 0, 'out-data': b'old command output\xff'},
            {'exited': True, 'exitcode': 1},
            {'exited': True, 'exitcode': 0, 'out-data':
             ('ZSN2|%s|cancelled|42|43|%s' % (task['task_id'], '2'*32)).encode('ascii')},
            {'exited': True, 'exitcode': 0, 'out-data':
             ('ZSN2|%s|cancelled|42|43|%s' % (task['task_id'], control_id)).encode('ascii')}]
        self.vm.guest_exec_status_with_timeout.side_effect = replies
        with mock.patch.object(self.qga.uuid, 'uuid4', return_value=fixed):
            self.assertEqual('cancelled', self.vm._net_task_control(task, 'cancel')['phase'])
        self.assertEqual(4, self.vm.guest_exec_status_with_timeout.call_count)

    def test_result_collection_decodes_only_matching_invocation(self):
        del self.vm._net_task_control
        task = {'task_id': 'f'*32}
        fixed = mock.Mock(hex='1'*32)
        frame = 'ZSN2|%s|finished|42|43|%s|0|Y29tcGxldGVk|' % (task['task_id'], fixed.hex)
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0, 'out-data': frame.encode('ascii')}
        with mock.patch.object(self.qga.uuid, 'uuid4', return_value=fixed):
            result = self.vm._net_task_control(task, 'collect')
        self.assertEqual(0, result['exitcode'])
        self.assertEqual(b'completed', result['out-data'])
        self.assertIn('![ZStackNetJob]::Absent($jobName)', self.vm._net_task_script(task, 'collect'))

    def test_no_matching_nonce_never_confirms_cleanup(self):
        del self.vm._net_task_control
        self.vm.guest_exec_status_with_timeout.return_value = {'exited': True, 'exitcode': 0, 'out-data': b'stale'}
        with self.assertRaises(Exception): self.vm._net_task_control({'task_id': 'f'*32}, 'cancel')

    def test_wall_clock_budget_includes_slow_status_calls(self):
        def status(pid, timeout):
            self.now += 2
            return {'exited': False}
        self.vm.guest_exec_status_with_timeout.side_effect = status
        self.run_net()
        self.vm.guest_exec_status_with_timeout.assert_not_called()

if __name__ == '__main__': unittest.main()
