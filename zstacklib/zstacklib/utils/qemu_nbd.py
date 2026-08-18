from zstacklib.utils import linux
from zstacklib.utils import shell
import subprocess

DEFAULT_KILL_TIMEOUT = 60
DEFAULT_KILL_INTERVAL = 1


def export(port, *args):
    command = 'qemu-nbd -p %s' % port
    if args:
        command += ' ' + ' '.join(str(arg) for arg in args)
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process


def find_qemu_nbd_process(pattern):
    command = "pgrep -a qemu-nbd | grep -F -- %s" % linux.shellquote(pattern)
    return shell.run(command)


def kill_qemu_nbd_process(pattern):
    command = ("pgrep -a qemu-nbd | grep -F -- %s | "
               "awk '{print $1}' | xargs -r kill -15") % linux.shellquote(pattern)
    return shell.run(command)


def wait_qemu_nbd_process_gone(pattern, timeout=DEFAULT_KILL_TIMEOUT, interval=DEFAULT_KILL_INTERVAL):
    def gone(_):
        return find_qemu_nbd_process(pattern) != 0

    return linux.wait_callback_success(gone, timeout=timeout, interval=interval)


def kill_nbd_process_by_flag(flag, timeout=DEFAULT_KILL_TIMEOUT, interval=DEFAULT_KILL_INTERVAL):
    ret = kill_qemu_nbd_process(flag)
    if not wait_qemu_nbd_process_gone(flag, timeout, interval):
        raise Exception('timeout waiting qemu-nbd process[%s] gone after SIGTERM' % flag)
    return ret
