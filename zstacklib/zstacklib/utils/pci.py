import os

from zstacklib.utils import log, linux, bash

logger = log.get_logger(__name__)


def is_gpu(type):
    return type in ['GPU_3D_Controller', 'GPU_Video_Controller']


def fmt_pci_address(pci_device):
    # type: (dict) -> str
    domain = pci_device['domain'] if 'domain' in pci_device else 0
    return "%s:%s:%s.%s" % (format(domain, '04x'),
                            format(pci_device['bus'], '02x'),
                            format(pci_device['slot'], '02x'),
                            format(pci_device['function'], 'x'))

#   ex: lspci_s('0000:00:04.0') # equals to: lspci -s '0000:00:04.0'
#   output:
#   [
#       {
#           'Slot' : '0000:00:04.0',
#           'Vendor' : 'Intel Corporation',
#           'VendorId' : '8086',
#           'NUMANode' : '0',
#           'IOMMUGroup' : '0',
#           ...
#       }
#   ]
def lspci_s(option_slot):
    # type: (str) -> list[dict]
    value_r, value_o = bash.bash_ro('lspci -Dmmv -s ' + option_slot)
    if value_r != 0:
        return []

    id_r, id_o = bash.bash_ro('lspci -Dmmnv -s ' + option_slot)
    if id_r != 0:
        return []

    return _parse_lspci(value_o, id_o)

def lspci():
    # type: () -> list[dict]
    value_r, value_o = bash.bash_ro('lspci -Dmmv')
    if value_r != 0:
        return []

    id_r, id_o = bash.bash_ro('lspci -Dmmnv')
    if id_r != 0:
        return []

    return _parse_lspci(value_o, id_o)

def lspci_s_or_throw(option_slot):
    # type: (str) -> list[dict]
    value_r, value_o, value_e = bash.bash_roe('lspci -Dmmv -s ' + option_slot)
    if value_r != 0:
        raise Exception('failed to execute command lspci: %s' % value_e)

    id_r, id_o, id_e = bash.bash_roe('lspci -Dmmnv -s ' + option_slot)
    if id_r != 0:
        raise Exception('failed to execute command lspci: %s' % id_e)

    return _parse_lspci(value_o, id_o)

def lspci_or_throw():
    # type: () -> list[dict]
    value_r, value_o, value_e = bash.bash_roe('lspci -Dmmv')
    if value_r != 0:
        raise Exception('failed to execute command lspci: %s' % value_e)

    id_r, id_o, id_e = bash.bash_roe('lspci -Dmmnv')
    if id_r != 0:
        raise Exception('failed to execute command lspci: %s' % id_e)

    return _parse_lspci(value_o, id_o)

def _parse_lspci(value_o, id_o):
    # type: (str, str) -> list[dict]
    results = []

    def parse_lines(lines):
        groups = {}
        current = {}
        for line in lines:
            line = line.strip()
            if not line:
                if 'Slot' in current:
                    groups[current['Slot']] = current
                current = {}
                continue

            key_value = line.split(':', 1)
            if len(key_value) < 2:
                continue

            key = key_value[0].strip()
            value = key_value[1].strip()
            current[key] = value

        if current and 'Slot' in current:
            groups[current['Slot']] = current
        return groups

    group_value_by_slot = parse_lines(value_o.strip().splitlines())
    group_id_by_slot = parse_lines(id_o.strip().splitlines())

    for slot, pci in group_value_by_slot.items():
        id_data = group_id_by_slot.get(slot, {})
        if id_data.get('Class', ''):
            pci['ClassId'] = id_data.get('Class', '')
        if id_data.get('Vendor', ''):
            pci['VendorId'] = id_data.get('Vendor', '')
        if id_data.get('Device', ''):
            pci['DeviceId'] = id_data.get('Device', '')
        if id_data.get('SVendor', ''):
            pci['SVendorId'] = id_data.get('SVendor', '')
        if id_data.get('SDevice', ''):
            pci['SDeviceId'] = id_data.get('SDevice', '')
        results.append(pci)

    return results

