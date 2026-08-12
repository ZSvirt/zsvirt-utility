
import base64
import json
import os
import string
import zlib

import bash
import jsonobject
import log
import shell
from linux import get_vm_pid, get_process_start_time, HOST_ARCH
from zstacklib.utils import qemu_img

logger = log.get_logger(__name__)


def get_colo_path():
    return '/var/lib/zstack/colo/qemu-system-x86_64'

def get_path():
    if os.path.exists('/usr/libexec/qemu-kvm'):
        return '/usr/libexec/qemu-kvm'
    elif os.path.exists('/bin/qemu-kvm'):
        return '/bin/qemu-kvm'
    elif os.path.exists('/usr/bin/qemu-system-{}'.format(HOST_ARCH)):
        return '/usr/bin/qemu-system-{}'.format(HOST_ARCH)
    else:
        raise Exception(
            'Could not find qemu-kvm in /bin/qemu-kvm or /usr/libexec/qemu-kvm or /usr/bin/qemu-system-{}'
            .format(HOST_ARCH))


def get_bin_dir():
    if os.path.exists('/usr/share/qemu-kvm/'):
        return '/usr/share/qemu-kvm/'
    elif os.path.exists('/usr/share/qemu/'):
        return '/usr/share/qemu/'
    else:
        raise Exception(
            'Could not find qemu/qemu-kvm bin directory in /usr/share/qemu-kvm/ or /usr/share/qemu/')


def get_version():
    version = shell.call("virsh version | awk '/hypervisor.*QEMU/{print $4}'", False).strip()

    # failed to get qemu version, fallback to get its version from path
    if version == "":
        return get_version_from_exe_file2(get_path())

    return version

def get_install_date():
    r, o, _ = bash.bash_roe("rpm -q --queryformat '%{INSTALLTIME}' qemu-kvm")
    if r == 0:
        return int(o.strip())
    return None

QEMU_INSTALL_DATE = get_install_date()
QEMU_VERSION = get_version()


def get_running_version(vm_uuid):
    pid = get_vm_pid(vm_uuid)
    if pid:
        start_time = get_process_start_time(pid)
        if start_time is not None and QEMU_INSTALL_DATE is not None and start_time > QEMU_INSTALL_DATE:
            return QEMU_VERSION
        exe = "/proc/%s/exe" % pid
        r = get_version_from_exe_file(exe)
        if r:
            return r

    r, o, e = bash.bash_roe("""virsh qemu-monitor-command %s '{"execute":"query-version"}'""" % vm_uuid)
    if r == 0:
        ret = jsonobject.loads(o.strip())
        if ret["return"].package:
            return _parse_version(ret["return"].package)
        else:
            qv = ret["return"].qemu
            return "%d.%d.%d" % (qv.major, qv.minor, qv.micro)

    logger.debug("cannot get vm[uuid:%s] version from qmp: %s" % (vm_uuid, e))
    return get_version_from_exe_file(get_path())

# QEMU emulator version 2.12.0 (qemu-kvm-ev-2.12.0-44.1.el7_9.1)
# return qemu-kvm-ev-2.12.0-44.1.el7_9.1
def get_version_from_exe_file(path, error_out=False):
    r, o, e = bash.bash_roe("%s --version" % path)
    if r == 0:
        return _parse_version(o.strip())

    if error_out:
        raise Exception("cannot get version from %s: %s" % (path, e))
    else:
        logger.debug("cannot get version from %s: %s" % (path, e))

    return ""

# QEMU emulator version 2.12.0 (qemu-kvm-ev-2.12.0-44.1.el7_9.1)
# return 2.12.0
def get_version_from_exe_file2(path, error_out=False):
    r, o, e = bash.bash_roe("%s --version" % path)
    if r == 0:
        return _parse_version2(o.strip())

    if error_out:
        raise Exception("cannot get version from %s: %s" % (path, e))
    else:
        logger.debug("cannot get version from %s: %s" % (path, e))

    return ""

