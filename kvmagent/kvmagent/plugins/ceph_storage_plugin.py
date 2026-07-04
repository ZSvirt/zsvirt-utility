import os
from contextlib import contextmanager
try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote

from kvmagent import kvmagent
from kvmagent.plugins.imagestore import ImageStoreClient
from kvmagent.plugins import volume_secret
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import linux
from zstacklib.utils import log
from zstacklib.utils import shell
from zstacklib.utils import qemu_img

logger = log.get_logger(__name__)


class CheckHostStorageConnectionCmd(kvmagent.AgentCommand):
    def __init__(self):
        super(CheckHostStorageConnectionCmd, self).__init__()
        self.monUrls = None
        self.hostUuid = None
        self.uuid = None
        self.poolNames = None


class CheckHostStorageConnectionRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(CheckHostStorageConnectionRsp, self).__init__()


class CephLuksRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(CephLuksRsp, self).__init__()
        self.actualSize = None
        self.installPath = None


class CephStoragePlugin(kvmagent.KvmAgent):
    CHECK_HOST_STORAGE_CONNECTION_PATH = "/ceph/primarystorage/check/host/connection"
    LUKS_CLONE_PATH = "/ceph/primarystorage/kvmhost/luksclone"
    LUKS_CREATE_EMPTY_PATH = "/ceph/primarystorage/kvmhost/lukscreateempty"
    LUKS_ENCRYPT_IN_PLACE_PATH = "/ceph/primarystorage/kvmhost/encryptinplace"
    LUKS_RESIZE_PATH = "/ceph/primarystorage/kvmhost/luksresize"
    LUKS_CONVERT_PATH = "/ceph/primarystorage/kvmhost/luksconvert"
    IMAGESTORE_ENCRYPTED_DOWNLOAD_PATH = "/ceph/primarystorage/kvmhost/imagestore/encrypteddownload"

    CEPH_CLIENT_CONF_ROOT = "/var/lib/zstack/ceph"

    @staticmethod
    def _env_prefix(env):
        if not env:
            return ""
        return " ".join(["%s=%s" % (k, shell_quote(str(v))) for k, v in env.items()]) + " "

    @contextmanager
    def _json_temp_file(self, content):
        path = self.imagestore_client._write_json_temp_file(content)
        try:
            yield path
        finally:
            linux.rm_file_force(path)

    def start(self):
        http_server = kvmagent.get_http_server()
        http_server.register_async_uri(self.CHECK_HOST_STORAGE_CONNECTION_PATH, self.check_host_storage_connection)
        http_server.register_async_uri(self.LUKS_CLONE_PATH, self.luks_clone)
        http_server.register_async_uri(self.LUKS_CREATE_EMPTY_PATH, self.luks_create_empty)
        http_server.register_async_uri(self.LUKS_ENCRYPT_IN_PLACE_PATH, self.luks_encrypt_in_place)
        http_server.register_async_uri(self.LUKS_RESIZE_PATH, self.luks_resize)
        http_server.register_async_uri(self.LUKS_CONVERT_PATH, self.luks_convert)
        http_server.register_async_uri(self.IMAGESTORE_ENCRYPTED_DOWNLOAD_PATH, self.download_encrypted_imagestore)
        self.imagestore_client = ImageStoreClient()

    @kvmagent.replyerror
    def check_host_storage_connection(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        mon_url = '\;'.join(cmd.monUrls)
        mon_url = mon_url.replace(':', '\\\:')
        rsp = CheckHostStorageConnectionRsp()

        def get_ceph_rbd_args(pool_name):
            if cmd.userKey is None:
                return 'rbd:%s:mon_host=%s' % (get_heartbeat_volume(pool_name, cmd.uuid, cmd.hostUuid), mon_url)
            return 'rbd:%s:id=zstack:key=%s:auth_supported=cephx\;none:mon_host=%s' % (get_heartbeat_volume(pool_name, cmd.uuid, cmd.hostUuid), cmd.userKey, mon_url)

        def heartbeat_file_exists(pool_name):
            touch = shell.ShellCmd('timeout 5 %s %s' %
                    (qemu_img.subcmd('info'), get_ceph_rbd_args(pool_name)))
            touch(False)

            if touch.return_code == 0:
                return True

            logger.warn('cannot query heartbeat image: %s: %s' % (cmd.heartbeatImagePath, touch.stderr))
            return False

        def create_heartbeat_file(pool_name):
            create = shell.ShellCmd('timeout 5 qemu-img create -f raw %s 1' %
                                        get_ceph_rbd_args(pool_name))
            create(False)

            if create.return_code == 0 or "File exists" in create.stderr:
                return True

            logger.warn('cannot create heartbeat image: %s: %s' % (cmd.heartbeatImagePath, create.stderr))
            return False
        
        def get_heartbeat_volume(pool_name, ps_uuid, host_uuid):
            return '%s/ceph-ps-%s-host-hb-%s' % (pool_name, ps_uuid, host_uuid)

        if len(cmd.poolNames) == 0:
            return jsonobject.dumps(rsp)

        failed_pools = []
        for pool_name in cmd.poolNames:    
            if heartbeat_file_exists(pool_name) or create_heartbeat_file(pool_name):
                continue

            failed_pools.append(pool_name)

        if len(failed_pools) == 0:
            return jsonobject.dumps(rsp)

        if len(failed_pools) == len(cmd.poolNames):
            rsp.error = "Can not connect to all pools of ceph storage[uuid:%s] from host[uuid:%s]" % (cmd.uuid, cmd.hostUuid)
        else:
            rsp.error = "Can not connect to pools[%s] of ceph storage[uuid:%s] from host[uuid:%s]" % (', '.join(failed_pools) ,cmd.uuid, cmd.hostUuid)

        rsp.success = False
        return jsonobject.dumps(rsp)
    
    @staticmethod
    def _rbd_uri(install_path, conf_path, extra=None):
        s = "rbd:%s:conf=%s" % (install_path, conf_path)
        if extra:
            s += ":" + extra
        return s

    @staticmethod
    def _rbd_image_opts(install_path, conf_path):
        image_path = install_path
        snapshot = None
        if "@" in image_path:
            image_path, snapshot = image_path.split("@", 1)
        pool, image = image_path.split("/", 1)
        opts = "file.driver=rbd,file.pool=%s,file.image=%s,file.conf=%s" % (pool, image, conf_path)
        if snapshot:
            opts += ",file.snapshot=%s" % snapshot
        return opts

    def _is_luks_rbd(self, install_path, conf_path):
        # Do not pass the one-shot LUKS secret FIFO here: qemu-img info would
        # consume it before the real convert command. The LUKS header is enough
        # for qemu-img to report the source format without opening the payload.
        try:
            out = shell.call("/usr/bin/qemu-img info %s" % self._rbd_uri(install_path, conf_path))
            return "file format: luks" in out
        except Exception as e:
            logger.warn("failed to probe RBD source format for %s: %s" % (install_path, e))
            raise

    def _validate_luks_cmd(self, cmd, rsp, encrypted_dek=False):
        if not getattr(cmd, "psUuid", None):
            rsp.success = False
            rsp.error = "psUuid is required for LUKS ceph operation on KVM host"
            return None
        if encrypted_dek:
            if not getattr(cmd, "encryptedDek", None):
                rsp.success = False
                rsp.error = "encryptedDek is required for LUKS ceph operation on KVM host"
                return None
        elif not getattr(cmd, "secFilePath", None):
            rsp.success = False
            rsp.error = "secFilePath is required for LUKS ceph operation on KVM host"
            return None
        # ZStack pushes per-PS ceph.conf + client.zstack.keyring to every
        # attached KVM host under /var/lib/zstack/ceph/<ps>/. We reuse that
        # so LUKS clone on a KVM host can talk to ceph without depending on
        # /etc/ceph/ceph.conf (which may not exist on non-converged hosts).
        conf = os.path.join(self.CEPH_CLIENT_CONF_ROOT, cmd.psUuid, "ceph.conf")
        if not os.path.exists(conf):
            rsp.success = False
            rsp.error = (
                "ceph client config not found on host: %s. "
                "Re-attach the primary storage to this host." % conf
            )
            return None
        return conf

    @kvmagent.replyerror
    def luks_clone(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)
        src_path = cmd.srcPath.replace("ceph://", "")
        dst_path = cmd.dstPath.replace("ceph://", "")

        def do_luks_convert(sec):
            if self._is_luks_rbd(src_path, conf):
                src_arg = (
                    "--image-opts driver=luks,key-secret=luks_sec,%s" % self._rbd_image_opts(src_path, conf)
                )
            else:
                src_arg = "-f raw %s" % self._rbd_uri(src_path, conf)

            shell.call(
                "/usr/bin/qemu-img convert "
                "--object secret,id=luks_sec,format=raw,file=%s "
                "-m 16 -W %s -O luks -o key-secret=luks_sec %s" % (
                    sec,
                    src_arg,
                    self._rbd_uri(dst_path, conf, "rbd_cache=false:rbd_concurrent_management_ops=20"),
                )
            )

        def do_luks_resize(sec, virtual_size):
            dst_pool, dst_image = dst_path.split("/", 1)
            shell.call(
                "/usr/bin/qemu-img resize "
                "--object secret,id=luks_sec,format=raw,file=%s "
                "--image-opts driver=luks,key-secret=luks_sec,"
                "file.driver=rbd,file.pool=%s,file.image=%s,file.conf=%s %s" % (
                    sec, dst_pool, dst_image, conf, virtual_size,
                )
            )

        try:
            with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
                do_luks_convert(sec)
            virtual_size = getattr(cmd, "virtualSizeForLuksClone", None)
            if virtual_size:
                with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
                    do_luks_resize(sec, virtual_size)
        finally:
            pass

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def download_encrypted_imagestore(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)
        if not getattr(cmd, "hostname", None):
            rsp.success = False
            rsp.error = "hostname is required for encrypted ImageStore download"
            return jsonobject.dumps(rsp)
        if not getattr(cmd, "backupStorageInstallPath", None):
            rsp.success = False
            rsp.error = "backupStorageInstallPath is required for encrypted ImageStore download"
            return jsonobject.dumps(rsp)
        if not getattr(cmd, "primaryStorageInstallPath", None):
            rsp.success = False
            rsp.error = "primaryStorageInstallPath is required for encrypted ImageStore download"
            return jsonobject.dumps(rsp)

        self.imagestore_client._check_zstore_cli()
        name, imageid = self.imagestore_client._parse_image_reference(cmd.backupStorageInstallPath)
        target_encryption = {
            "encrypted": True,
            "encryptedDek": getattr(cmd, "encryptedDek", None),
            "keyProviderUuid": getattr(cmd, "keyProviderUuid", None),
            "keyVersion": getattr(cmd, "keyVersion", None),
            "cipher": getattr(cmd, "cipher", None),
        }
        source_encryption = None
        if getattr(cmd, "sourceEncrypted", False):
            source_encryption = {
                "encrypted": True,
                "encryptedDek": getattr(cmd, "encryptedDek", None),
                "keyProviderUuid": getattr(cmd, "keyProviderUuid", None),
                "keyVersion": getattr(cmd, "keyVersion", None),
                "cipher": getattr(cmd, "cipher", None),
            }

        with self._json_temp_file(target_encryption) as target_encryption_file:
            if source_encryption:
                with self._json_temp_file(source_encryption) as source_encryption_file:
                    self._pull_encrypted_imagestore(
                        cmd, conf, name, imageid, target_encryption_file, source_encryption_file)
            else:
                self._pull_encrypted_imagestore(cmd, conf, name, imageid, target_encryption_file, None)
        return jsonobject.dumps(rsp)

    def _pull_encrypted_imagestore(self, cmd, conf, name, imageid, target_encryption_file, source_encryption_file):
        pull_extra_param = " -target-encryption-json-file %s -secret-channel-provider %s" % (
            shell_quote(str(target_encryption_file)), shell_quote(str(self.imagestore_client.KEY_AGENT_PROVIDER)))
        if source_encryption_file:
            pull_extra_param += " -encryption-json-file %s" % shell_quote(str(source_encryption_file))
        concurrency = getattr(cmd, "concurrency", 1)
        if concurrency and concurrency > 1:
            pull_extra_param += " -concurrency=%d" % concurrency
        env = {
            "CEPH_CONF": conf,
            "CEPH_ARGS": "--conf %s" % conf,
        }
        cmdstr = "%s%s -url %s:%s pull %s -installpath %s %s:%s" % (
            self._env_prefix(env),
            self.imagestore_client.ZSTORE_CLI_PATH,
            shell_quote(str(cmd.hostname)),
            self.imagestore_client.ZSTORE_DEF_PORT,
            pull_extra_param,
            shell_quote(str(cmd.primaryStorageInstallPath)),
            shell_quote(str(name)),
            shell_quote(str(imageid)))
        shell.call(cmdstr)

    @kvmagent.replyerror
    def luks_create_empty(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)
        install_path = cmd.installPath.replace("ceph://", "")
        try:
            with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
                shell.call(
                    "/usr/bin/qemu-img create "
                    "--object secret,id=luks_sec,format=raw,file=%s "
                    "-f luks -o key-secret=luks_sec %s %s" % (
                        sec, self._rbd_uri(install_path, conf), cmd.size,
                    )
                )
        finally:
            pass
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def luks_convert(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)

        src_path = cmd.installPath.replace("ceph://", "")
        target_path = getattr(cmd, "targetInstallPath", None)
        if not target_path:
            rsp.success = False
            rsp.error = "targetInstallPath is required for RBD LUKS conversion"
            return jsonobject.dumps(rsp)
        target_path = target_path.replace("ceph://", "")

        if "@" in src_path or "@" in target_path:
            rsp.success = False
            rsp.error = "RBD LUKS conversion only supports active image paths, got source[%s], target[%s]" % (
                src_path, target_path,
            )
            return jsonobject.dumps(rsp)

        source_is_luks = self._is_luks_rbd(src_path, conf)
        if source_is_luks:
            src_arg = "--image-opts driver=luks,key-secret=luks_sec,%s" % self._rbd_image_opts(src_path, conf)
        else:
            if not cmd.targetEncrypted:
                rsp.success = False
                rsp.error = "RBD image is not LUKS formatted: %s" % cmd.installPath
                return jsonobject.dumps(rsp)
            src_arg = "-f raw %s" % self._rbd_uri(src_path, conf)

        with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
            if cmd.targetEncrypted:
                target_format = "-O luks -o key-secret=luks_sec"
            else:
                target_format = "-O raw"
            shell.call(
                "/usr/bin/qemu-img convert "
                "--object secret,id=luks_sec,format=raw,file=%s "
                "-m 16 -W %s %s %s" % (
                    sec,
                    src_arg,
                    target_format,
                    self._rbd_uri(target_path, conf, "rbd_cache=false:rbd_concurrent_management_ops=20"),
                )
            )

        virtual_size = getattr(cmd, "virtualSize", None)
        if cmd.targetEncrypted and virtual_size:
            pool, image = target_path.split("/", 1)
            with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
                shell.call(
                    "/usr/bin/qemu-img resize "
                    "--object secret,id=luks_sec,format=raw,file=%s "
                    "--image-opts driver=luks,key-secret=luks_sec,"
                    "file.driver=rbd,file.pool=%s,file.image=%s,file.conf=%s %s" % (
                        sec, pool, image, conf, virtual_size,
                    )
                )

        rsp.installPath = cmd.targetInstallPath
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def luks_resize(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)
        install_path = cmd.installPath.replace("ceph://", "")
        try:
            with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
                if not self._is_luks_rbd(install_path, conf):
                    rsp.success = False
                    rsp.error = "RBD image is not LUKS formatted: %s" % cmd.installPath
                    return jsonobject.dumps(rsp)

                virtual_size = getattr(cmd, "virtualSize", None)
                pool, image = install_path.split("/", 1)
                if virtual_size:
                    shell.call(
                        "/usr/bin/qemu-img resize "
                        "--object secret,id=luks_sec,format=raw,file=%s "
                        "--image-opts driver=luks,key-secret=luks_sec,"
                        "file.driver=rbd,file.pool=%s,file.image=%s,file.conf=%s %s" % (
                            sec, pool, image, conf, virtual_size,
                        )
                    )
                else:
                    shell.call(
                        "/usr/bin/qemu-img info "
                        "--object secret,id=luks_sec,format=raw,file=%s "
                        "--image-opts driver=luks,key-secret=luks_sec,"
                        "file.driver=rbd,file.pool=%s,file.image=%s,file.conf=%s" % (
                            sec, pool, image, conf,
                        )
                    )
        finally:
            pass

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def luks_encrypt_in_place(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CephLuksRsp()
        conf = self._validate_luks_cmd(cmd, rsp, encrypted_dek=True)
        if conf is None:
            return jsonobject.dumps(rsp)
        install_path = cmd.installPath.replace("ceph://", "")
        target_path = getattr(cmd, "targetInstallPath", None)
        if not target_path:
            rsp.success = False
            rsp.error = "targetInstallPath is required for RBD LUKS encrypt-in-place"
            return jsonobject.dumps(rsp)
        target_path = target_path.replace("ceph://", "")

        logger.info("start LUKS encryption for RBD image: source[%s], target[%s]" % (
            install_path, target_path,
        ))
        with volume_secret.luks_secret_channel(cmd.encryptedDek) as sec:
            shell.call(
                "/usr/bin/qemu-img convert "
                "--object secret,id=luks_sec,format=raw,file=%s "
                "-m 16 -W -f raw -O luks -o key-secret=luks_sec %s %s" % (
                    sec,
                    self._rbd_uri(install_path, conf),
                    self._rbd_uri(target_path, conf, "rbd_cache=false:rbd_concurrent_management_ops=20"),
                )
            )
        logger.info("created encrypted RBD image: source[%s], target[%s]" % (install_path, target_path))
        rsp.installPath = cmd.targetInstallPath
        return jsonobject.dumps(rsp)

    def stop(self):
        pass
        
    def configure(self, config):
        self.config = config
