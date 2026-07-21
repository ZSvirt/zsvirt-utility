from kvmagent import kvmagent
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import log
from zstacklib.utils import bash
from zstacklib.utils import linux
from zstacklib.utils import rollback
from kvmagent.plugins.imagestore import ImageStoreClient
from kvmagent.plugins import volume_secret
import contextlib
import json
import os
import subprocess
import tempfile
import time
import uuid



logger = log.get_logger(__name__)

CBD_RESIZE_ALIGNMENT = 1024 * 1024
LUKS_PAYLOAD_OFFSET = 8 * 1024 * 1024
LUKS_ALIGN_PAYLOAD_SECTORS = LUKS_PAYLOAD_OFFSET // 512
PROTOCOL_CBD_PREFIX = "cbd:"
CBD_VOLUME_PATH = PROTOCOL_CBD_PREFIX + "{}/{}/{}"
CBD_SNAPSHOT_PATH = CBD_VOLUME_PATH + "@{}"


def parse_cbd_path(path):
    if path.startswith(PROTOCOL_CBD_PREFIX):
        path = path[len(PROTOCOL_CBD_PREFIX):]
    physical_pool, logical_pool, volume_part = path.split("/", 2)
    if "@" in volume_part:
        volume, snapshot = volume_part.split("@", 1)
    else:
        volume = volume_part
        snapshot = None
    return physical_pool, logical_pool, volume, snapshot


def cmd_attr(cmd, name, default=None):
    if hasattr(cmd, 'hasattr') and cmd.hasattr(name):
        return getattr(cmd, name)
    try:
        return getattr(cmd, name)
    except (AttributeError, KeyError):
        return default


class CheckHostStorageConnectionRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(CheckHostStorageConnectionRsp, self).__init__()


class ZbsLuksRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(ZbsLuksRsp, self).__init__()
        self.actualSize = 0