# QEMU emulator version 2.12.0 (qemu-kvm-ev-2.12.0-44.1.el7_9.1)
# return qemu-kvm-ev-2.12.0-44.1.el7_9.1
def _parse_version(version_output):
    lines = version_output.splitlines()
    ver_line = lines[0].strip()

    for line in lines:
        if line.lower().startswith("qemu"):
            ver_line = line.strip()
            break

    if "(" in ver_line:
        full_ver = ver_line.split("(", 1)[1].split(")", 1)[0]
    else:
        full_ver = ver_line

    return "-".join(filter(lambda s: s[0].isdigit(), full_ver.split("-")))

# QEMU emulator version 2.12.0 (qemu-kvm-ev-2.12.0-44.1.el7_9.1)
# QEMU emulator version 4.2.0 (qemu-kvm-4.2.0-640.g70d8f25.el7)
# return 2.12.0
def _parse_version2(version_output):
    return version_output.splitlines()[0].strip().split(" ")[3]


def get_device_map(path, option=""):
    out = shell.call("%s --output=json %s %s" % (qemu_img.subcmd('map'), option, path))
    return out.strip()


def get_data_bitmap(path, max_length, zero, data, qemu_img_command_option=""):
    map = get_device_map(path, qemu_img_command_option)
    json_map = jsonobject.loads(map)
    result = {}
    for item in json_map:
        if item['zero'] is zero and item['data'] is data:
            if item['length'] > max_length:
                result.update(split_large_blocks(item, max_length))
            else:
                result[item['start']] = item['length']
    return result


def get_rbd_data_bitmap(path, max_length):
    o = shell.call("""rbd diff %s --format json""" % (path))
    o = jsonobject.loads(o.strip())
    result = {}
    for item in o:
        if item['exists'] == 'true':
            if item['length'] > max_length:
                result.update(split_large_blocks(item, max_length))
            else:
                result[item['offset']] = item['length']
    return result


def split_large_blocks(item, max_length):
    start = item['start']
    length = item['length']
    result = {}

    while length > 0:
        chunk_length = min(length, max_length)
        result[start] = chunk_length
        start += chunk_length
        length -= chunk_length
    return result


def compress_and_encode_bitmap(bitmap_map):
    bitmap_json = json.dumps(bitmap_map)
    compressed_data = zlib.compress(bitmap_json.encode('utf-8'))
    return base64.b64encode(compressed_data).decode('utf-8')


"""
read image content with qemu-io read command, which only support -v option.
so we should analyze the output to get the content.
e.g.
00000000:  31 31 31 31 31 31 31 31 0a 31 31 31 31 31 31 31  11111111.1111111
00000010:  31 0a 31 31 31 31 31 31 31 31 0a 31 31 31 31 31  1.11111111.11111
00000020:  31 31 31 0a 31 31 31 31 31 31 31 31 0a 31 31 31  111.11111111.111
00000030:  31 31 31 31 31 0a 31 31 31 31 31 31 31 31 0a 31  11111.11111111.1
00000040:  31 31 31 31 31 31 31 0a 31 31 31 31 31 31 31 31  1111111.11111111
00000050:  0a 31 31 31 31 31 31 31 31 0a 31 31 31 31 31 31  .11111111.111111
00000060:  31 31 0a 31 11 11 11 11 11 11 11 11 11 11 11 11  11.1............
00000070:  11 11 11 11 11 11 11 11 11 11 11 11 11 11 11 11  ................
read 128/128 bytes at offset 0
128 bytes, 1 ops; 00.04 sec (3.405 KiB/sec and 27.2394 ops/sec)

"""

def read_image_content(image_path, offset, length, format):
    l = (length + 15) & ~15  # align to 16 bytes
    cmd = "qemu-io -c 'read -v %s %s' -f %s %s" % (offset, l, format, image_path)
    o = shell.call(cmd)

    content = ""
    for line in o.splitlines():
        h = line.split(":", 1)[0].strip()
        if set(h).issubset(string.hexdigits):
            # encode hex str to ascii
            content += "".join(chr(int(x, 16)) for x in line.split()[1:17])

    return content[0:length]
