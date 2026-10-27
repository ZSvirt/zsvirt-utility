# -*- coding: utf-8 -*-
"""Ambiguous submission is not equivalent to a rejected/unstarted task."""
import unittest
import test_qga_guest_exec_program as loader
mock = loader.mock

class SubmissionTest(unittest.TestCase):
    def setUp(self):
        self.before = loader.sys.modules.copy()
        self.qga = loader._load_qga_module()
        self.qga.log.mask_sensitive_field = lambda obj, value: '<masked>'
        self.vm = self.qga.VmQga.__new__(self.qga.VmQga)
        self.vm.vm_uuid = 'vm-a'
        self.vm.ZS_TOOLS_PATN_WIN = r'C:\Program Files\GuestTools\zs-tools\zs-tools.exe'
        self.now = 100.0
        self.patches = [mock.patch.object(self.qga.time, 'time', side_effect=lambda: self.now),
                        mock.patch.object(self.qga.time, 'sleep', side_effect=self.sleep)]
        for patch in self.patches: patch.start()
        self.records = {}
        self.launches = []
        self.drop_response = True
        self.delay_launch = False
        self.finished = False
        self.control_failed = False
        self.prepare_failed = False
        self.vm._net_task_control = mock.Mock(side_effect=self.control)
        self.vm.guest_exec = mock.Mock(side_effect=self.launch)
        self.vm.guest_exec_status_with_timeout = mock.Mock(side_effect=lambda pid, timeout: {'exited': self.finished, 'exitcode': 0})

    def sleep(self, seconds): self.now += seconds

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()
        loader._restore_sys_modules(self.before)

    def control(self, task, action, deadline=None):
        if self.control_failed or (action == 'prepare' and self.prepare_failed):
            raise RuntimeError('channel unavailable')
        token = task['task_id']
        if action == 'prepare':
            self.records[token] = {'phase': 'reserved', 'wrapper_pid': 0, 'pid': 0}
        record = self.records[token]
        if action == 'inspect' and self.finished: record['phase'] = 'finished'
        if action == 'collect':
            if not self.finished: raise RuntimeError('Job still active')
            return dict(record, exitcode=0)
        if action in ('seal', 'cancel') and record['phase'] != 'reserved' and not self.finished:
            raise RuntimeError('Net task Job is still active')
        if action == 'seal' or (action == 'cancel' and (record['phase'] == 'reserved' or self.finished)):
            record['phase'] = 'cancelled'
        return record.copy()

    def launch(self, args, timeout=None):
        task = self.qga._zs_tools_net_pending['vm-a']
        self.assertEqual('unknown', task['phase'])
        token = task['task_id']
        self.launches.append((token, args))
        if not self.delay_launch:
            self.records[token] = {'phase': 'running', 'wrapper_pid': 101, 'pid': 202}
        if self.drop_response: raise RuntimeError('accepted but PID response lost')
        return {'pid': 101}

    def run_net(self): return self.vm.guest_exec_zs_tools('net', '{}', wait=1, retry=3)

    def test_accepted_lost_pid_response_blocks_three_cross_instance_retries(self):
        code, message = self.run_net()
        self.assertEqual(1, code)
        self.assertIn('submission result unknown', message)
        task = self.qga._zs_tools_net_pending['vm-a']
        self.assertEqual('unknown', task['phase'])
        for unused in range(3):
            other = self.qga.VmQga.__new__(self.qga.VmQga)
            other.__dict__.update(self.vm.__dict__)
            self.assertEqual(1, other.guest_exec_zs_tools('net', '{}', wait=1, retry=3)[0])
            self.assertIs(task, self.qga._zs_tools_net_pending['vm-a'])
        self.assertEqual(1, len(self.launches))
        self.finished = True
        self.records[task['task_id']]['phase'] = 'finished'
        self.drop_response = False
        self.assertEqual((0, ''), self.run_net())
        self.assertEqual(2, len(self.launches))
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)

    def test_unclaimed_submission_is_sealed_before_delayed_launcher(self):
        self.delay_launch = True
        self.run_net()
        task = self.qga._zs_tools_net_pending['vm-a']
        self.vm._recover_net_task(task)
        self.assertEqual('cancelled', self.records[task['task_id']]['phase'])
        # This is the same condition enforced by the Guest launcher under lock.
        self.assertNotEqual('reserved', self.records[task['task_id']]['phase'])
        script = self.vm._net_task_script(task, 'launch', '{}')
        self.assertLess(script.index("$record[2] -ne 'reserved'"), script.index('Start-Process -FilePath'))
        self.assertLess(script.index("Write-Record 'running'"), script.index('$locked.Dispose()'))

    def test_definitely_unsent_prepare_failure_does_not_leave_pending(self):
        self.prepare_failed = True
        self.assertEqual(1, self.run_net()[0])
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)
        self.vm.guest_exec.assert_not_called()

    def test_unknown_receipt_or_channel_failure_keeps_blocking(self):
        self.run_net()
        self.control_failed = True
        for unused in range(3): self.assertEqual(1, self.run_net()[0])
        self.assertEqual(1, len(self.launches))
        self.assertIn('vm-a', self.qga._zs_tools_net_pending)

    def test_acknowledged_launch_keeps_pending_until_job_absence(self):
        self.drop_response = False
        self.assertEqual(1, self.run_net()[0])
        task = self.qga._zs_tools_net_pending['vm-a']
        self.assertEqual('submitted', task['phase'])
        self.finished = True
        self.assertEqual((0, ''), self.run_net())
        self.assertEqual(2, len(self.launches))
        self.assertNotIn('vm-a', self.qga._zs_tools_net_pending)

    def test_accepted_response_without_pid_still_retains_unknown(self):
        self.drop_response = False
        def no_pid(args, timeout=None):
            self.launch(args)
            return {}
        self.vm.guest_exec.side_effect = no_pid
        self.assertEqual(1, self.run_net()[0])
        self.assertEqual('unknown', self.qga._zs_tools_net_pending['vm-a']['phase'])
        self.assertEqual(1, self.run_net()[0])
        self.assertEqual(1, len(self.launches))

    def test_scripts_preserve_ps2_compatible_operations(self):
        task = {'task_id': 'a' * 32}
        for action in ('prepare', 'inspect', 'cancel', 'seal', 'launch'):
            script = self.vm._net_task_script(task, action, '{}')
            self.assertNotIn('ConvertTo-Json', script)
            self.assertNotIn('[PSCustomObject]', script)
            self.assertIn('[IO.FileShare]::None', script)

if __name__ == '__main__': unittest.main()
