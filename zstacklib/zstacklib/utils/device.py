import os

import linux
from zstacklib.utils import bash

try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote


def get_device_wwid(device_name):
    if not os.path.exists(os.path.join('/dev', device_name)):
        raise Exception('device name[%s] does not exist' % device_name)

    wwid_path = os.path.join('/sys/block/', device_name,  'device/wwid')
    if os.path.exists(wwid_path):
        return linux.read_file(wwid_path)

    slave_dir = os.path.join('/sys/block/', device_name,  'slaves')
    if not os.path.exists(slave_dir) or not os.listdir(slave_dir):
        raise Exception('device[%s] does not have wwid nor slaves' % device_name)

    for slave in os.listdir(slave_dir):
        return get_device_wwid(slave)


def get_block_device_wwid(device_path, device_type):
    """Read the discovery WWID used by SCSI, NVMe or multipath devices."""
    dev_name = os.path.basename(os.path.realpath(device_path))
    wwid = None
    result = 0
    if device_type == 'disk' and dev_name.startswith('nvme'):
        result, wwid = bash.bash_ro('timeout -k 1 10 cat /sys/class/block/%s/wwid' % shell_quote(dev_name))
    elif device_type == 'mpath':
        result, dm_uuid = bash.bash_ro('timeout -k 1 10 cat /sys/class/block/%s/dm/uuid' % shell_quote(dev_name))
        if dm_uuid.startswith('mpath-'):
            wwid = dm_uuid[len('mpath-'):]
    elif device_type == 'disk':
        if os.path.exists('/usr/lib/udev/scsi_id'):
            result, wwid = bash.bash_ro('timeout -k 1 10 /usr/lib/udev/scsi_id -g -u -d %s' % shell_quote(device_path))
        else:
            result, wwid = bash.bash_ro("timeout -k 1 10 lsscsi -i | awk -v disk=%s "
                                      "'$(NF-1)==disk {print $NF}'" % shell_quote(device_path), pipe_fail=True)
    return wwid if result == 0 else None