NAIDIA_SRIOV_MANAGE_PATH = '/usr/lib/nvidia/sriov-manage'

def is_nvidia_pci_device(pci_info):
    # type: (dict) -> bool
    # ex:
    #   is_nvidia_pci_device(lspci_s('0000:00:04.0'))
    #   is_nvidia_pci_device(lspci()[0])
    return  pci_info.has_key('Vendor') and 'NVIDIA' in pci_info['Vendor'] or \
            pci_info.has_key('Device') and 'NVIDIA' in pci_info['Device'] or \
            pci_info.has_key('SVendor') and 'NVIDIA' in pci_info['SVendor']

def disable_nvidia_vgpu_by_sriov_manage(pci_info):
    # type: (dict) -> str | None
    # return: error message
    # ex:
    #   disable_nvidia_vgpu_by_sriov_manage(lspci_s('0000:00:04.0'))
    #   disable_nvidia_vgpu_by_sriov_manage(lspci()[0])
    return disable_nvidia_vgpu_address_by_sriov_manager(pci_info['Slot'])

def disable_nvidia_vgpu_address_by_sriov_manager(address):
    # type: (str) -> str | None
    # return: error message
    # ex:
    #   disable_nvidia_vgpu_address_by_sriov_manager('0000:00:04.0')
    sriov_manage_exists = os.path.exists(NAIDIA_SRIOV_MANAGE_PATH)
    if not sriov_manage_exists:
        return None

    r, _, stderr = bash.bash_roe("/usr/lib/nvidia/sriov-manage -d %s" % address)
    if r != 0:
        return 'Failed to disable vgpu device %s by sriov-manage: %s' % (address, stderr)
    return None

def enable_nvidia_vgpu_by_sriov_manage(pci_info):
    # type: (dict) -> str | None
    # return: error message
    # ex:
    #   enable_nvidia_vgpu_by_sriov_manage(lspci_s('0000:00:04.0'))
    #   enable_nvidia_vgpu_by_sriov_manage(lspci()[0])
    return enable_nvidia_vgpu_address_by_sriov_manage(pci_info['Slot'])

def enable_nvidia_vgpu_address_by_sriov_manage(address):
    # type: (str) -> str | None
    # return: error message
    # ex:
    #   enable_nvidia_vgpu_address_by_sriov_manage('0000:00:04.0')
    sriov_manage_exists = os.path.exists(NAIDIA_SRIOV_MANAGE_PATH)
    if not sriov_manage_exists:
        return 'Failed to enable vgpu device %s: sriov-manage not found' % address

    r, _, stderr = bash.bash_roe("/usr/lib/nvidia/sriov-manage -e %s" % address)
    if r != 0:
        return 'Failed to enable vgpu device %s by sriov-manage: %s' % (address, stderr)
    return None

PCI_IOV_NUM_BAR = 6
PCI_BASE_ADDRESS_MEM_TYPE_MASK = 0x06
PCI_BASE_ADDRESS_MEM_TYPE_32 = 0x00  # 32 bit address
PCI_BASE_ADDRESS_MEM_TYPE_64 = 0x04  # 64 bit address
PCI_DEVICES_ROOT = "/sys/bus/pci/devices"

DEFAULT_PCDPCIMMIO64SIZE_ON_32BIT = 0x100000000
DEFAULT_PCDPCIMMIO64SIZE_MIN_SIZE = 0x800000000
max_addressable_memory_32bit = 2 * 1024 * 1024
max_addressable_memory_64bit = 2 * 1024 * 1024


class MemoryResource:
    def __init__(self, start, end, flags, path):
        self.start = start
        self.end = end
        self.flags = flags
        self.path = path

    def __str__(self):
        return "start: %s, end: %s, flags: %s, path: %s" % (self.start, self.end, self.flags, self.path)

    def __repr__(self):
        return str(self)


