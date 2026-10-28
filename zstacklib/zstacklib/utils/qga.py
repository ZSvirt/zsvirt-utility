import base64
import json
import time
import threading
import uuid
import subprocess

import libvirt
import libvirt_qemu
import xml.etree.ElementTree as ET

import log

logger = log.get_logger(__name__)

"""
VM_OS_LINUX_DEBIAN = "debian"
VM_OS_LINUX_KYLIN = "kylin"
VM_OS_LINUX_UOS = "uos"
VM_OS_WINDOWS = "mswindows"
VM_OS_LINUX_SUPPORT = [VM_OS_LINUX_UBUNTU, VM_OS_LINUX_CENTOS, VM_OS_LINUX_DEBIAN, VM_OS_LINUX_KYLIN, VM_OS_LINUX_UOS]

ERROR_CODE_VM_NOT_RUNNING = 'VM_NOT_RUNNING'
ERROR_CODE_VM_CONFIG_IPV6_NOT_SUPPORT = 'VM_CONFIG_IPV6_NOT_SUPPORT'
ERROR_CODE_VM_CONFIG_NOT_EFFECTIVE_IP = 'VM_CONFIG_NOT_EFFECTIVE_NET_IP'
ERROR_CODE_VM_CONFIG_NOT_EFFECTIVE_GW = 'VM_CONFIG_NOT_EFFECTIVE_NET_GW'
ERROR_CODE_VM_CONFIG_NOT_EFFECTIVE_MTU = 'VM_CONFIG_NOT_EFFECTIVE_NET_MTU'
ERROR_CODE_VM_CONFIG_NOT_EFFECTIVE_DNS = 'VM_CONFIG_NOT_EFFECTIVE_NET_DNS'
ERROR_CODE_VM_CONFIG_PERSISTENCE_FAILED = 'VM_CONFIG_PERSISTENCE_FAILED'
ERROR_CODE_VM_CONFIG_INTERNAL = 'VM_CONFIG_INTERNAL'
ERROR_CODE_QGA_NOT_RUNNING = 'QGA_NOT_RUNNING'
ERROR_CODE_QGA_OS_NOT_SUPPORT = 'QGA_OS_NOT_SUPPORT'
ERROR_CODE_QGA_COMMAND_IS_DISABLED = 'QGA_COMMAND_IS_DISABLED'
ERROR_CODE_QGA_VERSION_TOO_LOWER = 'QGA_VERSION_TOO_LOWER'
ERROR_CODE_QGA_COMMAND_ERROR = 'QGA_COMMAND_EXEC_ERROR'
ERROR_CODE_QGA_RETURN_VALUE_ERROR = 'QGA_RETURN_VALUE_ERROR'

VM_CONFIG_SYNC_OS_VERSION_SUPPORT = {
    # VM_OS_LINUX_UBUNTU: ("16", "18", "20", "21", "22"),
    # VM_OS_LINUX_CENTOS: ("6", "7", "8"),
    VM_OS_LINUX_KYLIN: ("v10",),
    VM_OS_LINUX_UOS: ("20",)
}
"""
# qga command wait 30 seconds
qga_exec_wait_interval = 1
qga_exec_wait_retry = 120
qga_command_timeout = 3
qga_channel_state_connected = 'connected'
qga_channel_state_disconnected = 'disconnected'

encodings = ['utf-8', 'GB2312', 'ISO-8859-1']


def decode_with_fallback(encoded_bytes):
    for encoding in encodings:
        try:
            return encoded_bytes.decode(encoding).encode('utf-8')
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("Unable to decode bytes using provided encodings")


def get_qga_channel_state(vm_dom):
    xml_tree = ET.fromstring(vm_dom.XMLDesc())
    channel = xml_tree.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    if channel is not None:
        return channel.get('state')
    else:
        return None


def is_qga_connected(vm_dom):
    try:
        return True if get_qga_channel_state(vm_dom) == qga_channel_state_connected else False
    except:
        return False


# windows zs-tools command wait 120s
zs_tools_wait_retry = 120
# Preserve unconfirmed net executions across VmQga instances. This protects
# older guests too; the Guest mutex additionally survives Agent restarts.
_zs_tools_net_pending = {}
_zs_tools_net_lock = threading.RLock()


class QgaException(Exception):
    """ The base exception class for all exceptions this agent raises."""

    def __init__(self, error_code, msg=None):
        self.error_code = error_code
        super(QgaException, self).__init__('QGA exception' if msg is None else msg)


