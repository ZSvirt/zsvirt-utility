# -*- coding: utf-8 -*-
import unittest
import re
import json
import threading

try:
    from unittest import mock
except ImportError:
    import mock

from zstacklib.utils import qmp

# ------------------------------------------------

test_qmp_command1 = '{"execute": "object-add", "arguments":{ "qom-type": "colo-compare", "id": "comp","props": { "primary_in": ' \
                    '"primary-in-c","secondary_in": "secondary-in-s","outdev":"primary-out-c", "iothread": "iothread", "vnet_hdr_support": true } } }'

# ------------------------------------------------
except_qmp_command1 = '{"execute": "object-add", "arguments": {"vnet_hdr_support": true, "iothread": "iothread", "secondary_in": ' \
                      '"secondary-in-s", "primary_in": "primary-in-c", "id": "comp", "qom-type": "colo-compare", "outdev": "primary-out-c"}}'

# ------------------------------------------------
test_qmp_command2 = '{"execute": "object-add", "arguments":{ "qom-type": "filter-mirror", "id": "fm-%s", "props": { "netdev": "hostnet%s",' \
                    ' "queue": "tx", "outdev": "zs-mirror-%s", "vnet_hdr_support": true} } }'


# ------------------------------------------------


class Test(unittest.TestCase):
    def setUp(self):
        self.qemu_version = mock.patch.object(
            qmp, "QEMU_VERSION", "6.2.0")
        self.qemu_version.start()
        self.addCleanup(self.qemu_version.stop)

    def test_function_is_bad_vm_root_volume(self):
        def remove_command_props_parameter(cmd):
            if re.match(r'.*object-add.*arguments.*props.*', cmd):
                j_cmd = json.loads(cmd)
                props = j_cmd.get("arguments").get("props")
                j_cmd.get("arguments").pop("props")
                j_cmd.get("arguments").update(props)
                cmd = json.dumps(j_cmd)
            return cmd

        assert remove_command_props_parameter(test_qmp_command1) == except_qmp_command1
        assert qmp.qmp_subcmd("6.2.0", test_qmp_command2) == remove_command_props_parameter(test_qmp_command2)

    def test_execute_qmp_uses_argv_without_shell_interpolation(self):
        command = json.dumps({
            "execute": "drive-mirror",
            "arguments": {"target": "nbd://host:10809/vol-a';touch-pwned;#"}
        })
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b'{"return": {}}', b'')

        with mock.patch.object(qmp.shell, "get_process", return_value=process) as get_process:
            self.assertEqual({}, qmp._execute_qmp_command("vm-uuid", command))

        args, kwargs = get_process.call_args
        self.assertEqual(
            ["virsh", "qemu-monitor-command", "vm-uuid", command], args[0])
        self.assertIs(False, kwargs["shell"])
        self.assertIs(True, kwargs["pipe"])

    def test_execute_qmp_timeout_kills_stuck_virsh(self):
        timers = []

        class ControllableTimer(object):
            def __init__(self, delay, function):
                self.delay = delay
                self.function = function
                self.daemon = False
                self.cancelled = False
                self.start_called = False
                self.worker = None
                timers.append(self)

            def start(self):
                self.start_called = True
                self.worker = threading.Thread(target=self.function)
                self.worker.daemon = True
                self.worker.start()

            def cancel(self):
                self.cancelled = True

            def join(self):
                self.worker.join()

        class StuckProcess(object):
            def __init__(self):
                self.returncode = None
                self.killed = False
                self.kill_event = threading.Event()

            def communicate(self):
                if not self.kill_event.wait(1):
                    raise AssertionError("communicate returned before timeout killed the process")
                return b'', b''

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.kill_event.set()

        process = StuckProcess()
        with mock.patch.object(qmp.shell, "get_process",
                               return_value=process), \
             mock.patch.object(qmp.threading, "Timer", ControllableTimer):
            with self.assertRaises(qmp.QMPTimeoutError):
                qmp._execute_qmp_command(
                    "vm-uuid", '{"execute":"query-block-jobs"}',
                    command_timeout=2)

        self.assertTrue(process.killed)
        self.assertEqual(2.0, timers[0].delay)
        self.assertTrue(timers[0].start_called)
        self.assertTrue(timers[0].cancelled)

    def test_completed_process_is_not_marked_timed_out_by_late_callback(self):
        class LateTimer(object):
            def __init__(self, unused_delay, function):
                self.function = function
                self.daemon = False

            def start(self):
                pass

            def cancel(self):
                # Model a timer callback that has already been dispatched and
                # races with cancellation after communicate() completed.
                self.function()

        class CompletedProcess(object):
            def __init__(self):
                self.returncode = None
                self.kill_called = False

            def communicate(self):
                self.returncode = 0
                return b'complete', b''

            def poll(self):
                return self.returncode

            def kill(self):
                self.kill_called = True
                raise OSError("process already exited")

        process = CompletedProcess()
        with mock.patch.object(qmp.threading, "Timer", LateTimer):
            output, error, timed_out = qmp._communicate_with_timeout(
                process, 2)

        self.assertEqual(b'complete', output)
        self.assertEqual(b'', error)
        self.assertFalse(timed_out)
        self.assertFalse(process.kill_called)

    def test_process_exiting_between_poll_and_kill_is_not_marked_timed_out(self):
        class LateTimer(object):
            def __init__(self, unused_delay, function):
                self.function = function
                self.daemon = False

            def start(self):
                pass

            def cancel(self):
                self.function()

        class RacingProcess(object):
            def __init__(self):
                self.returncode = None
                self.kill_called = False

            def communicate(self):
                return b'complete', b''

            def poll(self):
                # The process exits normally immediately after poll observes
                # it as running, before the timeout callback can kill it.
                self.returncode = 0
                return None

            def kill(self):
                self.kill_called = True
                raise OSError("process already exited")

        process = RacingProcess()
        with mock.patch.object(qmp.threading, "Timer", LateTimer):
            output, error, timed_out = qmp._communicate_with_timeout(
                process, 2)

        self.assertEqual(b'complete', output)
        self.assertEqual(b'', error)
        self.assertFalse(timed_out)
        self.assertTrue(process.kill_called)
        self.assertEqual(0, process.returncode)

    def test_execute_qmp_rejects_invalid_timeout_before_spawning_virsh(self):
        invalid_timeouts = [0, -1, "invalid", float("nan"), float("inf")]
        for command_timeout in invalid_timeouts:
            with mock.patch.object(qmp.shell, "get_process") as get_process:
                with self.assertRaises(ValueError):
                    qmp._execute_qmp_command(
                        "vm-uuid", '{"execute":"query-block-jobs"}',
                        command_timeout=command_timeout)
            get_process.assert_not_called()

    def test_execute_qmp_reaps_virsh_when_timer_start_fails(self):
        class FailingTimer(object):
            def __init__(self, delay, function):
                self.daemon = False

            def start(self):
                raise RuntimeError("timer unavailable")

        process = mock.Mock()
        process.communicate.return_value = (b'', b'')
        with mock.patch.object(qmp.shell, "get_process",
                               return_value=process), \
             mock.patch.object(qmp.threading, "Timer", FailingTimer):
            with self.assertRaises(RuntimeError):
                qmp._execute_qmp_command(
                    "vm-uuid", '{"execute":"query-block-jobs"}',
                    command_timeout=2)

        process.kill.assert_called_once_with()
        process.communicate.assert_called_once_with()

    def test_execute_qmp_keeps_process_timeout_out_of_qmp_arguments(self):
        with mock.patch.object(
                qmp, "_execute_qmp_command", return_value={}) as execute:
            qmp.execute_qmp_command(
                "vm-uuid", "query-block-jobs",
                command_timeout=3, sample_value=7)

        command = json.loads(execute.call_args[0][1].decode("utf-8"))
        self.assertEqual({"sample-value": 7}, command["arguments"])
        self.assertEqual(3, execute.call_args[1]["command_timeout"])

    def test_execute_qmp_raw_accepts_utf8_bytes(self):
        command = u'{"execute":"human-monitor-command","arguments":{"command-line":"info 测试"}}'

        with mock.patch.object(
                qmp, "_execute_qmp_command", return_value={}) as execute:
            qmp.execute_qmp_command_raw(
                "vm-uuid", command.encode("utf-8"))

        self.assertEqual(command, execute.call_args[0][1])

    def test_execute_qmp_raw_decodes_bytes_on_python2_branch(self):
        command = u'{"execute":"human-monitor-command","arguments":{"command-line":"info \u6d4b\u8bd5"}}'

        with mock.patch.object(qmp.sys, "version_info", (2, 7, 18)), \
             mock.patch.object(qmp, "unicode", str, create=True), \
             mock.patch.object(
                 qmp, "_execute_qmp_command", return_value={}) as execute:
            qmp.execute_qmp_command_raw(
                "vm-uuid", command.encode("utf-8"))

        self.assertEqual(command, execute.call_args[0][1])

    def test_execute_qmp_raw_preserves_unicode_text(self):
        command = u'{"execute":"human-monitor-command","arguments":{"command-line":"info 测试"}}'

        with mock.patch.object(
                qmp, "_execute_qmp_command", return_value={}) as execute:
            qmp.execute_qmp_command_raw("vm-uuid", command)

        self.assertIs(command, execute.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
