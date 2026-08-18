import unittest

from zstacklib.utils import qemu_nbd


class TestQemuNbd(unittest.TestCase):
    def test_kill_nbd_waits_until_process_gone(self):
        calls = []
        original_kill = qemu_nbd.kill_qemu_nbd_process
        original_wait = qemu_nbd.wait_qemu_nbd_process_gone

        def kill(pattern):
            calls.append(('kill', pattern))
            return 0

        def wait(pattern, timeout, interval):
            calls.append(('wait', pattern, timeout, interval))
            return True

        qemu_nbd.kill_qemu_nbd_process = kill
        qemu_nbd.wait_qemu_nbd_process_gone = wait
        try:
            ret = qemu_nbd.kill_nbd_process_by_flag('/dev/vg/volume', timeout=7, interval=2)
        finally:
            qemu_nbd.kill_qemu_nbd_process = original_kill
            qemu_nbd.wait_qemu_nbd_process_gone = original_wait

        self.assertEqual(0, ret)
        self.assertEqual(('kill', '/dev/vg/volume'), calls[0])
        self.assertEqual(('wait', '/dev/vg/volume', 7, 2), calls[1])

    def test_kill_nbd_fails_when_process_still_exists(self):
        original_kill = qemu_nbd.kill_qemu_nbd_process
        original_wait = qemu_nbd.wait_qemu_nbd_process_gone

        qemu_nbd.kill_qemu_nbd_process = lambda pattern: 0
        qemu_nbd.wait_qemu_nbd_process_gone = lambda pattern, timeout, interval: False
        try:
            self.assertRaises(Exception, qemu_nbd.kill_nbd_process_by_flag, '/dev/vg/volume')
        finally:
            qemu_nbd.kill_qemu_nbd_process = original_kill
            qemu_nbd.wait_qemu_nbd_process_gone = original_wait

    def test_find_qemu_nbd_process_uses_fixed_string_match(self):
        original_run = qemu_nbd.shell.run
        commands = []

        def run(command):
            commands.append(command)
            return 1

        qemu_nbd.shell.run = run
        try:
            qemu_nbd.find_qemu_nbd_process("/dev/vg/volume'with-quote")
        finally:
            qemu_nbd.shell.run = original_run

        self.assertEqual(1, len(commands))
        self.assertTrue("grep -F --" in commands[0])
        self.assertTrue("/dev/vg/volume" in commands[0])

    def test_kill_qemu_nbd_process_uses_fixed_string_match(self):
        original_run = qemu_nbd.shell.run
        commands = []

        def run(command):
            commands.append(command)
            return 0

        qemu_nbd.shell.run = run
        try:
            qemu_nbd.kill_qemu_nbd_process("/dev/vg/volume'with-quote")
        finally:
            qemu_nbd.shell.run = original_run

        self.assertEqual(1, len(commands))
        self.assertTrue("grep -F --" in commands[0])
        self.assertTrue("xargs -r kill -15" in commands[0])
        self.assertTrue("'\\''" in commands[0])


if __name__ == "__main__":
    unittest.main()