class ZbsStoragePlugin(kvmagent.KvmAgent):
    CHECK_HOST_STORAGE_CONNECTION_PATH = "/zbs/primarystorage/check/host/connection"
    LUKS_CLONE_PATH = "/zbs/primarystorage/kvmhost/luksclone"
    LUKS_CREATE_EMPTY_PATH = "/zbs/primarystorage/kvmhost/lukscreateempty"
    LUKS_ENCRYPT_IN_PLACE_PATH = "/zbs/primarystorage/kvmhost/encryptinplace"
    LUKS_CONVERT_PATH = "/zbs/primarystorage/kvmhost/luksconvert"
    LUKS_RESIZE_PATH = "/zbs/primarystorage/kvmhost/luksresize"
    IMAGESTORE_ENCRYPTED_DOWNLOAD_PATH = "/zbs/primarystorage/kvmhost/imagestore/encrypteddownload"

    def start(self):
        http_server = kvmagent.get_http_server()
        http_server.register_async_uri(self.CHECK_HOST_STORAGE_CONNECTION_PATH, self.check_host_storage_connection)
        http_server.register_async_uri(self.LUKS_CLONE_PATH, self.luks_clone)
        http_server.register_async_uri(self.LUKS_CREATE_EMPTY_PATH, self.luks_create_empty)
        http_server.register_async_uri(self.LUKS_ENCRYPT_IN_PLACE_PATH, self.luks_encrypt_in_place)
        http_server.register_async_uri(self.LUKS_CONVERT_PATH, self.luks_convert)
        http_server.register_async_uri(self.LUKS_RESIZE_PATH, self.luks_resize)
        http_server.register_async_uri(self.IMAGESTORE_ENCRYPTED_DOWNLOAD_PATH, self.download_encrypted_imagestore)
        self.imagestore_client = ImageStoreClient()

    def _cbd_qemu_path(self, install_path):
        if not install_path.startswith(PROTOCOL_CBD_PREFIX):
            install_path = PROTOCOL_CBD_PREFIX + install_path
        return '%s_zbs_:/etc/zbs/client.conf' % install_path

    def _raw_cbd_size(self, install_path):
        out = bash.bash_errorout('/usr/bin/qemu-img info --output=json -f raw %s' %
                                 linux.shellquote(self._cbd_qemu_path(install_path)))
        return int(json.loads(out).get('virtual-size', 0))

    def _raw_cbd_actual_size(self, install_path):
        out = bash.bash_errorout('/usr/bin/qemu-img info --output=json -f raw %s' %
                                 linux.shellquote(self._cbd_qemu_path(install_path)))
        return int(json.loads(out).get('actual-size', 0) or 0)

    def _resize_raw_cbd_if_needed(self, install_path, required_size):
        required_size = int(required_size or 0)
        if required_size <= 0:
            return
        required_size = ((required_size + CBD_RESIZE_ALIGNMENT - 1) //
                         CBD_RESIZE_ALIGNMENT) * CBD_RESIZE_ALIGNMENT

        current_size = self._raw_cbd_size(install_path)
        if current_size >= required_size:
            return

        cmd = '/usr/bin/qemu-img resize -f raw %s %d' % (
            linux.shellquote(self._cbd_qemu_path(install_path)), required_size)
        bash.bash_errorout(cmd)

    def _luks_image_opts_value(self, install_path):
        opts = 'driver=luks,key-secret=luks_sec,file.driver=cbd,file.filename=%s' % self._cbd_qemu_path(install_path)
        return opts

    def _luks_image_opts(self, install_path):
        return '--image-opts %s' % linux.shellquote(self._luks_image_opts_value(install_path))

    def _plain_image_arg(self, install_path):
        return '-f raw %s' % linux.shellquote(self._cbd_qemu_path(install_path))

    def _is_luks_volume(self, install_path, encrypted_dek=None):
        if encrypted_dek:
            with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
                cmd = '/usr/bin/qemu-img info --object secret,id=luks_sec,format=raw,file=%s %s' % (
                    linux.shellquote(secret_file), self._luks_image_opts(install_path))
                bash.bash_errorout(cmd)
                return True
        out = bash.bash_errorout('/usr/bin/qemu-img info %s' % linux.shellquote(self._cbd_qemu_path(install_path)))
        return 'file format: luks' in out

    def _new_luks_temp_path(self):
        return '/tmp/zbs-luks-%s.img' % uuid.uuid4().hex

    def _copy_local_image_to_cbd(self, local_path, dst_path):
        cmd = '/usr/bin/qemu-img convert -n --target-is-zero -S 4k -m 16 -W -f raw %s -O raw %s' % (
            linux.shellquote(local_path), linux.shellquote(self._cbd_qemu_path(dst_path)))
        bash.bash_errorout(cmd)

    def _initialize_luks_cbd_volume(self, install_path, virtual_size, secret_file):
        tmp_path = self._new_luks_temp_path()
        try:
            bash.bash_errorout('/usr/bin/truncate -s %d %s' % (
                int(virtual_size) + LUKS_PAYLOAD_OFFSET, linux.shellquote(tmp_path)))
            # qemu-img creates a non-MiB-aligned LUKS payload offset, which leaks CBD
            # backing alignment as extra guest-visible bytes. cryptsetup lets us pin
            # the payload at 8MiB so virtual_size stays exact after ZBS MiB alignment.
            with linux.temporary_luks_secret_file(linux.read_luks_secret_material_file(secret_file)) as reusable_secret_file:
                cmd = '/usr/sbin/cryptsetup -q luksFormat --type luks1 --batch-mode --align-payload=%d --key-file %s %s' % (
                    LUKS_ALIGN_PAYLOAD_SECTORS,
                    linux.shellquote(reusable_secret_file),
                    linux.shellquote(tmp_path))
                bash.bash_errorout(cmd)
            self._copy_local_image_to_cbd(tmp_path, install_path)
        finally:
            linux.rm_file_force(tmp_path)

    def _write_plain_source_to_luks_cbd(self, src_path, dst_path, secret_file):
        secret_opt = '--object secret,id=luks_sec,format=raw,file=%s' % linux.shellquote(secret_file)
        src_arg = self._plain_image_arg(src_path)
        cmd = '/usr/bin/qemu-img convert -n --target-image-opts %s -m 16 -W %s %s' % (
            secret_opt, src_arg, linux.shellquote(self._luks_image_opts_value(dst_path)))
        bash.bash_errorout(cmd)

    def _create_luks_cbd_volume_from_plain_source(self, src_path, dst_path, virtual_size, encrypted_dek):
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            self._initialize_luks_cbd_volume(dst_path, virtual_size, secret_file)
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            self._write_plain_source_to_luks_cbd(src_path, dst_path, secret_file)

    def _resize_luks_target(self, install_path, virtual_size, encrypted_dek):
        if not virtual_size:
            return
        virtual_size = int(virtual_size)

        if not self._is_luks_volume(install_path, encrypted_dek):
            raise Exception('ZBS volume is not LUKS formatted: %s' % install_path)
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            info_cmd = '/usr/bin/qemu-img info --output=json --object secret,id=luks_sec,format=raw,file=%s %s' % (
                linux.shellquote(secret_file), self._luks_image_opts(install_path))
            current_size = int(json.loads(bash.bash_errorout(info_cmd)).get('virtual-size', 0))
        if current_size == virtual_size:
            return
        if current_size > virtual_size:
            logger.debug('skip shrinking newly initialized ZBS LUKS volume[%s] from %s to %s' % (
                install_path, current_size, virtual_size))
            return
        self._resize_raw_cbd_if_needed(
            install_path, self._raw_cbd_size(install_path) + virtual_size - current_size)
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            info_cmd = '/usr/bin/qemu-img info --output=json --object secret,id=luks_sec,format=raw,file=%s %s' % (
                linux.shellquote(secret_file), self._luks_image_opts(install_path))
            current_size = int(json.loads(bash.bash_errorout(info_cmd)).get('virtual-size', 0))
        if current_size >= virtual_size:
            if current_size > virtual_size:
                logger.debug('skip shrinking resized ZBS LUKS volume[%s] from %s to %s' % (
                    install_path, current_size, virtual_size))
            return
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            cmd = '/usr/bin/qemu-img resize --object secret,id=luks_sec,format=raw,file=%s %s %s' % (
                linux.shellquote(secret_file), self._luks_image_opts(install_path), virtual_size)
            bash.bash_errorout(cmd)

    def _clone_plain_to_luks(self, src_path, dst_path, encrypted_dek, virtual_size=None):
        if not encrypted_dek:
            raise Exception('encryptedDek is required for ZBS LUKS clone')

        if self._is_luks_volume(src_path):
            cmd = '/usr/bin/qemu-img convert -n -m 16 -W %s -O raw %s' % (
                self._plain_image_arg(src_path), linux.shellquote(self._cbd_qemu_path(dst_path)))
            bash.bash_errorout(cmd)
        else:
            virtual_size = int(virtual_size or self._raw_cbd_size(src_path))
            self._create_luks_cbd_volume_from_plain_source(src_path, dst_path, virtual_size, encrypted_dek)
        self._resize_luks_target(dst_path, virtual_size, encrypted_dek)

    def _create_luks_volume(self, install_path, size, encrypted_dek):
        if not encrypted_dek:
            raise Exception('encryptedDek is required for ZBS LUKS createempty')
        size = int(size or 0)
        if size <= 0:
            raise Exception('size is required and must be greater than 0 for ZBS LUKS createempty')
        with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
            self._initialize_luks_cbd_volume(install_path, size, secret_file)

    def _encrypt_luks_in_place(self, install_path, target_install_path, encrypted_dek):
        if not encrypted_dek:
            raise Exception('encryptedDek is required for ZBS encryptInPlace')
        if not target_install_path:
            raise Exception('targetInstallPath is required for ZBS encryptInPlace')
        if self._is_luks_volume(install_path):
            raise Exception('ZBS volume bits are already LUKS formatted while encryption metadata is not set: %s' %
                            install_path)
        self._clone_plain_to_luks(install_path, target_install_path, encrypted_dek)
        return target_install_path

    @kvmagent.replyerror
    @bash.in_bash
    def check_host_storage_connection(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CheckHostStorageConnectionRsp()

        cmd = 'qemu-io -c "read 0G 4k" -f cbd {}_zbs_:/etc/zbs/client.conf'.format(cmd.path)
        r, o, e = bash.bash_roe(cmd)
        if r != 0:
            if linux.catch_bad_alloc_exception(r, e):
                return jsonobject.dumps(rsp)
            rsp.error = "failed to check heartbeat[%s], %s" % (cmd.path, e)
            rsp.success = False
            return jsonobject.dumps(rsp)

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def luks_clone(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        src_path = '<missing>'
        dst_path = '<missing>'
        try:
            src_path = cmd_attr(cmd, 'srcPath')
            dst_path = cmd_attr(cmd, 'dstPath')
            if not src_path or not dst_path:
                raise Exception('srcPath and dstPath are required')
            self._clone_plain_to_luks(src_path, dst_path,
                                      cmd_attr(cmd, 'encryptedDek'),
                                      cmd_attr(cmd, 'virtualSizeForLuksClone'))
            rsp.actualSize = self._raw_cbd_actual_size(dst_path)
        except Exception as e:
            logger.warn(linux.get_exception_stacktrace())
            rsp.success = False
            rsp.error = 'failed to clone ZBS LUKS volume from[%s] to[%s]: %s' % (
                src_path or '<missing>', dst_path or '<missing>', str(e))
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def luks_convert(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        src_path = '<missing>'
        dst_path = '<missing>'
        try:
            src_path = cmd_attr(cmd, 'installPath')
            dst_path = cmd_attr(cmd, 'targetInstallPath')
            if not src_path or not dst_path:
                raise Exception('installPath and targetInstallPath are required')
            encrypted_dek = cmd_attr(cmd, 'encryptedDek')
            if not encrypted_dek:
                raise Exception('encryptedDek is required')
            virtual_size = int(cmd_attr(cmd, 'virtualSize') or 0)
            if virtual_size <= 0:
                raise Exception('virtualSize is required and must be greater than 0')
            target_encrypted = cmd_attr(cmd, 'targetEncrypted')
            if not isinstance(target_encrypted, bool):
                raise Exception('targetEncrypted is required and must be a boolean')
            source_is_luks = self._is_luks_volume(src_path)
            if source_is_luks == bool(target_encrypted):
                raise Exception('ZBS source format does not match requested encryption conversion')
            secret_file_provider = lambda: volume_secret.luks_secret_channel(encrypted_dek)
            if target_encrypted:
                with secret_file_provider() as secret_file:
                    self._initialize_luks_cbd_volume(dst_path, virtual_size, secret_file)
                with secret_file_provider() as secret_file:
                    linux.convert_volume_encryption(
                        self._plain_image_arg(src_path),
                        linux.shellquote(self._luks_image_opts_value(dst_path)),
                        linux.shellquote(secret_file), bash.bash_errorout,
                        target_is_precreated=True, use_target_image_opts=True)
            else:
                with secret_file_provider() as secret_file:
                    linux.convert_volume_encryption(
                        self._luks_image_opts(src_path),
                        linux.shellquote(self._cbd_qemu_path(dst_path)),
                        linux.shellquote(secret_file), bash.bash_errorout,
                        target_format_options="-O raw", target_is_precreated=True)
            rsp.actualSize = self._raw_cbd_actual_size(dst_path)
        except Exception as e:
            logger.warn(linux.get_exception_stacktrace())
            rsp.success = False
            rsp.error = 'failed to convert ZBS volume encryption from[%s] to[%s]: %s' % (
                src_path or '<missing>', dst_path or '<missing>', str(e))
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def luks_create_empty(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        install_path = '<missing>'
        try:
            install_path = cmd_attr(cmd, 'installPath')
            if not install_path:
                raise Exception('installPath is required')
            self._create_luks_volume(install_path, cmd_attr(cmd, 'size'), cmd_attr(cmd, 'encryptedDek'))
            rsp.actualSize = self._raw_cbd_actual_size(install_path)
        except Exception as e:
            logger.warn(linux.get_exception_stacktrace())
            rsp.success = False
            rsp.error = 'failed to create empty ZBS LUKS volume[%s]: %s' % (install_path or '<missing>', str(e))
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def luks_encrypt_in_place(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        install_path = '<missing>'
        try:
            install_path = cmd_attr(cmd, 'installPath')
            if not install_path:
                raise Exception('installPath is required')
            rsp.installPath = self._encrypt_luks_in_place(
                install_path, cmd_attr(cmd, 'targetInstallPath'), cmd_attr(cmd, 'encryptedDek'))
            rsp.actualSize = self._raw_cbd_actual_size(rsp.installPath)
        except Exception as e:
            logger.warn(linux.get_exception_stacktrace())
            rsp.success = False
            rsp.error = 'failed to encrypt ZBS volume[%s] in place: %s' % (install_path or '<missing>', str(e))
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def luks_resize(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        install_path = '<missing>'
        try:
            install_path = cmd_attr(cmd, 'installPath')
            if not install_path:
                raise Exception('installPath is required')
            virtual_size = int(cmd_attr(cmd, 'virtualSize') or 0)
            if virtual_size <= 0:
                raise Exception('virtualSize is required and must be greater than 0')
            self._resize_luks_target(install_path, virtual_size,
                                     cmd_attr(cmd, 'encryptedDek'))
            rsp.actualSize = self._raw_cbd_actual_size(install_path)
        except Exception as e:
            logger.warn(linux.get_exception_stacktrace())
            rsp.success = False
            rsp.error = 'failed to resize ZBS LUKS volume[%s]: %s' % (install_path or '<missing>', str(e))
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @rollback.rollback
    @bash.in_bash
    def download_encrypted_imagestore(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ZbsLuksRsp()
        hostname = cmd.backupStorageHostname
        nbd_port = int(cmd.backupStorageNbdPort or 0)
        export_names = list(cmd.backupStorageNbdExportNames)
        target_path = cmd.primaryStorageInstallPath
        encrypted_dek = cmd.encryptedDek
        source_encrypted = cmd.sourceEncrypted or False

        self._convert_imagestore_nbd_chain_to_luks_cbd(
            hostname, nbd_port, export_names, target_path, encrypted_dek, source_encrypted)
        return jsonobject.dumps(rsp)

    def _convert_imagestore_nbd_chain_to_luks_cbd(self, hostname, nbd_port, export_names,
                                                  target_path, encrypted_dek,
                                                  source_encrypted=True):
        work_path = tempfile.mkdtemp(prefix='zbs-imagestore-nbd-')
        nbd_socket = os.path.join(work_path, 'source.sock')
        qsd = None
        qsd_log = None

        @contextlib.contextmanager
        def source_secret_channel():
            if not source_encrypted:
                yield None
                return
            with volume_secret.luks_secret_channel(encrypted_dek) as secret_file:
                yield secret_file

        def cleanup_qsd():
            if qsd is not None and qsd.poll() is None:
                linux.kill_process(qsd.pid, is_exception=False)
                qsd.wait()
            if qsd_log is not None:
                qsd_log.close()
            linux.rm_dir_force(work_path)

        @rollback.rollbackable
        def rollback_qsd():
            cleanup_qsd()
        rollback_qsd()

        with source_secret_channel() as source_secret_file:
            secret = 'luks_sec'
            qsd_args = ['/usr/bin/qemu-storage-daemon']
            if source_encrypted:
                qsd_args.extend([
                    '--object', 'secret,id=%s,format=raw,file=%s' % (secret, source_secret_file),
                ])

            parent_node = None
            top_node = None
            for index in reversed(range(len(export_names))):
                nbd_node = 'source-nbd-%d' % index
                qcow2_node = 'source-qcow2-%d' % index
                qsd_args.extend([
                    '--blockdev', json.dumps({
                        'driver': 'nbd',
                        'node-name': nbd_node,
                        'server': {
                            'type': 'inet',
                            'host': str(hostname),
                            'port': str(nbd_port),
                        },
                        'export': str(export_names[index]),
                        'read-only': True,
                    }, separators=(',', ':')),
                ])
                qcow2_options = {
                    'driver': 'qcow2',
                    'node-name': qcow2_node,
                    'file': nbd_node,
                    'backing': parent_node,
                    'read-only': True,
                }
                if source_encrypted:
                    qcow2_options['encrypt'] = {
                        'format': 'luks',
                        'key-secret': secret,
                    }
                qsd_args.extend([
                    '--blockdev', json.dumps(qcow2_options, separators=(',', ':')),
                ])
                parent_node = qcow2_node
                top_node = qcow2_node

            qsd_args.extend([
                '--nbd-server', 'addr.type=unix,addr.path=%s' % nbd_socket,
                '--export', json.dumps({
                    'type': 'nbd',
                    'id': 'logical-top',
                    'node-name': top_node,
                    'name': 'top',
                    'writable': False,
                }, separators=(',', ':')),
            ])
            qsd_log = open(os.path.join(work_path, 'qsd.log'), 'w+')
            qsd = subprocess.Popen(qsd_args, stdout=qsd_log, stderr=subprocess.STDOUT, close_fds=True)

            for _ in range(100):
                if os.path.exists(nbd_socket):
                    break
                if qsd.poll() is not None:
                    qsd_log.flush()
                    qsd_log.seek(0)
                    raise Exception('failed to start ImageStore NBD source QSD: %s' % qsd_log.read())
                time.sleep(0.1)
            else:
                raise Exception('timed out waiting for ImageStore NBD source QSD')

        with volume_secret.luks_secret_channel(encrypted_dek) as target_secret_file:
            source_path = 'nbd+unix:///top?socket=%s' % nbd_socket
            cmd = '/usr/bin/qemu-img convert -n --target-image-opts ' \
                  '--object secret,id=%s,format=raw,file=%s ' \
                  '-m 16 -W -f raw %s %s' % (
                      secret,
                      linux.shellquote(target_secret_file),
                      linux.shellquote(source_path),
                      linux.shellquote(self._luks_image_opts_value(target_path)))
            bash.bash_errorout(cmd)
        cleanup_qsd()


    def stop(self):
        pass

    def configure(self, config):
        self.config = config