def calc_next_power_of_2(n):
    """
    Calculate the next power of 2 for a given number.

    :param n: The input number
    :return: The next power of 2
    """
    n -= 1
    n |= n >> 1
    n |= n >> 2
    n |= n >> 4
    n |= n >> 8
    n |= n >> 16
    n |= n >> 32
    n += 1
    return n


def need_config_pcimmio():
    if max_addressable_memory_64bit <= DEFAULT_PCDPCIMMIO64SIZE_ON_32BIT:
        logger.info("max_addressable_memory %s is less than DEFAULT_PCDPCIMMIO64SIZE_ON_32BIT %s" %
                    (max_addressable_memory_64bit, DEFAULT_PCDPCIMMIO64SIZE_ON_32BIT))
        return False

    return True


def get_bars_max_addressable_memory():
    if max_addressable_memory_64bit is None:
        logger.warn("max_addressable_memory is None, please reconnect host and try again")

    if max_addressable_memory_64bit < DEFAULT_PCDPCIMMIO64SIZE_MIN_SIZE:
        return DEFAULT_PCDPCIMMIO64SIZE_MIN_SIZE / 1024 / 1024

    return max_addressable_memory_64bit / 1024 / 1024


def calculate_max_addressable_memory(pci_devices):
    global max_addressable_memory_32bit
    global max_addressable_memory_64bit
    max32bit = 2 * 1024 * 1024
    max64bit = 2 * 1024 * 1024

    for dev in pci_devices:
        if not is_gpu(dev.type):
            continue

        mem_size_32bit, mem_size_64bit = get_total_addressable_memory(get_pci_resources(dev.pciDeviceAddress))
        logger.info("get pci device: %s, name: %s, max addressable memory: %s" %
                    (dev.pciDeviceAddress, dev.name, mem_size_64bit))
        if max32bit < mem_size_32bit:
            max32bit = mem_size_32bit
        if max64bit < mem_size_64bit:
            max64bit = mem_size_64bit

    max_addressable_memory_32bit = max32bit * 2
    max_addressable_memory_64bit = max64bit
    logger.info("calculate max addressable memory: 32bit: "
                "%s, 64bit: %s", max_addressable_memory_32bit, max_addressable_memory_64bit)


def get_total_addressable_memory(resources):
    # type: (dict) -> (int, int)
    """
        Calculate the total addressable memory for 32-bit and 64-bit addresses.

        :param resources: A dictionary of memory resources
        :return: A tuple containing the 32-bit and 64-bit addressable memory sizes
    """
    mem_size_32bit = 0
    mem_size_64bit = 0

    for key in resources.keys():
        # The PCIe spec only defines 5 BARs per device, we're
        # discarding everything after the 5th entry of the resources
        # file, see lspci.c
        if key >= PCI_IOV_NUM_BAR:
            break

        region = resources[key]
        flags = region.flags & PCI_BASE_ADDRESS_MEM_TYPE_MASK
        mem_size = (region.end - region.start) + 1

        if flags == PCI_BASE_ADDRESS_MEM_TYPE_32:
            mem_size_32bit += mem_size
        if flags == PCI_BASE_ADDRESS_MEM_TYPE_64:
            mem_size_64bit += mem_size

    mem_size_32bit = calc_next_power_of_2(mem_size_32bit)
    mem_size_64bit = calc_next_power_of_2(mem_size_64bit)

    return mem_size_32bit, mem_size_64bit


def get_pci_resources(device_address):
    device_path = os.path.join(PCI_DEVICES_ROOT, device_address)
    return parse_resources(device_path)


def parse_resources(device_path):
    resources = {}
    try:
        with open(os.path.join(device_path, "resource"), "r") as f:
            for i, line in enumerate(f):
                start, end, flags = map(lambda x: int(x, 16), line.strip().split())
                if start != 0 or end != 0:
                    resources[i] = MemoryResource(start, end, flags, os.path.join(device_path, "resource"))
    except Exception as e:
        logger.warn(linux.get_exception_stacktrace())
        logger.warn("Error parsing resources for %s: %s" % (device_path, str(e)))

    logger.info("get pci device[path: %s],resources: %s" % (device_path, resources))
    return resources
