import os
import shlex
import shutil
import tempfile
import unittest

from zstacklib.utils import linux


class TestConvertVolumeEncryption(unittest.TestCase):
    def setUp(self):
        self.commands = []

    def convert(self, *args, **kwargs):
        converter = getattr(linux, "convert_volume_encryption", None)
        self.assertTrue(callable(converter))
        converter(*args, **kwargs)

    def test_writes_into_precreated_luks_target_image_opts(self):
        self.convert(
            "-f raw cbd:source",
            "'driver=luks,file.driver=cbd,file.filename=cbd:target'",
            "'/tmp/luks-secret'", self.commands.append,
            target_is_precreated=True, use_target_image_opts=True)

        self.assertEqual([
            "/usr/bin/qemu-img convert -n --target-image-opts "
            "--object secret,id=luks_sec,format=raw,file='/tmp/luks-secret' "
            "-m 16 -W -f raw cbd:source "
            "'driver=luks,file.driver=cbd,file.filename=cbd:target'"
        ], self.commands)

    def test_writes_into_precreated_raw_target_path(self):
        self.convert(
            "--image-opts driver=luks,file.driver=cbd,file.filename=cbd:source",
            "'cbd:target'", "'/tmp/luks-secret'", self.commands.append,
            target_format_options="-O raw", target_is_precreated=True)

        self.assertEqual([
            "/usr/bin/qemu-img convert -n "
            "--object secret,id=luks_sec,format=raw,file='/tmp/luks-secret' "
            "-m 16 -W --image-opts driver=luks,file.driver=cbd,file.filename=cbd:source "
            "-O raw 'cbd:target'"
        ], self.commands)

    def test_writes_luks_rbd_target(self):
        self.convert(
            "-f raw rbd:pool/source:conf=/var/lib/zstack/ceph/ps/ceph.conf",
            "rbd:pool/target:conf=/var/lib/zstack/ceph/ps/ceph.conf:"
            "rbd_cache=false:rbd_concurrent_management_ops=20",
            "/var/run/key-agent/secret", self.commands.append,
            target_format_options="-O luks -o key-secret=luks_sec")

        self.assertEqual([
            "/usr/bin/qemu-img convert "
            "--object secret,id=luks_sec,format=raw,file=/var/run/key-agent/secret "
            "-m 16 -W -f raw rbd:pool/source:conf=/var/lib/zstack/ceph/ps/ceph.conf "
            "-O luks -o key-secret=luks_sec "
            "rbd:pool/target:conf=/var/lib/zstack/ceph/ps/ceph.conf:"
            "rbd_cache=false:rbd_concurrent_management_ops=20"
        ], self.commands)

    def test_writes_raw_rbd_target(self):
        self.convert(
            "--image-opts driver=luks,key-secret=luks_sec,file.driver=rbd,"
            "file.pool=pool,file.image=source,file.conf=/var/lib/zstack/ceph/ps/ceph.conf",
            "rbd:pool/target:conf=/var/lib/zstack/ceph/ps/ceph.conf:"
            "rbd_cache=false:rbd_concurrent_management_ops=20",
            "/var/run/key-agent/secret", self.commands.append,
            target_format_options="-O raw")

        self.assertEqual([
            "/usr/bin/qemu-img convert "
            "--object secret,id=luks_sec,format=raw,file=/var/run/key-agent/secret "
            "-m 16 -W "
            "--image-opts driver=luks,key-secret=luks_sec,file.driver=rbd,"
            "file.pool=pool,file.image=source,file.conf=/var/lib/zstack/ceph/ps/ceph.conf "
            "-O raw rbd:pool/target:conf=/var/lib/zstack/ceph/ps/ceph.conf:"
            "rbd_cache=false:rbd_concurrent_management_ops=20"
        ], self.commands)


class TestConvertQcow2VolumeEncryption(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.src = os.path.join(self.workdir, "source.qcow2")
        self.dst = os.path.join(self.workdir, "target.qcow2")
        with open(self.src, "wb") as fd:
            fd.write("source")

        self.commands = []
        self.convert_targets = []
        self.original_check_run = linux.shell.check_run
        self.original_chain_has_luks = linux._qcow2_chain_has_luks_encrypted_image
        self.original_is_block_device = linux._is_block_device
        self.original_qemu_img_subcmd = linux.qemu_img.subcmd
        self.original_chmod = os.chmod
        linux.shell.check_run = self.run_command
        linux._qcow2_chain_has_luks_encrypted_image = lambda _: False
        linux.qemu_img.subcmd = lambda operation: "/usr/bin/qemu-img %s" % operation

    def tearDown(self):
        linux.shell.check_run = self.original_check_run
        linux._qcow2_chain_has_luks_encrypted_image = self.original_chain_has_luks
        linux._is_block_device = self.original_is_block_device
        linux.qemu_img.subcmd = self.original_qemu_img_subcmd
        os.chmod = self.original_chmod
        shutil.rmtree(self.workdir)

    def run_command(self, command):
        self.commands.append(command)
        args = shlex.split(command)
        if "convert" in args:
            target = args[-1]
            self.convert_targets.append(target)
            with open(target, "wb") as fd:
                fd.write("converted")
        elif args[0] == "mv":
            os.rename(args[-2], args[-1])

    def test_writes_file_target_directly_without_rename(self):
        actual_size = linux.convert_qcow2_volume_encryption(self.src, self.dst, False)

        self.assertEqual([self.dst], self.convert_targets)
        self.assertFalse(any(shlex.split(command)[0] == "mv" for command in self.commands))
        self.assertEqual(len("converted"), actual_size)

    def test_removes_direct_target_when_finalization_fails(self):
        def fail_chmod(*_):
            raise Exception("chmod failed")

        os.chmod = fail_chmod

        with self.assertRaises(Exception):
            linux.convert_qcow2_volume_encryption(self.src, self.dst, False)

        self.assertEqual([self.dst], self.convert_targets)
        self.assertFalse(os.path.exists(self.dst))
        with open(self.src, "rb") as fd:
            self.assertEqual("source", fd.read())

    def test_keeps_existing_file_when_validation_fails(self):
        with open(self.dst, "wb") as fd:
            fd.write("existing")

        with self.assertRaises(Exception):
            linux.convert_qcow2_volume_encryption(
                self.src, self.dst, False,
                target_backing_file=os.path.join(self.workdir, "missing-backing.qcow2"))

        self.assertEqual([], self.convert_targets)
        with open(self.dst, "rb") as fd:
            self.assertEqual("existing", fd.read())

    def test_leaves_failed_block_target_for_caller_cleanup(self):
        linux._is_block_device = lambda _: True

        def fail_conversion(command):
            self.run_command(command)
            raise Exception("convert failed")

        linux.shell.check_run = fail_conversion

        with self.assertRaises(Exception):
            linux.convert_qcow2_volume_encryption(self.src, self.dst, False)

        self.assertTrue(os.path.exists(self.dst))