class VmQga(object):
    QGA_STATE_RUNNING = "Running"
    QGA_STATE_NOT_RUNNING = "NotRunning"

    VM_OS_LINUX_KYLIN = "kylin"
    VM_OS_LINUX_NEO_KYLIN = "neokylin"
    VM_OS_LINUX_UOS = "uos"
    VM_OS_LINUX_UBUNTU = "ubuntu"
    VM_OS_LINUX_CENTOS = "centos"
    VM_OS_LINUX_OPEN_SUSE = "opensuse-leap"
    VM_OS_LINUX_SUSE_S = "sles"
    VM_OS_LINUX_SUSE_D = "sled"
    VM_OS_LINUX_ORACLE = "ol"
    VM_OS_LINUX_REDHAT = "rhel"
    VM_OS_LINUX_ANOLIS = "anolis"
    VM_OS_LINUX_DEBIAN = "debian"
    VM_OS_LINUX_FEDORA = "fedora"
    VM_OS_LINUX_OPENEULER = "openeuler"
    VM_OS_LINUX_ROCKY = "rocky"
    VM_OS_LINUX_ALMALINUX = "almalinux"
    VM_OS_WINDOWS = "mswindows"

    ZS_TOOLS_PATN_WIN = r"C:\Program Files\GuestTools\zs-tools\zs-tools.exe"
    ZS_TOOLS_VERSION_PATN_WIN = r"C:\Program Files\Common Files\GuestTools\VERSION"

    def __init__(self, domain):
        self.domain = domain
        self.vm_uuid = domain.name()
        self.state = self.QGA_STATE_NOT_RUNNING
        self.version = None
        self.supported_commands = {}
        self.os = None
        self.os_version = None
        self.os_id_like = None
        self.qga_init()
        # self.qga_debug()

    '''
    def qga_debug(self):
        logger.debug('qga info: vm_uuid: %s\n state:%s\n version:%s\n os:%s\n os_version:%s\n supported_commands:%s' \
                     % (self.vm_uuid, self.state, self.version, self.os, self.os_version, self.supported_commands))
    '''

    def call_qga_command(self, command, args=None, timeout=qga_command_timeout):
        """
        Execute QEMU-GA command and return result as dict or None on error

        command   the command to execute (string)
        args      arguments to the command (dict) or None
        :rtype: dict
        """
        if self.supported_commands:
            if command not in self.supported_commands:
                raise Exception('qga command {} not support, qga version is {}'.format(command, self.version))
            if not self.supported_commands[command]:
                raise Exception('qga command {} has been disabled'.format(command))

        cmd = {'execute': command}
        if args:
            if 'buf-b64' in args:
                args['buf-b64'] = base64.b64encode(args['buf-b64'])
            cmd['arguments'] = args
        cmd = json.dumps(cmd)
        try:
            ret = libvirt_qemu.qemuAgentCommand(self.domain, cmd,
                                                timeout, 0)
        except libvirt.libvirtError as e:
            message = 'exec qga command[{}] args[{}] error: {}'.format(cmd, args, e.message)
            raise Exception(message)

        try:
            # run qga command log has too many to influence whole log file, hide it for clean log
            # logger.debug("vm {} run qga command {}".format(self.vm_uuid, cmd))
            
            parsed = json.loads(ret)
        except ValueError:
            raise Exception('qga command return value parsing error:{}'.format(ret))

        if 'return' not in parsed:
            raise Exception('qga command return value format error:{}'.format(ret))

        parsedRet = parsed['return']
        if isinstance(parsedRet, dict):
            if 'out-data' in parsedRet:
                parsedRet['out-data'] = base64.b64decode(parsedRet['out-data'])
            if 'err-data' in parsedRet:
                parsedRet['err-data'] = base64.b64decode(parsedRet['err-data'])
            if 'buf-b64' in parsedRet:
                parsedRet['buf-b64'] = base64.b64decode(parsedRet['buf-b64'])

        return parsedRet

    def guest_exec_status(self, pid):
        return self.guest_exec_status_with_timeout(pid, qga_command_timeout)

    def guest_exec_status_with_timeout(self, pid, timeout):
        ret = self.call_qga_command("guest-exec-status", args={'pid': pid}, timeout=timeout)
        if not ret or 'exited' not in ret:
            raise Exception('guest-exec-status exception')
        return ret

    def guest_exec(self, args, timeout=None):
        if timeout is None:
            return self.call_qga_command("guest-exec", args=args)
        return self.call_qga_command("guest-exec", args=args, timeout=timeout)

    def guest_exec_program(self, path, args=None, output=True, wait=qga_exec_wait_interval, retry=qga_exec_wait_retry):
        if args is None:
            args = []
        ret = self.guest_exec(
            {"path": path, "arg": args, "capture-output": output})
        if ret and "pid" in ret:
            pid = ret["pid"]
        else:
            raise Exception('qga exec program {} failed for vm {}'.format(path, self.vm_uuid))

        if not output:
            logger.warn("run qga program: {} failed, no output".format(path))
            return 0, None

        ret = None
        for i in range(retry):
            time.sleep(wait)
            ret = self.guest_exec_status(pid)
            if ret['exited']:
                break

        if not ret or not ret.get('exited'):
            raise Exception('qga exec program {} timeout for vm {}'.format(path, self.vm_uuid))

        exit_code = ret.get('exitcode')
        ret_data = None
        if 'out-data' in ret:
            ret_data = decode_with_fallback(ret['out-data'])
        elif 'err-data' in ret:
            ret_data = decode_with_fallback(ret['err-data'])

        return exit_code, ret_data

    def guest_exec_program_no_exitcode(self, path, args=None, exception=True, output=True):
        exitcode, ret_data = self.guest_exec_program(path, args, output)
        if exitcode != 0:
            if exception:
                raise Exception('program {}, args {}, exitcode {}, ret {}'.format(path, args, exitcode, ret_data))
            return None
        return ret_data

    def guest_exec_bash_no_exitcode(self, cmd, exception=True, output=True):
        exitcode, ret_data = self.guest_exec_bash(cmd, output)
        if exitcode != 0:
            # logger.debug("qga exec command: {}, exitcode {}, ret {}".format(cmd, exitcode, ret_data))
            if exception:
                raise Exception('cmd {}, exitcode {}, ret {}'
                                .format(cmd, exitcode, ret_data))
            return None
        return ret_data

    def guest_exec_bash(self, cmd, output=True, wait=qga_exec_wait_interval, retry=qga_exec_wait_retry):

        ret = self.guest_exec(
            {"path": "bash", "arg": ["-c", cmd], "capture-output": output})
        if ret and "pid" in ret:
            pid = ret["pid"]
        else:
            raise Exception('qga exec cmd {} failed for vm {}'.format(cmd, self.vm_uuid))

        if not output:
            logger.warn("run qga bash: {} failed, no output".format(cmd))
            return 0, None

        ret = None
        for i in range(retry):
            time.sleep(wait)
            ret = self.guest_exec_status(pid)
            # format: {"return":{"exited":false}}
            if ret['exited']:
                break

        if not ret or not ret.get('exited'):
            raise Exception('qga exec cmd {} timeout for vm {}'.format(cmd, self.vm_uuid))

        exit_code = ret.get('exitcode')
        ret_data = None
        if 'out-data' in ret:
            ret_data = decode_with_fallback(ret['out-data'])
        elif 'err-data' in ret:
            ret_data = decode_with_fallback(ret['err-data'])

        return exit_code, ret_data

    # not a good function, just for hurry push
    def guest_exec_python(self, file, params=None, output=True, wait=qga_exec_wait_interval, retry=qga_exec_wait_retry):
        path = self.guest_exec_bash_no_exitcode("which python2", exception=False)
        if not path:
            path = self.guest_exec_bash_no_exitcode("which python3", exception=False)

        if not path:
            raise Exception('python not installed in vm {}'.format(file, self.vm_uuid))

        args = [file]
        if params is not None:
            for d in params:
                args.append(d)

        ret = self.guest_exec(
            {"path": path.strip(), "arg": args, "capture-output": output})
        if ret and "pid" in ret:
            pid = ret["pid"]
        else:
            raise Exception('qga exec cmd {} failed for vm {}'.format(file, self.vm_uuid))

        if not output:
            logger.warn("run qga python: {} failed, no output".format(file))
            return 0, None

        ret = None
        for i in range(retry):
            time.sleep(wait)
            ret = self.guest_exec_status(pid)
            # format: {"return":{"exited":false}}
            if ret['exited']:
                break

        if not ret or not ret.get('exited'):
            raise Exception('qga exec cmd {} timeout for vm {}'.format(file, self.vm_uuid))

        exit_code = ret.get('exitcode')
        ret_data = None
        if 'out-data' in ret:
            ret_data = decode_with_fallback(ret['out-data'])
        elif 'err-data' in ret:
            ret_data = decode_with_fallback(ret['err-data'])

        return exit_code, ret_data

    def _net_task_script(self, task, action, config=None):
        # ZSN2 binds every launched tree to a unique, non-inheritable Job handle.
        # All state transitions and cancellation use the same exclusive lock.
        script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$taskId = '%s'
