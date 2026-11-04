import unittest
import json
import os
import sys
import types


def _identity_decorator(*args, **kwargs):
    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]
    return lambda func: func


def _module(name, path=None):
    ret = types.ModuleType(name)
    if path is not None:
        ret.__path__ = [path]
    sys.modules[name] = ret
    return ret


def _install_import_stubs():
    package_dir = os.path.dirname(os.path.dirname(__file__))

    original_exists = os.path.exists
    os.path.exists = lambda path: True if path == "/var/lib/libvirt/qemu/zstack" else original_exists(path)

    kvmagent_pkg = _module("kvmagent", package_dir)
    kvmagent_mod = _module("kvmagent.kvmagent")

    class KvmError(Exception):
        pass

    class AgentCommand(object):
        pass

    class AgentResponse(object):
        def __init__(self):
            self.success = True
            self.error = None

    class KvmAgent(object):
        pass

    kvmagent_mod.KvmError = KvmError
    kvmagent_mod.AgentCommand = AgentCommand
    kvmagent_mod.AgentResponse = AgentResponse
    kvmagent_mod.KvmAgent = KvmAgent
    kvmagent_mod.replyerror = _identity_decorator
    kvmagent_mod.get_http_server = lambda: None
    kvmagent_pkg.kvmagent = kvmagent_mod

    plugins_pkg = _module("kvmagent.plugins", os.path.join(package_dir, "plugins"))
    for name in [
        "kvmagent.plugins.baremetal_v2_gateway_agent",
        "kvmagent.plugins.bmv2_gateway_agent",
        "kvmagent.plugins.imagestore",
        "kvmagent.plugins.nvram",
        "kvmagent.plugins.volume_secret",
        "kvmagent.plugins.vms",
        "kvmagent.plugins.vms.vm_host_file",
        "kvmagent.plugins.vms.vm_host_file_monitor",
        "kvmagent.plugins.vms.tpm",
    ]:
        module = _module(name)
        parent = ".".join(name.split(".")[:-1])
        setattr(sys.modules[parent], name.split(".")[-1], module)

    sys.modules["kvmagent.plugins.baremetal_v2_gateway_agent"].BaremetalV2GatewayAgentPlugin = object
    sys.modules["kvmagent.plugins.bmv2_gateway_agent"].utils = object()
    sys.modules["kvmagent.plugins.imagestore"].ImageStoreClient = object
    sys.modules["kvmagent.plugins.nvram"].nvram = object()
    plugins_pkg.volume_secret = sys.modules["kvmagent.plugins.volume_secret"]

    zstacklib_pkg = _module("zstacklib")
    utils_pkg = _module("zstacklib.utils")
    zstacklib_pkg.utils = utils_pkg

    class Logger(object):
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warn(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    for sub in [
        "ip", "ebtables", "iptables", "lock", "bash", "plugin", "iscsi", "qemu_nbd",
        "lvm", "ft", "shell", "uuidhelper", "xmlobject", "xmlhook", "linux", "misc",
        "qemu_img", "qemu", "qmp", "vm_operator", "pci", "image", "iproute", "ovs",
        "drbd", "jsonobject", "qga", "report", "vm_plugin_queue_singleton",
        "libvirt_singleton",
    ]:
        module = _module("zstacklib.utils." + sub)
        setattr(utils_pkg, sub, module)

    log_mod = _module("zstacklib.utils.log")
    log_mod.get_logger = lambda name: Logger()
    log_mod.sensitive_fields = _identity_decorator
    utils_pkg.log = log_mod
    utils_pkg.report.log = log_mod

    utils_pkg.lock.file_lock = _identity_decorator
    utils_pkg.lock.lock = _identity_decorator
    utils_pkg.bash.in_bash = _identity_decorator
    utils_pkg.misc.ignoreerror = _identity_decorator

    class TimeoutObject(object):
        pass

    utils_pkg.linux.with_arch = _identity_decorator
    utils_pkg.linux.retry = _identity_decorator
    utils_pkg.linux.on_redhat_based = _identity_decorator
    utils_pkg.linux.is_virtual_machine = lambda: False
    utils_pkg.linux.get_max_vm_ipa_size = lambda: 0
    utils_pkg.linux.get_libvirt_version = lambda: "9.0.0"
    utils_pkg.linux.shellquote = lambda value: value
    utils_pkg.linux.TimeoutObject = TimeoutObject

    utils_pkg.qemu.get_version = lambda: "8.2.0"
    utils_pkg.qmp.get_block_node_name_and_file = lambda *args, **kwargs: None
    utils_pkg.qmp.execute_qmp_command = lambda *args, **kwargs: None
    utils_pkg.jsonobject.JsonObject = dict

    class TaskDaemon(object):
        pass

    class FakeConn(object):
        def getCapabilities(self):
            return "<capabilities><host><uuid>uuid</uuid><cpu/><cells/></host>" \
                   "<guest><os_type>hvm</os_type></guest></capabilities>"

    class FakeLibvirtSingleton(object):
        def __init__(self):
            self.conn = FakeConn()
            self.libvirt_event_callbacks = []

    class FakeEventManagerSingleton(object):
        pass

    utils_pkg.plugin.TaskDaemon = TaskDaemon
    utils_pkg.vm_plugin_queue_singleton.VmPluginQueueSingleton = object
    utils_pkg.libvirt_singleton.LibvirtEventManager = object
    utils_pkg.libvirt_singleton.LibvirtEventManagerSingleton = FakeEventManagerSingleton
    utils_pkg.libvirt_singleton.LibvirtSingleton = FakeLibvirtSingleton

    simplejson_mod = _module("simplejson")
    simplejson_mod.loads = json.loads
    simplejson_mod.dumps = json.dumps
    _module("libvirt")
    _module("netaddr")


_install_import_stubs()

from kvmagent.plugins import vm_plugin


class Obj(object):
    pass


class TestVmPluginLuksCbdResize(unittest.TestCase):
    def test_luks_cbd_format_node_uses_requested_size_after_backing_alignment(self):
        vm = vm_plugin.Vm.__new__(vm_plugin.Vm)
        vm.uuid = "vm-uuid"

        volume = Obj()
        volume.deviceType = "cbd"
        volume.deviceId = 1
        volume.volumeUuid = "volume-uuid"

        alias = Obj()
        alias.name_ = "virtio-disk1"
        source = Obj()
        source.name_ = "pool/logical/volume_zbs_:/etc/zbs/client.conf"
        target_disk = Obj()
        target_disk.alias = alias
        target_disk.source = source
        target_disk.encryption = Obj()

        requested_size = 1073741825
        payload_offset = 8 * 1024 * 1024
        aligned_storage_size = requested_size + payload_offset + 1048575
        calls = []

        original_get_vm_blocks = vm_plugin.get_vm_blocks
        original_get_cbd_storage_size = vm_plugin.Vm._get_cbd_storage_size
        original_execute_qmp_command = vm_plugin.qmp.execute_qmp_command
        try:
            vm_plugin.get_vm_blocks = lambda uuid: [{
                "qdev": "/machine/peripheral/virtio-disk1",
                "inserted": {
                    "node-name": "drive-virtio-disk1-format",
                    "image": {
                        "format-specific": {
                            "data": {
                                "payload-offset": payload_offset,
                            },
                        },
                    },
                },
            }]
            vm_plugin.Vm._get_cbd_storage_size = staticmethod(lambda disk: aligned_storage_size)

            def execute_qmp_command(vm_uuid, command, **kwargs):
                calls.append((vm_uuid, command, kwargs))

            vm_plugin.qmp.execute_qmp_command = execute_qmp_command

            vm._resize_luks_cbd_block_node(volume, target_disk, requested_size)

            self.assertEqual(2, len(calls))
            self.assertEqual(("vm-uuid", "block_resize", {
                "node_name": "drive-virtio-disk1-storage",
                "size": aligned_storage_size,
            }), calls[0])
            self.assertEqual(("vm-uuid", "block_resize", {
                "node_name": "drive-virtio-disk1-format",
                "size": requested_size,
            }), calls[1])
        finally:
            vm_plugin.get_vm_blocks = original_get_vm_blocks
            vm_plugin.Vm._get_cbd_storage_size = original_get_cbd_storage_size
            vm_plugin.qmp.execute_qmp_command = original_execute_qmp_command


if __name__ == "__main__":
    unittest.main()
