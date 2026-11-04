from zstacklib.utils import linux
from zstacklib.utils import shell
import errno
import os
import signal
import subprocess

DEFAULT_KILL_TIMEOUT = 60
DEFAULT_KILL_INTERVAL = 1


def export(port, *args):
    command = ['qemu-nbd', '-p', str(port)]
    command.extend(str(arg) for arg in args)
    process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process


def find_qemu_nbd_process(pattern):
    command = "pgrep -a qemu-nbd | grep -F -- %s" % linux.shellquote(pattern)
    return shell.run(command)


def find_qemu_nbd_processes_by_export_name(export_name):
    try:
        pids = subprocess.check_output(['pgrep', '-x', 'qemu-nbd']).split()
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return []
        raise

    if not isinstance(export_name, bytes):
        export_name = export_name.encode('utf-8')
    matches = []
    for pid in pids:
        try:
            with open('/proc/%s/cmdline' % pid.decode('ascii'), 'rb') as cmdline_file:
                args = cmdline_file.read().split(b'\0')
        except (IOError, OSError):
            continue  # The process may have exited after pgrep.
        if any(args[index] == b'-x' and args[index + 1] == export_name
               for index in range(len(args) - 1)):
            matches.append(int(pid))
    return matches


def kill_nbd_process_by_export_name(export_name, timeout=DEFAULT_KILL_TIMEOUT,
                                    interval=DEFAULT_KILL_INTERVAL):
    pids = find_qemu_nbd_processes_by_export_name(export_name)
    if not pids:
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise
    if not linux.wait_callback_success(
            lambda _: not find_qemu_nbd_processes_by_export_name(export_name),
            timeout=timeout, interval=interval):
        raise Exception('timeout waiting qemu-nbd export[%s] gone after SIGTERM' % export_name)
    return 0


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
