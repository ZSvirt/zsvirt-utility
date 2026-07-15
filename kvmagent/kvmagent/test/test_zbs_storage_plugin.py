import unittest
import contextlib
import json
import os
import sys
import types


def _install_import_stubs():
    package_dir = os.path.dirname(os.path.dirname(__file__))

    kvmagent_pkg = types.ModuleType("kvmagent")
    kvmagent_pkg.__path__ = [package_dir]
    kvmagent_mod = types.ModuleType("kvmagent.kvmagent")

    class KvmAgent(object):
        pass

    class AgentResponse(object):
        def __init__(self):
            self.success = True
            self.error = None

    kvmagent_mod.KvmAgent = KvmAgent
    kvmagent_mod.AgentResponse = AgentResponse
    kvmagent_mod.replyerror = lambda func: func
    kvmagent_mod.get_http_server = lambda: None
    kvmagent_pkg.kvmagent = kvmagent_mod
    sys.modules["kvmagent"] = kvmagent_pkg
    sys.modules["kvmagent.kvmagent"] = kvmagent_mod

    zstacklib_pkg = types.ModuleType("zstacklib")
    zstacklib_utils_pkg = types.ModuleType("zstacklib.utils")
    sys.modules["zstacklib"] = zstacklib_pkg
    sys.modules["zstacklib.utils"] = zstacklib_utils_pkg

    http_mod = types.ModuleType("zstacklib.utils.http")
    http_mod.REQUEST_BODY = "body"
    zstacklib_utils_pkg.http = http_mod
    sys.modules["zstacklib.utils.http"] = http_mod

    class AttrDict(dict):
        def __getattr__(self, item):
            return self[item]

        def hasattr(self, item):
            return item in self

    def to_attr(value):
        if isinstance(value, dict):
            ret = AttrDict()
            for k, v in value.items():
                ret[k] = to_attr(v)
            return ret
        if isinstance(value, list):
            return [to_attr(v) for v in value]
        return value

    jsonobject_mod = types.ModuleType("zstacklib.utils.jsonobject")
    jsonobject_mod.loads = lambda value: to_attr(json.loads(value)) if isinstance(value, str) else to_attr(value)
    jsonobject_mod.dumps = lambda value: json.dumps(value if isinstance(value, dict) else value.__dict__)
    zstacklib_utils_pkg.jsonobject = jsonobject_mod
    sys.modules["zstacklib.utils.jsonobject"] = jsonobject_mod

    log_mod = types.ModuleType("zstacklib.utils.log")

    class Logger(object):
        def warn(self, msg):
            pass

        def debug(self, msg):
            pass

    log_mod.get_logger = lambda name: Logger()
    zstacklib_utils_pkg.log = log_mod
    sys.modules["zstacklib.utils.log"] = log_mod

    bash_mod = types.ModuleType("zstacklib.utils.bash")
    bash_mod.bash_errorout = lambda cmd: ""
    bash_mod.bash_roe = lambda cmd: (0, "", "")
    bash_mod.in_bash = lambda func: func
    zstacklib_utils_pkg.bash = bash_mod
    sys.modules["zstacklib.utils.bash"] = bash_mod

    linux_mod = types.ModuleType("zstacklib.utils.linux")
    linux_mod.shellquote = lambda value: value
    linux_mod.rm_file_force = lambda path: None
    linux_mod.get_exception_stacktrace = lambda: ""
    linux_mod.catch_bad_alloc_exception = lambda ret, err: False
    linux_mod.read_luks_secret_material_file = lambda path: b"secret-material"

    def convert_volume_encryption(source_image_arg, target_arg, secret_file_arg, command_runner,
                                  target_format_options=None, target_is_precreated=False,
                                  use_target_image_opts=False):
        options = []
        if target_is_precreated:
            options.append("-n")
        if use_target_image_opts:
            options.append("--target-image-opts")
        options.append("--object secret,id=luks_sec,format=raw,file=%s" % secret_file_arg)
        options.extend(["-m 16 -W", source_image_arg])
        if target_format_options:
            options.append(target_format_options)
        options.append(target_arg)
        return command_runner("/usr/bin/qemu-img convert %s" % " ".join(options))

    linux_mod.convert_volume_encryption = convert_volume_encryption

    @contextlib.contextmanager
    def temporary_luks_secret_file(secret_material):
        yield "/tmp/luks-secret-persistent"

    linux_mod.temporary_luks_secret_file = temporary_luks_secret_file
    zstacklib_utils_pkg.linux = linux_mod
    sys.modules["zstacklib.utils.linux"] = linux_mod

    plugins_pkg = types.ModuleType("kvmagent.plugins")
    plugins_pkg.__path__ = [os.path.join(package_dir, "plugins")]
    sys.modules["kvmagent.plugins"] = plugins_pkg

    volume_secret_mod = types.ModuleType("kvmagent.plugins.volume_secret")

    @contextlib.contextmanager
    def luks_secret_channel(encrypted_dek):
        yield "/tmp/luks-secret"

    volume_secret_mod.luks_secret_channel = luks_secret_channel
    plugins_pkg.volume_secret = volume_secret_mod
    sys.modules["kvmagent.plugins.volume_secret"] = volume_secret_mod