$recordPath = 'C:\ProgramData\ZStack\zs-tools-net\' + $taskId + '.state'
$lockPath = $recordPath + '.lock'
function Lock-Record {
    return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
function Read-Record {
    $parts = [IO.File]::ReadAllText($recordPath).Trim().Split('|')
    if ($parts.Length -ne 5 -or $parts[0] -ne 'ZSN2' -or
        $parts[1] -ne $taskId -or
        @('reserved','starting','running','finished','failed','cancelled') -notcontains $parts[2] -or
        $parts[3] -notmatch '^\d+$' -or $parts[4] -notmatch '^\d+$') {
        throw 'Invalid net task receipt'
    }
    return $parts
}
function Write-Record($state, $wrapperPid, $childPid) {
    $text = 'ZSN2|{0}|{1}|{2}|{3}' -f $taskId,$state,$wrapperPid,$childPid
    [IO.File]::WriteAllText($recordPath, $text, [Text.Encoding]::ASCII)
}
""" % task['task_id']
        script += r"""
$jobName = 'Global\ZStackNetTask_' + $taskId
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class ZStackNetJob {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr info, uint length);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, IntPtr info, uint length, IntPtr result);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    public static IntPtr Enter(string name) {
        IntPtr job = CreateJobObject(IntPtr.Zero, name);
        int error = Marshal.GetLastWin32Error();
        if (job == IntPtr.Zero) throw new Win32Exception(error);
        if (error == 183) { CloseHandle(job); throw new InvalidOperationException("Duplicate job"); }
        int size = IntPtr.Size == 8 ? 144 : 112;
        IntPtr info = Marshal.AllocHGlobal(size);
        try {
            for (int i=0; i<size; i++) Marshal.WriteByte(info, i, 0);
            Marshal.WriteInt32(info, 16, 0x2000); // KILL_ON_JOB_CLOSE; no breakaway
            if (!SetInformationJobObject(job, 9, info, (uint)size) ||
                !AssignProcessToJobObject(job, GetCurrentProcess()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return job;
        } catch { CloseHandle(job); throw; }
        finally { Marshal.FreeHGlobal(info); }
    }
    public static uint Active(IntPtr job) {
        IntPtr info = Marshal.AllocHGlobal(48);
        try {
            if (!QueryInformationJobObject(job, 1, info, 48, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return (uint)Marshal.ReadInt32(info, 40);
        } finally { Marshal.FreeHGlobal(info); }
    }
    public static bool Absent(string name) {
        // QUERY only: the observer cannot cancel or rebind a process by PID.
        IntPtr job = OpenJobObject(4, false, name);
        if (job == IntPtr.Zero) {
            int error = Marshal.GetLastWin32Error();
            if (error == 2) return true;
            throw new Win32Exception(error);
        }
        try { Active(job); return false; }
        finally { CloseHandle(job); }
    }
    public static void Stop(IntPtr job) {
        // The wrapper is also a member. Successful termination does not return.
        if (!TerminateJobObject(job, 1))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
"""

        if action == 'launch':
            arguments = subprocess.list2cmdline(['net', '--config', config])
            script += r"""
$locked = $null
$job = [IntPtr]::Zero
try {
    $locked = Lock-Record
    $record = Read-Record
    if ($record[2] -ne 'reserved') { throw 'Net task is sealed or already claimed' }
    # Join BEFORE creating the child. Even a crash in Start-Process cannot
    # leave an unbound child; normal children automatically inherit this Job.
    $job = [ZStackNetJob]::Enter($jobName)
    [Environment]::SetEnvironmentVariable('ZS_TOOLS_NET_JOB', $taskId, 'Process')
    Write-Record 'starting' $PID 0
    $stdoutPath = $recordPath + '.out'
    $stderrPath = $recordPath + '.err'
    $process = Start-Process -FilePath '%s' -ArgumentList '%s' -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $processHandle = $process.Handle
    Write-Record 'running' $PID $process.Id
    $locked.Dispose(); $locked = $null
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while (!$process.WaitForExit(100)) {
        if ([IO.File]::Exists($recordPath + '.cancel') -or $watch.Elapsed.TotalSeconds -ge %d) {
            [ZStackNetJob]::Stop($job)
        }
    }
    # Keep the original root handle until the complete Job is checked.
    # A root exit is not proof that its children stopped.
    $drain = [Diagnostics.Stopwatch]::StartNew()
    while ([ZStackNetJob]::Active($job) -gt 1 -and $drain.Elapsed.TotalSeconds -lt 3) { Start-Sleep -Milliseconds 50 }
    if ([ZStackNetJob]::Active($job) -ne 1) { [ZStackNetJob]::Stop($job) }
    $code = $process.ExitCode
    $locked = Lock-Record
    [IO.File]::WriteAllText($recordPath + '.result', [string]$code, [Text.Encoding]::ASCII)
    Write-Record 'finished' $PID $process.Id
    $locked.Dispose(); $locked = $null
    [Console]::Out.Write([IO.File]::ReadAllText($stdoutPath))
    [Console]::Error.Write([IO.File]::ReadAllText($stderrPath))
    $process.Dispose()
    # Do not explicitly close our Job while alive: that would kill this wrapper
    # before it can return the exit code. OS exit closes its sole owning handle.
    exit $code
} catch {
    [Console]::Error.WriteLine('Net task launcher failed')
    if ($job -ne [IntPtr]::Zero) { [ZStackNetJob]::Stop($job) }
    if ($locked -ne $null -and $record -ne $null -and $record[2] -eq 'reserved') { Write-Record 'failed' $PID 0 }
    exit 1
} finally {
    if ($locked -ne $null) { $locked.Dispose() }
}
""" % (self.ZS_TOOLS_PATN_WIN.replace("'", "''"), arguments.replace("'", "''"),
       max(1, int(task.get('budget', 105))))
        else:
            if action not in ('prepare', 'inspect', 'cancel', 'seal', 'collect'):
                raise ValueError('invalid net task action')
            script += r"""
$action = '%s'
if ($action -eq 'prepare') { [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($recordPath)) | Out-Null }
$locked = $null
try {
    $locked = Lock-Record
    if ($action -eq 'prepare') {
        if ([IO.File]::Exists($recordPath)) { throw 'Duplicate net task ID' }
        Write-Record 'reserved' 0 0
    }
    $record = Read-Record
    if ($action -eq 'cancel' -or $action -eq 'seal') {
        if ($record[2] -eq 'reserved') {
            Write-Record 'cancelled' $record[3] $record[4]
        } else {
            # Signal a task, never a PID. Only the wrapper owns a terminating
            # handle. Missing Job means its complete bound tree has ended.
            [IO.File]::WriteAllText($recordPath + '.cancel', 'cancel')
            if ([ZStackNetJob]::Absent($jobName)) {
                Write-Record 'cancelled' $record[3] $record[4]
            } else { throw 'Net task Job is still active' }
        }
        $record = Read-Record
    }
    if ($action -eq 'inspect' -and @('starting','running') -contains $record[2] -and
        [ZStackNetJob]::Absent($jobName)) {
        Write-Record 'failed' $record[3] $record[4]
        $record = Read-Record
    }
    $reply = ($record -join '|') + '|%s'
    if ($action -eq 'collect') {
        if ($record[2] -ne 'finished' -or ![ZStackNetJob]::Absent($jobName)) {
            throw 'Net task result is not ready'
        }
        $code = [IO.File]::ReadAllText($recordPath + '.result').Trim()
        if ($code -notmatch '^-?\d+$') { throw 'Invalid net task result' }
        $stdout = [Convert]::ToBase64String([IO.File]::ReadAllBytes($recordPath + '.out'))
        $stderr = [Convert]::ToBase64String([IO.File]::ReadAllBytes($recordPath + '.err'))
        $reply = $reply + '|' + $code + '|' + $stdout + '|' + $stderr
    }
    [Console]::Out.WriteLine($reply)
} catch {
    [Console]::Out.WriteLine('ZSN2|%s|error|0|0|%s')
    exit 1
} finally { if ($locked -ne $null) { $locked.Dispose() } }
""" % (action, task.get('control_id', 'source-test'), task['task_id'], task.get('control_id', 'source-test'))
        return script

    def _net_task_launch_args(self, task, config):
        encoded = base64.b64encode(self._net_task_script(task, 'launch', config).encode('utf-16le'))
        if not isinstance(encoded, str):
            encoded = encoded.decode('ascii')
        return {'path': r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                'arg': ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                'capture-output': True}

    @staticmethod
    def _net_task_timeout(deadline, limit):
        # libvirt takes whole seconds. Never round a fractional remainder up
        # or pass zero (which has special timeout semantics).
        remaining = int(deadline - time.time())
        if remaining < 1:
            raise Exception('net task deadline exhausted')
        return min(limit, remaining)

    def _net_task_control(self, task, action, deadline=None):
        # Prepare/cancel have their own bounded control budget. Execution
        # inspect/collect inherit the caller's absolute deadline unchanged.
        if deadline is None:
            deadline = time.time() + 10
        request = dict(task, control_id=uuid.uuid4().hex)
        script = self._net_task_script(request, action)
        encoded = base64.b64encode(script.encode('utf-16le'))
        if not isinstance(encoded, str):
            encoded = encoded.decode('ascii')
        result = self.guest_exec({
            'path': r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
            'arg': ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            'capture-output': True}, timeout=self._net_task_timeout(deadline, 3))
        if not result or 'pid' not in result:
            raise Exception('net task control did not return a PID')
        while time.time() < deadline:
            try:
                status = self.guest_exec_status_with_timeout(
                    result['pid'], self._net_task_timeout(deadline, 2))
            except Exception:
                time.sleep(min(0.2, max(0, deadline - time.time())))
                continue
            if time.time() >= deadline:
                break
            if status.get('exited'):
                data = status.get('out-data', b'')
                if not isinstance(data, str):
                    data = data.decode('ascii', 'replace')
                fields = data.strip().split('|')
                # QGA may retain an exited record after releasing its process
                # handle. A reused PID can return another invocation's result.
                # Consume stale replies, but accept only this random nonce.
                if (len(fields) in (6, 9) and
                        fields[:2] == ['ZSN2', task['task_id']] and
                        fields[5] == request['control_id']):
                    if fields[2] == 'error' or status.get('exitcode') != 0:
                        raise Exception('net task control failed or is not ready')
                    if (fields[2] not in ('reserved', 'starting', 'running', 'finished', 'failed', 'cancelled') or
                            not fields[3].isdigit() or not fields[4].isdigit()):
                        raise Exception('invalid net task receipt response')
                    record = {'phase': fields[2], 'wrapper_pid': int(fields[3]), 'pid': int(fields[4])}
                    if action == 'collect':
                        if len(fields) != 9 or fields[2] != 'finished' or not fields[6].lstrip('-').isdigit():
                            raise Exception('invalid net task result response')
                        record.update(exitcode=int(fields[6]),
                                      **{'out-data': base64.b64decode(fields[7]),
                                         'err-data': base64.b64decode(fields[8])})
                    elif len(fields) != 6:
                        raise Exception('invalid net task control response')
                    return record
            time.sleep(min(0.2, max(0, deadline - time.time())))
        raise Exception('net task control response timed out')

    def _recover_net_task(self, task):
        if not isinstance(task, dict):
            raise Exception('legacy net reservation has no safe cancellation identity')
        # Cancellation is task-bound. A lost reply remains blocked until the
        # Guest independently confirms the named Job has been destroyed.
        record = self._net_task_control(task, 'cancel')
        if record['phase'] != 'cancelled':
            raise Exception('previous net task is still active or unconfirmed')

    def _guest_exec_zs_tools_net(self, config, config_obj, output, wait, retry):
        started = time.time()
        task = None
        execution_started = None
        with _zs_tools_net_lock:
            try:
                if self.vm_uuid in _zs_tools_net_pending:
                    try:
                        self._recover_net_task(_zs_tools_net_pending[self.vm_uuid])
                        _zs_tools_net_pending.pop(self.vm_uuid)
                    except Exception as error:
                        raise Exception('new execution blocked: {}'.format(error))
                task = {'task_id': uuid.uuid4().hex, 'phase': 'preparing', 'pid': None,
                        'budget': max(1, wait * retry - 5)}
                _zs_tools_net_pending[self.vm_uuid] = task
                self._net_task_control(task, 'prepare')
                # Everything after this point may have been accepted by QGA,
                # even if the transport never delivers the wrapper PID.
                task['phase'] = 'unknown'
                execution_started = time.time()
                deadline = execution_started + wait * retry
                result = self.guest_exec(self._net_task_launch_args(task, config),
                                         timeout=self._net_task_timeout(deadline, 3))
                if not result or 'pid' not in result:
                    raise Exception('net launcher submission result is unknown')
                task['wrapper_pid'] = result['pid']
                task['phase'] = 'submitted'
                while time.time() < deadline:
                    time.sleep(min(wait, max(0, deadline-time.time())))
                    remaining = deadline-time.time()
                    if remaining < 1:
                        break
                    try:
                        record = self._net_task_control(task, 'inspect', deadline=deadline)
                    except Exception:
                        # A launch transition can briefly hold the receipt
                        # lock. Retain the reservation and retry within budget.
                        continue
                    task['pid'] = record['pid'] or None
                    remaining = deadline-time.time()
                    if remaining < 1:
                        break
                    if record['phase'] == 'finished':
                        try:
                            completed = self._net_task_control(task, 'collect', deadline=deadline)
                        except Exception:
                            # The wrapper can still be flushing its own output.
                            # Only collect confirms Job absence and binds output
                            # to the task; a numeric QGA exit record cannot do so.
                            continue
                        _zs_tools_net_pending.pop(self.vm_uuid)
                        data = []
                        if output:
                            formatted = self._get_zs_tools_output(completed)
                            if formatted:
                                data.append(formatted)
                        # Best-effort QGA record reclamation only. A stale or
                        # lost PID status never supplies the task result or
                        # proves tree termination.
                        try:
                            self.guest_exec_status_with_timeout(
                                task['wrapper_pid'], self._net_task_timeout(deadline, 1))
                        except Exception:
                            pass
                        code = completed['exitcode']
                        if code != 0 and not data:
                            data.append('net task {} pid={} exited with code={}'.format(
                                task['task_id'], task.get('pid'), code))
                        if code != 0:
                            tail = self._try_read_zs_tools_log_tail(deadline=deadline)
                            if tail:
                                data.append('zs-tools.log tail:\n' + tail)
                        return code, '\n'.join(data)
                    if record['phase'] in ('failed', 'cancelled'):
                        self._recover_net_task(task)
                        _zs_tools_net_pending.pop(self.vm_uuid)
                        detail = 'net task {} pid={} ended without a confirmed result'.format(
                            task['task_id'], task.get('pid'))
                        tail = self._try_read_zs_tools_log_tail(deadline=deadline)
                        if tail:
                            detail += '\nzs-tools.log tail:\n' + tail
                        return 1, detail
                raise Exception('net task {} exceeded deadline {}s'.format(task['task_id'], wait*retry))
            except Exception as error:
                cleanup_started = time.time()
                execution_elapsed = cleanup_started - execution_started if execution_started is not None else 0
                cleanup = ''
                if task is not None:
                    if task['phase'] == 'preparing':
                        # No net launch has been sent; a lost prepare response
                        # can leave an unused receipt but cannot start a task.
                        _zs_tools_net_pending.pop(self.vm_uuid, None)
                    elif task.get('pid') is not None:
                        try:
                            self._recover_net_task(task)
                            _zs_tools_net_pending.pop(self.vm_uuid, None)
                            cleanup = '; guest process tree terminated'
                        except Exception:
                            cleanup = '; cleanup unconfirmed; subsequent net execution blocked'
                    else:
                        cleanup = '; submission result unknown; subsequent net execution blocked'
                tail = self._try_read_zs_tools_log_tail(deadline=cleanup_started + 10)
                if tail:
                    error = '{}\nzs-tools.log tail:\n{}'.format(error, tail)
                return 1, 'qga exec zs-tools operate net failed for vm {}, task={}, pid={}, elapsed={:.3f}s, execution_elapsed={:.3f}s, cleanup_elapsed={:.3f}s, config: {}, error: {}{}'.format(
                    self.vm_uuid, task['task_id'] if task else None,
                    task.get('pid') if task else None, time.time()-started,
                    execution_elapsed, time.time()-cleanup_started, log.mask_sensitive_field(config_obj, config), error, cleanup)

    def guest_exec_zs_tools(self, operate, config, config_obj=None, output=True, wait=qga_exec_wait_interval, retry=zs_tools_wait_retry):
        if operate == 'net':
            return self._guest_exec_zs_tools_net(config, config_obj, output, wait, retry)
        def get_return_data(result):
            return self._get_zs_tools_output(result)

        ret = None
        try:
            execution_started = time.time()
            ret = self.guest_exec(
                {"path": self.ZS_TOOLS_PATN_WIN, "arg": [operate, "--config", config], "capture-output": output})

            if ret and "pid" in ret:
                pid = ret["pid"]
            else:
                raise Exception('guest-exec did not return pid, ret={}'.format(
                    json.dumps(ret, default=repr)))

            ret = None
            deadline = execution_started + wait * retry
            while time.time() < deadline:
                time.sleep(min(wait, max(0, deadline - time.time())))
                remaining = deadline - time.time()
                if remaining < 1:
                    break
                ret = self.guest_exec_status_with_timeout(pid, min(15, int(remaining)))
                if ret['exited']:
                    break

            if not ret or not ret.get('exited'):
                raise Exception('zs-tools pid {} not exited after {} retries, ret={}, {}'.format(
                    pid, retry, json.dumps(ret, default=repr), get_return_data(ret)))

            exitcode = ret.get('exitcode')
            if exitcode is None:
                raise Exception('zs-tools pid {} exited without exitcode, ret={}, {}'.format(
                    pid, json.dumps(ret, default=repr), get_return_data(ret)))
            result = get_return_data(ret)
            if exitcode != 0:
                result = 'zs-tools pid {} exitcode {}, {}'.format(pid, exitcode, result)
                tail = self._try_read_zs_tools_log_tail()
                if tail:
                    result += '\nzs-tools.log tail:\n' + tail
            return exitcode, result
        except Exception as e:
            detail = str(e) or type(e).__name__
            partial_output = get_return_data(ret)
            if partial_output and partial_output not in detail:
                detail += '\n' + partial_output
            tail = self._try_read_zs_tools_log_tail()
            if tail:
                detail += '\nzs-tools.log tail:\n' + tail
            return 1, 'qga exec zs-tools operate {} failed for vm {}, config: {}, error: {}'.format(
                    operate, self.vm_uuid, log.mask_sensitive_field(config_obj, config), detail)

    @staticmethod
    def _decode_zs_tools_data(data):
        decoded = decode_with_fallback(data)
        if not isinstance(decoded, str):
            decoded = decoded.decode('utf-8')
        return decoded.replace('\r\n', '\n')

    def _get_zs_tools_output(self, result):
        if not result:
            return ''
        return '\n'.join(label + ': ' + self._decode_zs_tools_data(result[key])
                         for key, label in [('out-data', 'stdout'), ('err-data', 'stderr')]
                         if result.get(key) is not None)

    def _try_read_zs_tools_log_tail(self, n_bytes=4096, deadline=None):
        # Each attempt uses only bounded QGA file operations, never guest-exec.
        def file_command(command, args):
            timeout = 1
            if deadline is not None:
                # Reserve two seconds before non-close calls so an acquired
                # handle can be closed even with whole-second QGA timeouts.
                reserve = 0 if command == 'guest-file-close' else 2
                timeout = self._net_task_timeout(deadline - reserve, 1)
            return self.call_qga_command(command, args, timeout=timeout)

        paths = [r'C:\Program Files\GuestTools\zs-tools\log\zs-tools.log',
                 r'C:\Program Files\zstack\zs-tools\log\zs-tools.log']
        for path in paths:
            handle = None
            try:
                handle = file_command('guest-file-open',
                                      {'path': path, 'mode': 'rb'})
                end = file_command('guest-file-seek',
                                   {'handle': handle, 'offset': 0, 'whence': 2})
                file_command('guest-file-seek',
                             {'handle': handle, 'offset': max(0, end['position'] - n_bytes),
                              'whence': 0})
                data = file_command('guest-file-read',
                                    {'handle': handle, 'count': n_bytes})
                return self._decode_zs_tools_data(data.get('buf-b64', b''))
            except Exception:
                pass
            finally:
                if handle is not None:
                    try:
                        file_command('guest-file-close', {'handle': handle})
                    except Exception:
                        pass
        return ''

    def guest_exec_wmic(self, cmd, output=True, wait=qga_exec_wait_interval, retry=qga_exec_wait_retry):
        cmd_parts = cmd.split('|')
        cmd = "{}".format(" ".join([part for part in cmd_parts]))

        ret = self.guest_exec(
            {"path": "wmic", "arg": cmd.split(" "), "capture-output": output})
        if ret and "pid" in ret:
            pid = ret["pid"]
        else:
            raise Exception('qga exec cmd {} failed for vm {}'.format(cmd, self.vm_uuid))

        if not output:
            logger.warn("run qga wmic: {} failed, no output".format(cmd))
            return 0, None

        ret = None
        for i in range(retry):
            time.sleep(wait)
            ret = self.guest_exec_status(pid)
            if ret['exited']:
                break

        if not ret or not ret.get('exited'):
            raise Exception('qga exec cmd {} timeout for vm {}'.format(cmd, self.vm_uuid))

        exit_code = ret.get('exitcode')
        ret_data = None
        if 'out-data' in ret:
            ret_data = decode_with_fallback(ret['out-data'])
        elif 'err-data' in ret:
            ret_data = decode_with_fallback(ret['err-data'])

        return exit_code, ret_data

    def guest_exec_powershell(self, cmd, output=True, wait=qga_exec_wait_interval, retry=qga_exec_wait_retry):
        cmd_parts = cmd.split('|')
        cmd = "& '{}'".format("' '".join([part for part in cmd_parts]))

        ret = self.guest_exec(
            {"path": "powershell.exe", "arg": ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd], "capture-output": output})
        if ret and "pid" in ret:
            pid = ret["pid"]
        else:
            raise Exception('qga exec cmd {} failed for vm {}'.format(cmd, self.vm_uuid))

        if not output:
            logger.warn("run qga powershell: {} failed, no output".format(cmd))
            return 0, None

        ret = None
        for i in range(retry):
            time.sleep(wait)
            ret = self.guest_exec_status(pid)
            if ret['exited']:
                break

        if not ret or not ret.get('exited'):
            raise Exception('qga exec cmd {} timeout for vm {}'.format(cmd, self.vm_uuid))

        exit_code = ret.get('exitcode')
        ret_data = None
        if 'out-data' in ret:
            ret_data = decode_with_fallback(ret['out-data'])
        elif 'err-data' in ret:
            ret_data = decode_with_fallback(ret['err-data'])

        return exit_code, ret_data

    def guest_exec_powershell_no_exitcode(self, cmd, exception=True, output=True):
        exitcode, ret_data = self.guest_exec_powershell(cmd, output)
        if exitcode != 0:
            if exception:
                raise Exception('cmd {}, exitcode {}, ret {}'
                                .format(cmd, exitcode, ret_data))
            return None
        return ret_data

    def guest_exec_cmd_no_exitcode(self, cmd, exception=True, output=True):
        if "mswindows" in self.os:
            return self.guest_exec_powershell_no_exitcode(cmd, exception, output)
        else:
            return self.guest_exec_bash_no_exitcode(cmd, exception, output)

    def guest_info(self):
        """
        {"return":{
                "version":"2.12.0",
                "supported_commands":[
                    {"enabled":true,"name":"guest-get-osinfo","success-response":true},
                    {"enabled":true,"name":"guest-get-timezone","success-response":true},
                    {"enabled":true,"name":"guest-get-users","success-response":true},
                    {"enabled":true,"name":"guest-get-host-name","success-response":true},
                    {"enabled":true,"name":"guest-exec","success-response":true},
                    {"enabled":true,"name":"guest-exec-status","success-response":true},
                    {"enabled":true,"name":"guest-get-memory-block-info","success-response":true},
                    {"enabled":true,"name":"guest-set-memory-blocks","success-response":true},
                    {"enabled":true,"name":"guest-get-memory-blocks","success-response":true},
                    {"enabled":true,"name":"guest-set-user-password","success-response":true},
                    {"enabled":true,"name":"guest-get-fsinfo","success-response":true},
                    {"enabled":true,"name":"guest-set-vcpus","success-response":true},
                    {"enabled":true,"name":"guest-get-vcpus","success-response":true},
                    {"enabled":true,"name":"guest-network-get-interfaces","success-response":true},
                    {"enabled":true,"name":"guest-suspend-hybrid","success-response":false},
                    {"enabled":true,"name":"guest-suspend-ram","success-response":false},
                    {"enabled":true,"name":"guest-suspend-disk","success-response":false},
                    {"enabled":true,"name":"guest-fstrim","success-response":true},
                    {"enabled":true,"name":"guest-fsfreeze-thaw","success-response":true},
                    {"enabled":true,"name":"guest-fsfreeze-freeze-list","success-response":true},
                    {"enabled":true,"name":"guest-fsfreeze-freeze","success-response":true},
                    {"enabled":true,"name":"guest-fsfreeze-status","success-response":true},
                    {"enabled":true,"name":"guest-file-flush","success-response":true},
                    {"enabled":true,"name":"guest-file-seek","success-response":true},
                    {"enabled":true,"name":"guest-file-write","success-response":true},
                    {"enabled":true,"name":"guest-file-read","success-response":true},
                    {"enabled":true,"name":"guest-file-close","success-response":true},
                    {"enabled":true,"name":"guest-file-open","success-response":true},
                    {"enabled":true,"name":"guest-shutdown","success-response":false},
                    {"enabled":true,"name":"guest-info","success-response":true},
                    {"enabled":true,"name":"guest-set-time","success-response":true},
                    {"enabled":true,"name":"guest-get-time","success-response":true},
                    {"enabled":true,"name":"guest-ping","success-response":true},
                    {"enabled":true,"name":"guest-sync","success-response":true},
                    {"enabled":true,"name":"guest-sync-delimited","success-response":true}]}}
        """
        return self.call_qga_command("guest-info")

    def guest_ping(self):
        ret = self.call_qga_command("guest-ping")
        return ret

    def guest_agent_available(self):
        try:
            ret = self.guest_ping()
            if ret is None:
                return False
            return True
        except:
            return False

    def guest_exec_get_os_info(self):
        """
        {"name":"CentOS Linux",
        "kernel-release":"3.10.0-957.el7.x86_64",
        "version":"7 (Core)",
        "pretty-name":"CentOS Linux 7 (Core)",
        "version-id":"7",
        "kernel-version":"#1 SMP Thu Nov 8 23:39:32 UTC 2018",
        "machine":"x86_64",
        "id":"centos" }
        """
        ret = self.call_qga_command("guest-get-osinfo")
        if ret and "id" in ret and "version-id" in ret:
            vm_os = ret["id"].lower()
            version = ret["version-id"].lower().split(".")[0]
            kernel_version = ret["kernel-version"].lower().split(".")[0]
            return vm_os, version, kernel_version
        raise Exception('get vm %s os info failed' % self.vm_uuid)

    def guest_get_os_id_like(self):
        """
        # cat /etc/os-release
            NAME="CentOS Linux"
            VERSION="7 (Core)"
            ID="centos"
            ID_LIKE="rhel fedora"
            VERSION_ID="7"
            PRETTY_NAME="CentOS Linux 7 (Core)"
            ANSI_COLOR="0;31"
            CPE_NAME="cpe:/o:centos:centos:7"
            HOME_URL="https://www.centos.org/"
            BUG_REPORT_URL="https://bugs.centos.org/"

            CENTOS_MANTISBT_PROJECT="CentOS-7"
            CENTOS_MANTISBT_PROJECT_VERSION="7"
            REDHAT_SUPPORT_PRODUCT="centos"
            REDHAT_SUPPORT_PRODUCT_VERSION="7"
        """
        ret = self.guest_exec_bash_no_exitcode('cat /etc/os-release | grep ID_LIKE', exception=False)
        if ret:
            info = ret.split("=")
            return info[1] if len(info) > 1 else None
        return None

    def guest_get_os_info(self):
        ret = self.guest_exec_bash_no_exitcode('cat /etc/os-release')
        if not ret:
            # Parse /etc/redhat-release for CentOS/RHEL 6
            ret = self.guest_exec_bash_no_exitcode('cat /etc/redhat-release')
            if not ret:
                raise Exception('get os info failed')
            parts = ret.split()
            if len(parts) >= 3 and parts[1] == 'release':
                config = {'ID': parts[0].lower(), 'VERSION_ID': parts[2]}
        else:
            # Parse /etc/os-release
            lines = [line for line in ret.split('\n') if line != ""]
            config = {}
            for line in lines:
                if line.startswith('#'):
                    continue

                info = line.split('=')
                if len(info) != 2:
                    continue
                config[info[0].strip()] = info[1].strip().strip('"')

        vm_os = config.get('ID')
        version = config.get('VERSION_ID')
        if vm_os and version and vm_os == self.VM_OS_LINUX_UBUNTU:
            version = version.split(".")[0]

        return vm_os, version, config.get('ID_LIKE')

    def qga_init(self):
        self.state = self.QGA_STATE_NOT_RUNNING
        if not is_qga_connected(self.domain):
            return

        if self.domain.isActive() and self.guest_agent_available():
            self.state = self.QGA_STATE_RUNNING
        else:
            return

        ret = self.guest_info()
        if ret:
            self.version = ret["version"]
            supported_commands = ret["supported_commands"]
            self.supported_commands = {cmd["name"]: cmd["enabled"] for cmd in supported_commands}

        try:
            if 'guest-get-osinfo' in self.supported_commands and \
                    self.supported_commands['guest-get-osinfo']:
                self.os, self.os_version, kernel_version = self.guest_exec_get_os_info()
                if self.os == VmQga.VM_OS_WINDOWS:
                    self.os_id_like = "windows"
                    if 'n/a' in self.os_version:
                        self.os_version = kernel_version
                else:
                    self.os_id_like = self.guest_get_os_id_like()
            else:
                self.os, self.os_version, self.os_id_like = self.guest_get_os_info()
        except Exception as e:
            # logger.warn("qga init failed {}".format(e))
            pass

    """
    def guest_get_hostname(self):
        ret = self.call_qga_command("guest-get-host-name")
        if ret and 'host-name' in ret:
            return ret['host-name']
        else:
            raise QgaException(ERROR_CODE_QGA_RETURN_VALUE_ERROR, 'qga get hostname failed for vm %s' % self.vm_uuid)
    """

    def guest_file_open(self, path, create=False):
        if create:
            mode = "w+"
        else:
            mode = "r"
        return self.call_qga_command("guest-file-open", args={"path": path, "mode": mode})

    def guest_file_close(self, handle):
        return self.call_qga_command("guest-file-close", args={"handle": handle})

    def guest_file_flush(self, handle):
        return self.call_qga_command("guest-file-flush", args={"handle": handle})

    def guest_file_read(self, path, not_exist_exception=False):
        try:
            handle = self.call_qga_command("guest-file-open", args={"path": path, "mode": 'r'})
        except Exception as e:
            if not not_exist_exception and 'No such file' in str(e):
                return 0, None
            raise

        data_b64 = ''
        total_count = 0

        try:
            while True:
                ret = self.call_qga_command("guest-file-read", args={"handle": handle})
                data_b64 += ret.get('buf-b64')
                total_count += ret.get('count')
                if ret.get('count') == 0:
                    break
        finally:
            self.guest_file_close(handle)
        return total_count, data_b64

    def guest_file_is_exist(self, path):
        try:
            handle = self.call_qga_command("guest-file-open", args={"path": path, "mode": 'r'})
        except Exception as e:
            return False

        self.guest_file_close(handle)
        return True

    def guest_file_write(self, path, contents):
        handle = self.guest_file_open(path, True)
        try:
            ret = self.call_qga_command("guest-file-write", args={"handle": handle, "buf-b64": contents})
        finally:
            self.guest_file_close(handle)
        return ret.get('count')

    def guest_ssh_add_authorized_keys(self, public_key):
        args = {
            'username': 'root',
            'keys': [public_key]
        }
        ret = self.call_qga_command('guest-ssh-add-authorized-keys', args)
        return ret

    def guest_ssh_remove_authorized_keys(self, public_key):
        args = {
            'username': 'root',
            'keys': [public_key]
        }
        ret = self.call_qga_command('guest-ssh-remove-authorized-keys', args)
        return ret
