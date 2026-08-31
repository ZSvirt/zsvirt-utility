import contextlib
import json
import os
import sys
import types
import unittest


def _install_import_stubs():
    package_dir = os.path.dirname(os.path.dirname(__file__))

    kvmagent_pkg = types.ModuleType("kvmagent")
    kvmagent_pkg.__path__ = [package_dir]
    kvmagent_mod = types.ModuleType("kvmagent.kvmagent")

    class AgentCommand(object):
        pass

    class AgentResponse(object):
        def __init__(self):
            self.success = True
            self.error = None

    class KvmAgent(object):
        pass

    kvmagent_mod.AgentCommand = AgentCommand
    kvmagent_mod.AgentResponse = AgentResponse
    kvmagent_mod.KvmAgent = KvmAgent
    kvmagent_mod.replyerror = lambda func: func
    kvmagent_pkg.kvmagent = kvmagent_mod
    sys.modules["kvmagent"] = kvmagent_pkg
    sys.modules["kvmagent.kvmagent"] = kvmagent_mod

    plugins_pkg = types.ModuleType("kvmagent.plugins")
    plugins_pkg.__path__ = [os.path.join(package_dir, "plugins")]
    sys.modules["kvmagent.plugins"] = plugins_pkg

    imagestore_mod = types.ModuleType("kvmagent.plugins.imagestore")
    imagestore_mod.ImageStoreClient = object
    sys.modules["kvmagent.plugins.imagestore"] = imagestore_mod

    volume_secret_mod = types.ModuleType("kvmagent.plugins.volume_secret")

    @contextlib.contextmanager
    def luks_secret_channel(encrypted_dek):
        yield "/var/run/key-agent/secret"

    volume_secret_mod.luks_secret_channel = luks_secret_channel
    plugins_pkg.volume_secret = volume_secret_mod
    sys.modules["kvmagent.plugins.volume_secret"] = volume_secret_mod

    zstacklib_pkg = types.ModuleType("zstacklib")
    utils_pkg = types.ModuleType("zstacklib.utils")
    zstacklib_pkg.utils = utils_pkg
    sys.modules["zstacklib"] = zstacklib_pkg
    sys.modules["zstacklib.utils"] = utils_pkg

    class AttrDict(dict):
        def __getattr__(self, item):
            try:
                return self[item]
            except KeyError:
                raise AttributeError(item)

    def to_attr(value):
        if isinstance(value, dict):
            return AttrDict((key, to_attr(item)) for key, item in value.items())
        if isinstance(value, list):
            return [to_attr(item) for item in value]
        return value

    http_mod = types.ModuleType("zstacklib.utils.http")
    http_mod.REQUEST_BODY = "body"
    utils_pkg.http = http_mod
    sys.modules["zstacklib.utils.http"] = http_mod

    jsonobject_mod = types.ModuleType("zstacklib.utils.jsonobject")
    jsonobject_mod.loads = lambda value: to_attr(json.loads(value))
    jsonobject_mod.dumps = lambda value: json.dumps(value.__dict__)
    utils_pkg.jsonobject = jsonobject_mod
    sys.modules["zstacklib.utils.jsonobject"] = jsonobject_mod

    linux_mod = types.ModuleType("zstacklib.utils.linux")
    linux_mod.rm_file_force = lambda path: None
    utils_pkg.linux = linux_mod
    sys.modules["zstacklib.utils.linux"] = linux_mod

    class Logger(object):
        def warn(self, message):
            pass

    log_mod = types.ModuleType("zstacklib.utils.log")
    log_mod.get_logger = lambda name: Logger()
    utils_pkg.log = log_mod
    sys.modules["zstacklib.utils.log"] = log_mod

    shell_mod = types.ModuleType("zstacklib.utils.shell")
    shell_mod.call = lambda command: ""
    utils_pkg.shell = shell_mod
    sys.modules["zstacklib.utils.shell"] = shell_mod

    qemu_img_mod = types.ModuleType("zstacklib.utils.qemu_img")
    utils_pkg.qemu_img = qemu_img_mod
    sys.modules["zstacklib.utils.qemu_img"] = qemu_img_mod


_install_import_stubs()

from kvmagent.plugins import ceph_storage_plugin
from zstacklib.utils import jsonobject


class TestCephStoragePluginLuksConvert(unittest.TestCase):
    def _call_luks_convert(self, plugin, target_encrypted):
        return jsonobject.loads(plugin.luks_convert({
            "body": json.dumps({
                "psUuid": "ps-uuid",
                "installPath": "ceph://pool/source",
                "targetInstallPath": "ceph://pool/target",
                "targetEncrypted": target_encrypted,
                "encryptedDek": "sealed-dek"
            })
        }))

    def _assert_delegates_to_linux_helper(self, source_is_luks, target_encrypted,
                                          expected_source, expected_target_format):
        plugin = ceph_storage_plugin.CephStoragePlugin()
        plugin._validate_luks_cmd = lambda cmd, rsp, encrypted_dek=False: \
            "/var/lib/zstack/ceph/ps-uuid/ceph.conf"
        plugin._is_luks_rbd = lambda install_path, conf_path: source_is_luks
        calls = []
        helper_name = "convert_volume_encryption"
        original_helper = getattr(ceph_storage_plugin.linux, helper_name, None)
        try:
            setattr(ceph_storage_plugin.linux, helper_name,
                    lambda *args, **kwargs: calls.append((args, kwargs)))

            rsp = self._call_luks_convert(plugin, target_encrypted)

            self.assertTrue(rsp.success)
            self.assertEqual("ceph://pool/target", rsp.installPath)
            self.assertEqual(1, len(calls))
            args, kwargs = calls[0]
            self.assertEqual(expected_source, args[0])
            self.assertEqual(
                "rbd:pool/target:conf=/var/lib/zstack/ceph/ps-uuid/ceph.conf:"
                "rbd_cache=false:rbd_concurrent_management_ops=20", args[1])
            self.assertEqual("/var/run/key-agent/secret", args[2])
            self.assertIs(ceph_storage_plugin.shell.call, args[3])
            self.assertEqual(expected_target_format, kwargs["target_format_options"])
            self.assertNotIn("target_is_precreated", kwargs)
            self.assertNotIn("use_target_image_opts", kwargs)
        finally:
            if original_helper is None:
                delattr(ceph_storage_plugin.linux, helper_name)
            else:
                setattr(ceph_storage_plugin.linux, helper_name, original_helper)

    def test_plain_to_encrypted_delegates_to_linux_helper(self):
        self._assert_delegates_to_linux_helper(
            False, True,
            "-f raw rbd:pool/source:conf=/var/lib/zstack/ceph/ps-uuid/ceph.conf",
            "-O luks -o key-secret=luks_sec")

    def test_encrypted_to_plain_delegates_to_linux_helper(self):
        self._assert_delegates_to_linux_helper(
            True, False,
            "--image-opts driver=luks,key-secret=luks_sec,file.driver=rbd,"
            "file.pool=pool,file.image=source,file.conf=/var/lib/zstack/ceph/ps-uuid/ceph.conf",
            "-O raw")


if __name__ == "__main__":
    unittest.main()