_install_import_stubs()

from kvmagent.plugins import zbs_storage_plugin
from zstacklib.utils import jsonobject


class TestZbsStoragePlugin(unittest.TestCase):
    def _call_luks_convert(self, plugin, target_encrypted):
        return jsonobject.loads(plugin.luks_convert({
            "body": json.dumps({
                "psUuid": "ps-uuid",
                "installPath": "cbd:physical/logical/source",
                "targetInstallPath": "cbd:physical/logical/target",
                "targetEncrypted": target_encrypted,
                "virtualSize": 13631488,
                "encryptedDek": "sealed-dek"
            })
        }))

    def test_start_registers_luks_convert_path(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        paths = []

        class HttpServer(object):
            def register_async_uri(self, path, handler):
                paths.append((path, handler.__name__))

        original_get_http_server = zbs_storage_plugin.kvmagent.get_http_server
        try:
            zbs_storage_plugin.kvmagent.get_http_server = lambda: HttpServer()

            plugin.start()

            self.assertEqual(1, paths.count((plugin.LUKS_CONVERT_PATH, "luks_convert")))
        finally:
            zbs_storage_plugin.kvmagent.get_http_server = original_get_http_server

    def test_start_registers_luks_resize_path(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        paths = []

        class HttpServer(object):
            def register_async_uri(self, path, handler):
                paths.append((path, handler.__name__))

        original_get_http_server = zbs_storage_plugin.kvmagent.get_http_server
        try:
            zbs_storage_plugin.kvmagent.get_http_server = lambda: HttpServer()

            plugin.start()

            self.assertIn((plugin.LUKS_RESIZE_PATH, "luks_resize"), paths)
        finally:
            zbs_storage_plugin.kvmagent.get_http_server = original_get_http_server

    def test_snapshot_install_path_is_passed_to_qemu_without_zbs_cli_lookup(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()

        path = plugin._cbd_qemu_path("cbd:physical/logical/volume@42")

        self.assertEqual("cbd:physical/logical/volume@42_zbs_:/etc/zbs/client.conf", path)

    def test_snapshot_path_adds_cbd_prefix_without_zbs_cli_lookup(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()

        path = plugin._cbd_qemu_path("physical/logical/volume@1")

        self.assertEqual("cbd:physical/logical/volume@1_zbs_:/etc/zbs/client.conf", path)

    def test_luks_convert_plain_to_encrypted_initializes_fixed_offset_target(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._new_luks_temp_path = lambda: "/tmp/zbs-luks-convert.img"
            plugin._raw_cbd_actual_size = lambda install_path: 0

            rsp = self._call_luks_convert(plugin, True)

            self.assertTrue(rsp.success)
            self.assertIn("/usr/bin/truncate -s 22020096 /tmp/zbs-luks-convert.img", commands[0])
            self.assertIn("--align-payload=16384", commands[1])
            self.assertIn("convert -n --target-image-opts", commands[-1])
            self.assertIn("driver=luks,key-secret=luks_sec", commands[-1])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_convert_encrypted_to_plain_uses_luks_source_image_opts(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            plugin._raw_cbd_actual_size = lambda install_path: 0

            rsp = self._call_luks_convert(plugin, False)

            self.assertTrue(rsp.success)
            self.assertEqual(1, len(commands))
            self.assertIn("convert -n", commands[0])
            self.assertIn("--image-opts driver=luks,key-secret=luks_sec", commands[0])
            self.assertIn("-O raw", commands[0])
            self.assertNotIn("truncate", commands[0])
            self.assertNotIn("cryptsetup", commands[0])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_convert_plain_to_encrypted_delegates_copy_to_linux_helper(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        helper_name = "convert_volume_encryption"
        original_helper = getattr(zbs_storage_plugin.linux, helper_name, None)
        try:
            setattr(zbs_storage_plugin.linux, helper_name,
                    lambda *args, **kwargs: calls.append((args, kwargs)))
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._initialize_luks_cbd_volume = lambda install_path, size, secret_file: None
            plugin._raw_cbd_actual_size = lambda install_path: 0

            rsp = self._call_luks_convert(plugin, True)

            self.assertTrue(rsp.success)
            self.assertEqual(1, len(calls))
            args, kwargs = calls[0]
            self.assertIn("-f raw cbd:physical/logical/source_zbs_:/etc/zbs/client.conf", args[0])
            self.assertIn("driver=luks,key-secret=luks_sec,file.driver=cbd", args[1])
            self.assertEqual("/tmp/luks-secret", args[2])
            self.assertIs(zbs_storage_plugin.bash.bash_errorout, args[3])
            self.assertTrue(kwargs["target_is_precreated"])
            self.assertTrue(kwargs["use_target_image_opts"])
            self.assertNotIn("target_format_options", kwargs)
        finally:
            if original_helper is None:
                delattr(zbs_storage_plugin.linux, helper_name)
            else:
                setattr(zbs_storage_plugin.linux, helper_name, original_helper)

    def test_luks_convert_encrypted_to_plain_delegates_copy_to_linux_helper(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        helper_name = "convert_volume_encryption"
        original_helper = getattr(zbs_storage_plugin.linux, helper_name, None)
        try:
            setattr(zbs_storage_plugin.linux, helper_name,
                    lambda *args, **kwargs: calls.append((args, kwargs)))
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            plugin._raw_cbd_actual_size = lambda install_path: 0

            rsp = self._call_luks_convert(plugin, False)

            self.assertTrue(rsp.success)
            self.assertEqual(1, len(calls))
            args, kwargs = calls[0]
            self.assertIn("--image-opts driver=luks,key-secret=luks_sec", args[0])
            self.assertEqual("cbd:physical/logical/target_zbs_:/etc/zbs/client.conf", args[1])
            self.assertEqual("/tmp/luks-secret", args[2])
            self.assertIs(zbs_storage_plugin.bash.bash_errorout, args[3])
            self.assertEqual("-O raw", kwargs["target_format_options"])
            self.assertTrue(kwargs["target_is_precreated"])
            self.assertNotIn("use_target_image_opts", kwargs)
        finally:
            if original_helper is None:
                delattr(zbs_storage_plugin.linux, helper_name)
            else:
                setattr(zbs_storage_plugin.linux, helper_name, original_helper)

    def test_luks_convert_requires_source_target_dek_and_virtual_size(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            valid = {
                "psUuid": "ps-uuid",
                "installPath": "cbd:physical/logical/source",
                "targetInstallPath": "cbd:physical/logical/target",
                "targetEncrypted": True,
                "virtualSize": 13631488,
                "encryptedDek": "sealed-dek"
            }
            cases = []
            for field in ["installPath", "targetInstallPath", "encryptedDek"]:
                body = dict(valid)
                del body[field]
                cases.append(body)
            body = dict(valid)
            body["virtualSize"] = 0
            cases.append(body)

            for body in cases:
                rsp = jsonobject.loads(plugin.luks_convert({"body": json.dumps(body)}))

                self.assertFalse(rsp.success)

            self.assertFalse(any("qemu-img convert" in cmd for cmd in commands))
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_convert_requires_boolean_target_encrypted_before_probing_source(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        probe_calls = []
        original_helper = zbs_storage_plugin.linux.convert_volume_encryption
        zbs_storage_plugin.linux.convert_volume_encryption = lambda *args, **kwargs: None
        plugin._initialize_luks_cbd_volume = lambda install_path, size, secret_file: None
        plugin._raw_cbd_actual_size = lambda install_path: 0
        valid = {
            "psUuid": "ps-uuid",
            "installPath": "cbd:physical/logical/source",
            "targetInstallPath": "cbd:physical/logical/target",
            "virtualSize": 13631488,
            "encryptedDek": "sealed-dek"
        }
        cases = [(dict(valid), True), (dict(valid, targetEncrypted="false"), False)]
        try:
            for body, source_is_luks in cases:
                plugin._is_luks_volume = lambda install_path, encrypted_dek=None, value=source_is_luks: \
                    probe_calls.append(install_path) or value
                rsp = jsonobject.loads(plugin.luks_convert({"body": json.dumps(body)}))

                self.assertFalse(rsp.success)
                self.assertIn("targetEncrypted", rsp.error)
            self.assertEqual([], probe_calls)
        finally:
            zbs_storage_plugin.linux.convert_volume_encryption = original_helper

    def test_luks_convert_rejects_source_format_mismatch_before_writing(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            for source_is_luks, target_encrypted in [(False, False), (True, True)]:
                plugin._is_luks_volume = lambda install_path, encrypted_dek=None, value=source_is_luks: value

                rsp = self._call_luks_convert(plugin, target_encrypted)

                self.assertFalse(rsp.success)
                self.assertIn("source format does not match", rsp.error)

            self.assertEqual([], commands)
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_convert_failure_returns_error_without_deleting_cbd_paths(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            def fail_convert(cmd):
                commands.append(cmd)
                raise Exception("qemu-img conversion failed")

            zbs_storage_plugin.bash.bash_errorout = fail_convert
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            source = "cbd:physical/logical/source"
            target = "cbd:physical/logical/target"

            rsp = jsonobject.loads(plugin.luks_convert({
                "body": json.dumps({
                    "psUuid": "ps-uuid",
                    "installPath": source,
                    "targetInstallPath": target,
                    "targetEncrypted": False,
                    "virtualSize": 13631488,
                    "encryptedDek": "sealed-dek"
                })
            }))

            self.assertFalse(rsp.success)
            self.assertIn(source, rsp.error)
            self.assertIn(target, rsp.error)
            self.assertFalse(any("delete" in cmd.lower() for cmd in commands))
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_convert_reports_target_actual_size(self):
        target = "cbd:physical/logical/target"
        for target_encrypted in (True, False):
            plugin = zbs_storage_plugin.ZbsStoragePlugin()
            calls = []
            plugin._is_luks_volume = \
                lambda install_path, encrypted_dek=None, value=target_encrypted: not value
            plugin._initialize_luks_cbd_volume = lambda install_path, size, secret_file: None
            original_helper = zbs_storage_plugin.linux.convert_volume_encryption
            zbs_storage_plugin.linux.convert_volume_encryption = (
                lambda *args, **kwargs: calls.append(("convert", target_encrypted)))
            plugin._raw_cbd_actual_size = lambda install_path: calls.append(
                ("actual-size", install_path)) or 4096
            try:
                rsp = self._call_luks_convert(plugin, target_encrypted)

                self.assertTrue(rsp.success)
                self.assertEqual(4096, rsp.actualSize)
                self.assertEqual([
                    ("convert", target_encrypted),
                    ("actual-size", target)
                ], calls)
            finally:
                zbs_storage_plugin.linux.convert_volume_encryption = original_helper

    def test_luks_clone_preserves_luks_source_bits(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            plugin._raw_cbd_size = lambda install_path: 4096
            plugin._resize_luks_target = lambda install_path, virtual_size, encrypted_dek: None

            plugin._clone_plain_to_luks("cbd:physical/logical/source", "cbd:physical/logical/target",
                                        "sealed-dek", 1024)

            self.assertEqual(1, len(commands))
            self.assertIn("-n", commands[0])
            self.assertIn("-f raw", commands[0])
            self.assertIn("-O raw", commands[0])
            self.assertNotIn("-O luks", commands[0])
            self.assertNotIn("key-secret=luks_sec", commands[0])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_clone_converts_plain_source_to_luks_target(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        removed = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        original_rm = zbs_storage_plugin.linux.rm_file_force
        original_read_secret = zbs_storage_plugin.linux.read_luks_secret_material_file
        original_temp_secret = zbs_storage_plugin.linux.temporary_luks_secret_file
        original_secret_channel = zbs_storage_plugin.volume_secret.luks_secret_channel
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            zbs_storage_plugin.linux.rm_file_force = lambda path: removed.append(path)
            secret_material_sources = []

            def read_secret(path):
                secret_material_sources.append(path)
                return b"persistent-secret-material"

            @contextlib.contextmanager
            def temp_secret(secret_material):
                self.assertEqual(b"persistent-secret-material", secret_material)
                try:
                    yield "/tmp/luks-secret-persistent"
                finally:
                    removed.append("/tmp/luks-secret-persistent")

            zbs_storage_plugin.linux.read_luks_secret_material_file = read_secret
            zbs_storage_plugin.linux.temporary_luks_secret_file = temp_secret
            secret_paths = iter(["/tmp/luks-secret-create", "/tmp/luks-secret-convert"])

            @contextlib.contextmanager
            def secret_channel(encrypted_dek):
                yield next(secret_paths)

            zbs_storage_plugin.volume_secret.luks_secret_channel = secret_channel
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._new_luks_temp_path = lambda: "/tmp/zbs-luks-test.img"
            plugin._resize_luks_target = lambda install_path, virtual_size, encrypted_dek: None

            plugin._clone_plain_to_luks("cbd:physical/logical/source", "cbd:physical/logical/target",
                                        "sealed-dek", 13631488)

            self.assertEqual(4, len(commands))
            self.assertIn("/usr/bin/truncate -s 22020096 /tmp/zbs-luks-test.img", commands[0])
            self.assertIn("/usr/sbin/cryptsetup -q luksFormat", commands[1])
            self.assertIn("--type luks1", commands[1])
            self.assertIn("--align-payload=16384", commands[1])
            self.assertIn("--key-file /tmp/luks-secret-persistent", commands[1])
            self.assertIn("/tmp/zbs-luks-test.img", commands[1])
            self.assertIn("convert -n --target-is-zero -S 4k", commands[2])
            self.assertIn("-O raw", commands[2])
            self.assertIn("/tmp/zbs-luks-test.img", commands[2])
            self.assertIn("convert -n --target-image-opts", commands[3])
            self.assertIn("driver=luks,key-secret=luks_sec,file.driver=cbd", commands[3])
            self.assertIn("file=/tmp/luks-secret-convert", commands[3])
            self.assertIn("cbd:physical/logical/target_zbs_:/etc/zbs/client.conf", commands[3])
            self.assertNotIn("/tmp/zbs-luks-test.img", commands[3])
            self.assertEqual(["/tmp/luks-secret-create"], secret_material_sources)
            self.assertEqual(["/tmp/luks-secret-persistent", "/tmp/zbs-luks-test.img"], removed)
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout
            zbs_storage_plugin.linux.rm_file_force = original_rm
            zbs_storage_plugin.linux.read_luks_secret_material_file = original_read_secret
            zbs_storage_plugin.linux.temporary_luks_secret_file = original_temp_secret
            zbs_storage_plugin.volume_secret.luks_secret_channel = original_secret_channel

    def test_luks_clone_assumes_target_exists_without_zbs_cli(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            def run(cmd):
                commands.append(cmd)
                if cmd.startswith("/usr/bin/zbs"):
                    raise AssertionError("kvmagent must not execute zbs CLI")
                return ""

            zbs_storage_plugin.bash.bash_errorout = run
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._new_luks_temp_path = lambda: "/tmp/zbs-luks-test.img"
            plugin._resize_luks_target = lambda install_path, virtual_size, encrypted_dek: None

            plugin._clone_plain_to_luks("cbd:physical/logical/source", "cbd:physical/logical/target",
                                        "sealed-dek", 13631488)

            self.assertEqual(4, len(commands))
            self.assertTrue(all(not cmd.startswith("/usr/bin/zbs") for cmd in commands))
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_create_empty_formats_local_luks_before_copying_to_cbd(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        removed = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        original_rm = zbs_storage_plugin.linux.rm_file_force
        original_temp_secret = zbs_storage_plugin.linux.temporary_luks_secret_file
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""
            zbs_storage_plugin.linux.rm_file_force = lambda path: removed.append(path)

            @contextlib.contextmanager
            def temp_secret(secret_material):
                try:
                    yield "/tmp/luks-secret-persistent"
                finally:
                    removed.append("/tmp/luks-secret-persistent")

            zbs_storage_plugin.linux.temporary_luks_secret_file = temp_secret
            plugin._new_luks_temp_path = lambda: "/tmp/zbs-luks-empty.img"

            plugin._create_luks_volume("cbd:physical/logical/target", 13631488, "sealed-dek")

            self.assertEqual(3, len(commands))
            self.assertIn("/usr/bin/truncate -s 22020096 /tmp/zbs-luks-empty.img", commands[0])
            self.assertIn("/usr/sbin/cryptsetup -q luksFormat", commands[1])
            self.assertIn("--align-payload=16384", commands[1])
            self.assertIn("--key-file /tmp/luks-secret-persistent", commands[1])
            self.assertIn("/tmp/zbs-luks-empty.img", commands[1])
            self.assertIn("convert -n", commands[2])
            self.assertIn("-O raw", commands[2])
            self.assertIn("cbd:physical/logical/target_zbs_:/etc/zbs/client.conf", commands[2])
            self.assertEqual(["/tmp/luks-secret-persistent", "/tmp/zbs-luks-empty.img"], removed)
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout
            zbs_storage_plugin.linux.rm_file_force = original_rm
            zbs_storage_plugin.linux.temporary_luks_secret_file = original_temp_secret

    def test_luks_create_empty_assumes_target_exists_without_zbs_cli(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            def run(cmd):
                commands.append(cmd)
                if cmd.startswith("/usr/bin/zbs"):
                    raise AssertionError("kvmagent must not execute zbs CLI")
                return ""

            zbs_storage_plugin.bash.bash_errorout = run
            plugin._new_luks_temp_path = lambda: "/tmp/zbs-luks-empty.img"

            plugin._create_luks_volume("cbd:physical/logical/target", 13631488, "sealed-dek")

            self.assertEqual(3, len(commands))
            self.assertTrue(all(not cmd.startswith("/usr/bin/zbs") for cmd in commands))
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_create_empty_rejects_invalid_size_before_formatting(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or ""

            with self.assertRaises(Exception) as ctx:
                plugin._create_luks_volume("cbd:physical/logical/target", 0, "sealed-dek")

            self.assertIn("size is required and must be greater than 0", str(ctx.exception))
            self.assertEqual([], commands)
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_clone_stops_when_source_format_probe_fails(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            def fail_probe(cmd):
                commands.append(cmd)
                raise Exception("probe failed")

            zbs_storage_plugin.bash.bash_errorout = fail_probe

            with self.assertRaises(Exception):
                plugin._clone_plain_to_luks("pool/source", "pool/target", "sealed-dek", 1024)

            self.assertEqual(1, len(commands))
            self.assertIn("qemu-img info", commands[0])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_handlers_return_failure_when_required_path_is_missing(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        cases = [
            (plugin.luks_clone, {"encryptedDek": "sealed-dek"}, "failed to clone ZBS LUKS volume"),
            (plugin.luks_create_empty, {"size": 1024, "encryptedDek": "sealed-dek"},
             "failed to create empty ZBS LUKS volume"),
            (plugin.luks_encrypt_in_place, {"encryptedDek": "sealed-dek"},
             "failed to encrypt ZBS volume"),
            (plugin.luks_resize, {"virtualSize": 2048, "encryptedDek": "sealed-dek"},
             "failed to resize ZBS LUKS volume"),
        ]

        for handler, body, err in cases:
            rsp = jsonobject.loads(handler({"body": json.dumps(body)}))

            self.assertFalse(rsp.success)
            self.assertIn(err, rsp.error)
            self.assertIn("<missing>", rsp.error)

    def test_luks_resize_expands_raw_cbd_before_luks_virtual_size(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        sizes = iter([1024, 1024, 2048])
        try:
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            plugin._raw_cbd_size = lambda install_path: 4096
            plugin._resize_raw_cbd_if_needed = lambda install_path, required_size: commands.append(
                "raw-resize %s %s" % (install_path, required_size))

            def info(cmd):
                commands.append(cmd)
                return json.dumps({"virtual-size": next(sizes)})

            zbs_storage_plugin.bash.bash_errorout = info

            plugin._resize_luks_target("cbd:physical/logical/volume", 2048, "sealed-dek")

            self.assertEqual(4, len(commands))
            self.assertIn("qemu-img info", commands[0])
            self.assertEqual("raw-resize cbd:physical/logical/volume 5120", commands[1])
            self.assertIn("qemu-img info", commands[2])
            self.assertIn("qemu-img resize", commands[3])
            self.assertIn("2048", commands[3])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_resize_does_not_shrink(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        commands = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
            zbs_storage_plugin.bash.bash_errorout = lambda cmd: commands.append(cmd) or json.dumps({"virtual-size": 2048})

            plugin._resize_luks_target("cbd:physical/logical/volume", 1024, "sealed-dek")

            self.assertEqual(1, len(commands))
            self.assertIn("qemu-img info", commands[0])
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout

    def test_luks_resize_handler_resizes_target_and_reports_actual_size(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        try:
            plugin._resize_luks_target = lambda install_path, virtual_size, encrypted_dek: calls.append(
                (install_path, virtual_size, encrypted_dek))
            plugin._raw_cbd_actual_size = lambda install_path: 4096

            rsp = jsonobject.loads(plugin.luks_resize({
                "body": json.dumps({
                    "installPath": "cbd:physical/logical/volume",
                    "virtualSize": 2048,
                    "encryptedDek": "sealed-dek"
                })
            }))

            self.assertTrue(rsp.success)
            self.assertEqual(4096, rsp.actualSize)
            self.assertEqual([("cbd:physical/logical/volume", 2048, "sealed-dek")], calls)
        finally:
            pass

    def test_luks_resize_handler_requires_positive_virtual_size(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        plugin._resize_luks_target = lambda install_path, virtual_size, encrypted_dek: calls.append(
            (install_path, virtual_size, encrypted_dek))

        for body in [
            {"installPath": "cbd:physical/logical/volume", "encryptedDek": "sealed-dek"},
            {"installPath": "cbd:physical/logical/volume", "virtualSize": 0, "encryptedDek": "sealed-dek"},
        ]:
            rsp = jsonobject.loads(plugin.luks_resize({"body": json.dumps(body)}))

            self.assertFalse(rsp.success)
            self.assertIn("virtualSize is required", rsp.error)

        self.assertEqual([], calls)

    def test_luks_encrypt_in_place_returns_new_install_path_without_replacing_original(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        try:
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._clone_plain_to_luks = lambda src_path, dst_path, encrypted_dek: calls.append(
                ("clone", src_path, dst_path, encrypted_dek))

            new_path = plugin._encrypt_luks_in_place("cbd:physical/logical/volume",
                                                     "cbd:physical/logical/volume-encrypted-new",
                                                     "sealed-dek")

            self.assertEqual("cbd:physical/logical/volume-encrypted-new", new_path)
            self.assertEqual([
                ("clone", "cbd:physical/logical/volume", new_path, "sealed-dek")
            ], calls)
        finally:
            pass

    def test_luks_encrypt_in_place_fails_when_bits_are_already_luks(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        plugin._is_luks_volume = lambda install_path, encrypted_dek=None: True
        plugin._clone_plain_to_luks = lambda src_path, dst_path, encrypted_dek: calls.append(
            ("clone", src_path, dst_path, encrypted_dek))

        with self.assertRaises(Exception) as ctx:
            plugin._encrypt_luks_in_place("cbd:physical/logical/volume",
                                          "cbd:physical/logical/volume-encrypted-new",
                                          "sealed-dek")

        self.assertIn("already LUKS", str(ctx.exception))
        self.assertEqual([], calls)

    def test_luks_encrypt_in_place_handler_reports_new_install_path(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        try:
            plugin._encrypt_luks_in_place = lambda install_path, target_install_path, encrypted_dek: target_install_path
            plugin._raw_cbd_actual_size = lambda install_path: 4096

            rsp = jsonobject.loads(plugin.luks_encrypt_in_place({
                "body": json.dumps({
                    "installPath": "cbd:physical/logical/volume",
                    "targetInstallPath": "cbd:physical/logical/volume-encrypted-new",
                    "encryptedDek": "sealed-dek"
                })
            }))

            self.assertTrue(rsp.success)
            self.assertEqual("cbd:physical/logical/volume-encrypted-new", rsp.installPath)
            self.assertEqual(4096, rsp.actualSize)
        finally:
            pass

    def test_luks_encrypt_in_place_handler_uses_supplied_target_without_zbs_cli(self):
        plugin = zbs_storage_plugin.ZbsStoragePlugin()
        calls = []
        original_errorout = zbs_storage_plugin.bash.bash_errorout
        try:
            def run(cmd):
                if cmd.startswith("/usr/bin/zbs"):
                    raise AssertionError("kvmagent must not execute zbs CLI")
                return ""

            zbs_storage_plugin.bash.bash_errorout = run
            plugin._is_luks_volume = lambda install_path, encrypted_dek=None: False
            plugin._clone_plain_to_luks = lambda src_path, dst_path, encrypted_dek: calls.append(
                ("clone", src_path, dst_path, encrypted_dek))
            plugin._raw_cbd_actual_size = lambda install_path: 4096

            rsp = jsonobject.loads(plugin.luks_encrypt_in_place({
                "body": json.dumps({
                    "installPath": "cbd:physical/logical/source",
                    "targetInstallPath": "cbd:physical/logical/target",
                    "encryptedDek": "sealed-dek"
                })
            }))

            self.assertTrue(rsp.success)
            self.assertEqual("cbd:physical/logical/target", rsp.installPath)
            self.assertEqual([("clone", "cbd:physical/logical/source", "cbd:physical/logical/target",
                               "sealed-dek")], calls)
        finally:
            zbs_storage_plugin.bash.bash_errorout = original_errorout


if __name__ == "__main__":
    unittest.main()
