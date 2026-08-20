__author__ = 'frank'

import base64
import binascii
import datetime
import os
import os.path
import pprint
import re
import subprocess
import time
import traceback
import urllib
import urllib2
import urlparse
import tempfile
import threading
import rados
import rbd
import cherrypy
import hashlib
from cherrypy.lib.static import _serve_fileobj
from cherrypy._cpreqbody import Entity, Part, SizedReader
from cherrypy._cprequest import Request

import zstacklib.utils.daemon as daemon
import zstacklib.utils.plugin as plugin
import zstacklib.utils.http as http
import zstacklib.utils.jsonobject as jsonobject
from zstacklib.utils import lock, report, bash
from zstacklib.utils import linux
from zstacklib.utils import thread
from zstacklib.utils.bash import *
from zstacklib.utils.ceph import get_mon_addr
from zstacklib.utils.file_downloader import FileDownloader
from zstacklib.utils.file_system_upload_task import FileSystemUploadTask
from zstacklib.utils.report import Report, get_exact_percent
from zstacklib.utils import shell
from zstacklib.utils import ceph
from zstacklib.utils import qemu_img
from zstacklib.utils import traceable_shell
from zstacklib.utils.rollback import rollback, rollbackable
from zstacklib.utils.ssh_validation import validate_ssh_host_ip, validate_ssh_username, validate_ssh_port, \
    validate_ssh_path, validate_ssh_script_path, SSHValidationError
from zstacklib.utils.upload_task import UploadTask, UploadHandler, StorageObject, UploadTasks
from zstacklib.utils.thread import AsyncThread

logger = log.get_logger(__name__)

BUFFER_SIZE = 16 * 1024 ** 2


class CephPoolCapacity(object):
    def __init__(self, name, available, used, total, replicated_size, security_policy, disk_utilization, related_osds, related_osd_capacity):
        self.name = name
        self.availableCapacity = available
        self.usedCapacity = used
        self.totalCapacity = total
        self.replicatedSize = replicated_size
        self.securityPolicy = security_policy
        self.diskUtilization = round(disk_utilization, 3)
        self.relatedOsds = related_osds
        self.relatedOsdCapacity = related_osd_capacity


class AgentCommand(object):
    def __init__(self):
        pass


class AgentResponse(object):
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error if error else ''
        self.totalCapacity = None
        self.availableCapacity = None
        self.poolCapacities = None
        self.type = None

class InitRsp(AgentResponse):
    def __init__(self):
        super(InitRsp, self).__init__()
        self.fsid = None

class DownloadRsp(AgentResponse):
    def __init__(self):
        super(DownloadRsp, self).__init__()
        self.size = None
        self.actualSize = None


class CephToCephMigrateImageCmd(AgentCommand):
    @log.sensitive_fields("dstMonSshPassword")
    def __init__(self):
        super(CephToCephMigrateImageCmd, self).__init__()
        self.imageUuid = None
        self.imageSize = None  # type: int
        self.srcInstallPath = None
        self.dstInstallPath = None
        self.dstMonHostname = None
        self.dstMonSshUsername = None
        self.dstMonSshPassword = None
        self.dstMonSshPort = None  # type: int


class UploadProgressRsp(AgentResponse):
    def __init__(self):
        super(UploadProgressRsp, self).__init__()
        self.completed = False
        self.progress = 0
        self.size = 0
        self.actualSize = 0
        self.installPath = None
        self.lastOpTime = 0
        self.downloadSize = 0

class GetImageSizeRsp(AgentResponse):
    def __init__(self):
        super(GetImageSizeRsp, self).__init__()
        self.size = None
        self.actualSize = None

class PingRsp(AgentResponse):
    def __init__(self):
        super(PingRsp, self).__init__()
        self.failure = None

class GetFactsRsp(AgentResponse):
    def __init__(self):
        super(GetFactsRsp, self).__init__()
        self.fsid = None
        self.monAddr = None

class DeleteImageMetaDataResponse(AgentResponse):
    def __init__(self):
        super(DeleteImageMetaDataResponse,self).__init__()
        self.ret = None

class WriteImageMetaDataResponse(AgentResponse):
    def __init__(self):
        super(WriteImageMetaDataResponse,self).__init__()

class GetImageMetaDataResponse(AgentResponse):
    def __init__(self):
        super(GetImageMetaDataResponse,self).__init__()
        self.imagesMetadata= None

class DumpImageMetaDataToFileResponse(AgentResponse):
    def __init__(self):
        super(DumpImageMetaDataToFileResponse,self).__init__()

class CheckImageMetaDataFileExistResponse(AgentResponse):
    def __init__(self):
        super(CheckImageMetaDataFileExistResponse, self).__init__()
        self.backupStorageMetaFileName = None
        self.exist = None

class GetLocalFileSizeRsp(AgentResponse):
    def __init__(self):
        super(GetLocalFileSizeRsp, self).__init__()
        self.size = None

class DownloadFileRsp(AgentResponse):
    def __init__(self):
        super(DownloadFileRsp, self).__init__()
        self.md5sum = None
        self.size = None

class DeleteFilesRsp(AgentResponse):
    def __init__(self):
        super(DeleteFilesRsp, self).__init__()

class UnzipFileRsp(AgentResponse):
    def __init__(self):
        super(UnzipFileRsp, self).__init__()
        self.unzipInstallPath = None
        self.fileSizes = None

class UploadFileRsp(AgentResponse):
    def __init__(self):
        super(UploadFileRsp, self).__init__()
        self.directUploadUrl = None

class UploadFileProgressRsp(AgentResponse):
    def __init__(self):
        super(UploadFileProgressRsp, self).__init__()
        self.apiId = None
        self.completed = False
        self.progress = 0
        self.size = 0
        self.actualSize = 0
        self.installPath = None
        self.lastOpTime = 0
        self.downloadSize = 0
        self.md5sum = None
        self.supportSuspend = False

class SoftwareUpgradePackageResponse(AgentResponse):
    def __init__(self):
        super(SoftwareUpgradePackageResponse, self).__init__()
        self.upgradeScriptPath = None

def replyerror(func):
    @functools.wraps(func)
    def wrap(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            content = traceback.format_exc()
            err = '%s\n%s\nargs:%s' % (str(e), content, pprint.pformat([args, kwargs]))
            rsp = AgentResponse()
            rsp.success = False
            rsp.error = str(e)
            logger.warn(err)
            return jsonobject.dumps(rsp)
    return wrap

def validate_install_path(install_path, param_name="installPath"):
    return linux.validate_install_path(install_path, param_name)

def get_image_format_from_header(ioctx, image_name):
    qcow2_length = 0x9007
    ifo = ImageFileObject(rbd.Image(ioctx, image_name))
    buf = ifo.read(qcow2_length)
    return get_image_format_from_buf(buf)

def get_image_format_from_buf(qhdr):
    if qhdr[:4] == 'QFI\xfb':
        if qhdr[16:20] == '\x00\x00\x00\00':
            return "qcow2"
        else:
            return "derivedQcow2"

    if qhdr[:5] == 'KDMV\x03':
        return 'vmdk'

    if qhdr[0x8001:0x8006] == 'CD001':
        return 'iso'

    if qhdr[0x8801:0x8806] == 'CD001':
        return 'iso'

    if qhdr[0x9001:0x9006] == 'CD001':
        return 'iso'
    return "raw"

class ImageFileObject(StorageObject):
    def __init__(self, image):
        # type: (rbd.Image) -> None
        super(ImageFileObject, self).__init__()
        self.offset = 0
        self.image = image
        self.size = image.size()

    def seek(self, offset):
        self.offset = min(offset, self.size)

    def read(self, n):
        length = min(self.size - self.offset, n)
        content = self.image.read(self.offset, length)
        self.offset += length
        return content

    def write(self, content):
        self.image.write(content, self.offset)
        self.offset = min(self.offset + len(content), self.size)

    def close(self):
        self.image.close()
        logger.debug("%s closed" % str(self.image))

class CephUploadTask(UploadTask):
    def __init__(self, imageUuid, installPath, dstPath, tmpPath, ioctx):
        super(CephUploadTask, self).__init__(imageUuid, installPath)
        self.tmpPath = tmpPath
        self.dstPath = dstPath
        self.ioctx = ioctx
        self.image_format = "raw"

    @AsyncThread
    def complete_upload(self):
        try:
            file_format = linux.get_img_fmt('rbd:' + self.tmpPath)
        except Exception as e:
            self.fail('upload image %s failed: %s' % (self.taskUuid, str(e)))
            shell.run('rbd rm %s' % self.tmpPath)
            return

        if file_format == 'qcow2' and linux.qcow2_get_backing_file('rbd:' + self.tmpPath):
            self.fail('Qcow2 image %s has backing file' % self.taskUuid)
            shell.run('rbd rm %s' % self.tmpPath)
            return

        if file_format in ['qcow2', 'vmdk']:
            conf_path = None
            try:
                with open('/etc/ceph/ceph.conf', 'r') as fd:
                    conf = fd.read()
                    conf = '%s\n%s\n' % (conf, 'rbd default format = 2')
                    conf_path = linux.write_to_temp_file(conf)

                shell.check_run('%s -f %s -O rbd rbd:%s rbd:%s:conf=%s' % (qemu_img.subcmd('convert'), file_format,
                                                                           self.tmpPath, self.dstPath, conf_path))
            except Exception as e:
                self.fail('cannot convert %s image %s to rbd, error: %s' % (file_format, self.taskUuid, str(e)))
                logger.warn('convert image %s failed: %s' % (self.taskUuid, str(e)))
                return
            finally:
                shell.run('rbd rm %s' % self.tmpPath)
                if conf_path:
                    os.remove(conf_path)
        else:
            shell.check_run('rbd mv %s %s' % (self.tmpPath, self.dstPath))

        if self.lastError:
            raise Exception(self.lastError)

        _, img_name = self.dstPath.split('/')
        self.image_format = get_image_format_from_header(self.ioctx, img_name)
        self.success()

    def create_object(self, slice_offset):
        _, image_name = self.tmpPath.split('/')
        self.create_image_if_not_exists(self.tmpPath)
        image_obj = ImageFileObject(rbd.Image(self.ioctx, image_name))
        image_obj.seek(slice_offset)
        return image_obj

    def create_image_if_not_exists(self, install_path):
        if self.task_created:
            return
        _, image_name = install_path.split('/')
        with lock.NamedLock("upload-image-%s" % self.taskUuid):
            if not self.task_created:
                rbd.RBD().create(self.ioctx, image_name, self.expectedSize)
                self.task_created = True

    @staticmethod
    def check_capacity(required_size):
        total, avail, _ = _get_capacity()
        if avail <= required_size:
            return "Ceph capacity not enough for size: %d, available: %d, total: %d" % (required_size, avail, total)
        return None

def _get_capacity():
    o = shell.call('ceph df -f json')
    df = jsonobject.loads(o)

    if df.stats.total_bytes__ is not None :
        total = long(df.stats.total_bytes_)
    elif df.stats.total_space__ is not None:
        total = long(df.stats.total_space__) * 1024
    else:
        raise Exception('unknown ceph df output: %s' % o)

    if df.stats.total_avail_bytes__ is not None:
        avail = long(df.stats.total_avail_bytes_)
    elif df.stats.total_avail__ is not None:
        avail = long(df.stats.total_avail_) * 1024
    else:
        raise Exception('unknown ceph df output: %s' % o)

    pool_capacities = []

    if not df.pools:
        return total, avail, pool_capacities

    pools = ceph.get_pools_capacity()
    if not pools:
        return total, avail, pool_capacities

    for pool in pools:
        pool_capacity = CephPoolCapacity(pool.pool_name,
                                         pool.available_capacity, pool.used_capacity, pool.pool_total_size,
                                         pool.replicated_size, pool.security_policy, pool.disk_utilization,
                                         pool.get_related_osds(), pool.related_osd_capacity)
        pool_capacities.append(pool_capacity)

    return total, avail, pool_capacities

class CephAgent(object):
    INIT_PATH = "/ceph/backupstorage/init"
    DOWNLOAD_IMAGE_PATH = "/ceph/backupstorage/image/download"
    JOB_CANCEL = "/job/cancel"
    UPLOAD_IMAGE_PATH = "/ceph/backupstorage/image/upload"
    EXPORT_IMAGE_PATH = "/ceph/export/:pool/:image"
    UPLOAD_PROGRESS_PATH = "/ceph/backupstorage/image/progress"
    DELETE_IMAGE_PATH = "/ceph/backupstorage/image/delete"
    PING_PATH = "/ceph/backupstorage/ping"
    ECHO_PATH = "/ceph/backupstorage/echo"
    GET_IMAGE_SIZE_PATH = "/ceph/backupstorage/image/getsize"
    ADD_EXPORT_TOKEN_PATH = "/ceph/backupstorage/image/export/addtoken"
    REMOVE_EXPORT_TOKEN_PATH = "/ceph/backupstorage/image/export/removetoken"
    GET_FACTS = "/ceph/backupstorage/facts"
    GET_IMAGES_METADATA = "/ceph/backupstorage/getimagesmetadata"
    DELETE_IMAGES_METADATA = "/ceph/backupstorage/deleteimagesmetadata"
    DUMP_IMAGE_METADATA_TO_FILE = "/ceph/backupstorage/dumpimagemetadatatofile"
    CHECK_IMAGE_METADATA_FILE_EXIST = "/ceph/backupstorage/checkimagemetadatafileexist"
    CHECK_POOL_PATH = "/ceph/backupstorage/checkpool"
    GET_LOCAL_FILE_SIZE = "/ceph/backupstorage/getlocalfilesize/"
    MIGRATE_IMAGE_PATH = "/ceph/backupstorage/image/migrate"

    FILE_DOWNLOAD_PATH = "/ceph/file/download"
    FILE_DIRECT_UPLOAD_PATH = "/ceph/file/direct/upload"
    FILE_UPLOAD_PATH = "/ceph/file/upload"
    FILE_UPLOAD_PROGRESS_PATH = "/ceph/file/progress"
    DELETE_FILES_PATH = "/ceph/files/delete"
    UNZIP_FILE_PATH = "/ceph/file/unzip"
    SOFTWARE_UPGRADE_PACKAGE_DEPLOY_PATH = "/ceph/upgrade/deploy"

    CEPH_METADATA_FILE = "bs_ceph_info.json"
    UPLOAD_PROTO = "upload://"
    LENGTH_OF_UUID = 32
    CEPH_CONF_PATH = "/etc/ceph/ceph.conf"
    # Strict base64 pattern: only standard base64 alphabet with optional trailing '=' padding.
    _BASE64_PATTERN = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
    _DEFAULT_UPGRADE_SCRIPT_TIMEOUT = 1800

    http_server = http.HttpServer(port=7761)
    http_server.logfile_path = log.get_logfile_path()
    upload_tasks = UploadTasks()
    upload_file_tasks = UploadTasks()

    def __init__(self):
        self.http_server.register_async_uri(self.INIT_PATH, self.init)
        self.http_server.register_async_uri(self.DOWNLOAD_IMAGE_PATH, self.download)
        self.http_server.register_raw_uri(self.UPLOAD_IMAGE_PATH, self.upload)
        self.http_server.register_raw_stream_uri(self.EXPORT_IMAGE_PATH, self.export)
        self.http_server.register_async_uri(self.ADD_EXPORT_TOKEN_PATH, self.add_export_token)
        self.http_server.register_async_uri(self.REMOVE_EXPORT_TOKEN_PATH, self.remove_export_token)
        self.http_server.register_async_uri(self.UPLOAD_PROGRESS_PATH, self.get_upload_progress)
        self.http_server.register_async_uri(self.DELETE_IMAGE_PATH, self.delete)
        self.http_server.register_async_uri(self.JOB_CANCEL, self.cancel)
        self.http_server.register_async_uri(self.PING_PATH, self.ping)
        self.http_server.register_async_uri(self.GET_IMAGE_SIZE_PATH, self.get_image_size)
        self.http_server.register_async_uri(self.GET_FACTS, self.get_facts)
        self.http_server.register_sync_uri(self.ECHO_PATH, self.echo)
        self.http_server.register_async_uri(self.GET_IMAGES_METADATA, self.get_images_metadata)
        self.http_server.register_async_uri(self.CHECK_IMAGE_METADATA_FILE_EXIST, self.check_image_metadata_file_exist)
        self.http_server.register_async_uri(self.DUMP_IMAGE_METADATA_TO_FILE, self.dump_image_metadata_to_file)
        self.http_server.register_async_uri(self.DELETE_IMAGES_METADATA, self.delete_image_metadata_from_file)
        self.http_server.register_async_uri(self.CHECK_POOL_PATH, self.check_pool)
        self.http_server.register_async_uri(self.GET_LOCAL_FILE_SIZE, self.get_local_file_size)
        self.http_server.register_async_uri(self.MIGRATE_IMAGE_PATH, self.migrate_image, cmd=CephToCephMigrateImageCmd())

        self.http_server.register_async_uri(self.FILE_DOWNLOAD_PATH, self.download_file)
        self.http_server.register_raw_uri(self.FILE_DIRECT_UPLOAD_PATH, self.direct_upload_file)
        self.http_server.register_async_uri(self.FILE_UPLOAD_PATH, self.upload_file)
        self.http_server.register_async_uri(self.FILE_UPLOAD_PROGRESS_PATH, self.get_upload_file_progress)
        self.http_server.register_async_uri(self.DELETE_FILES_PATH, self.delete_files)
        self.http_server.register_async_uri(self.UNZIP_FILE_PATH, self.unzip_file)
        self.http_server.register_async_uri(self.SOFTWARE_UPGRADE_PACKAGE_DEPLOY_PATH, self.deploy_and_execute_software_upgrade_package)

        self.cluster = None
        self.ioctx = {}
        self.op_lock = threading.Lock()

    def get_ioctx(self, pool_name):
        # type: (str) -> rados.Ioctx

        if pool_name in self.ioctx:
            return self.ioctx[pool_name]

        with self.op_lock:
            if not self.cluster:
                self.cluster = rados.Rados(conffile=self.CEPH_CONF_PATH)
                self.cluster.connect()

            self.ioctx[pool_name] = self.cluster.open_ioctx(pool_name)

        return self.ioctx[pool_name]

    def _set_capacity_to_response(self, rsp):
        total, avail, pool_capacities = _get_capacity()

        rsp.totalCapacity = total
        rsp.availableCapacity = avail
        rsp.poolCapacities = pool_capacities
        rsp.type = ceph.get_ceph_manufacturer()

    @replyerror
    def echo(self, req):
        logger.debug('get echoed')
        return ''

    def _normalize_install_path(self, path):
        return path.lstrip('ceph:').lstrip('//')

    def _get_file_size(self, path):
        o = shell.call('rbd --format json info %s' % path)
        o = jsonobject.loads(o)
        return long(o.size_)

    @replyerror
    def get_image_size(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetImageSizeRsp()
        path = self._normalize_install_path(cmd.installPath)
        rsp.size = self._get_file_size(path)
        return jsonobject.dumps(rsp)

    def _read_file_content(self, path):
        with open(path) as f:
            return f.read()

    @in_bash
    @replyerror
    def get_images_metadata(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool_name = cmd.poolName
        bs_uuid = pool_name.split("-")[-1]
        valid_images_info = ""
        self.get_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        last_image_install_path = ""
        bs_ceph_info_file = "/tmp/%s" % self.CEPH_METADATA_FILE
        with open(bs_ceph_info_file) as fd:
            images_info = fd.read()
            for image_info in images_info.split('\n'):
                if image_info != '':
                    image_json = jsonobject.loads(image_info)
                    # todo support multiple bs
                    image_uuid = image_json['uuid']
                    image_install_path = image_json["backupStorageRefs"][0]["installPath"]
                    ret = bash_r("rbd info %s" % image_install_path.split("//")[1])
                    if ret == 0 :
                        logger.info("Check image %s install path %s successfully!" % (image_uuid, image_install_path))
                        if image_install_path != last_image_install_path:
                            valid_images_info = image_info + '\n' + valid_images_info
                            last_image_install_path = image_install_path
                    else:
                        logger.warn("Image %s install path %s is invalid!" % (image_uuid, image_install_path))

        self.put_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        rsp = GetImageMetaDataResponse()
        rsp.imagesMetadata= valid_images_info
        return jsonobject.dumps(rsp)

    @in_bash
    @replyerror
    def check_image_metadata_file_exist(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool_name = cmd.poolName
        bs_uuid = pool_name.split("-")[-1]
        rsp = CheckImageMetaDataFileExistResponse()
        rsp.backupStorageMetaFileName = self.CEPH_METADATA_FILE
        ret, output = bash_ro("rados -p bak-t-%s stat %s" % (bs_uuid,self.CEPH_METADATA_FILE))
        if ret == 0:
            rsp.exist = True
        else:
            rsp.exist = False
        return jsonobject.dumps(rsp)

    def get_metadata_file(self, bs_uuid, file_name):
        local_file_name = "/tmp/%s" % file_name
        linux.rm_file_force(local_file_name)
        bash_ro("rados -p bak-t-%s get %s %s" % (bs_uuid, file_name, local_file_name))

    def put_metadata_file(self, bs_uuid, file_name):
        local_file_name = "/tmp/%s" % file_name
        ret, output = bash_ro("rados -p bak-t-%s put %s %s" % (bs_uuid, file_name, local_file_name))
        if ret == 0:
            linux.rm_file_force(local_file_name)

    @in_bash
    @replyerror
    def dump_image_metadata_to_file(self, req):

        def _write_info_to_metadata_file(fd):
            strip_list_content = content[1:-1]
            data_list = strip_list_content.split('},')
            for item in data_list:
                if item.endswith("}") is not True:
                    item = item + "}"
                    fd.write(item + '\n')

        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool_name = cmd.poolName
        bs_uuid = pool_name.split("-")[-1]
        content = cmd.imageMetaData
        dump_all_metadata = cmd.dumpAllMetaData
        if dump_all_metadata is True:
            # this means no metadata exist in ceph
            bash_r("touch /tmp/%s" % self.CEPH_METADATA_FILE)
        else:
            self.get_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        bs_ceph_info_file = "/tmp/%s" % self.CEPH_METADATA_FILE
        if content is not None:
            if '[' == content[0] and ']' == content[-1]:
                if dump_all_metadata is True:
                    with open(bs_ceph_info_file, 'w') as fd:
                        _write_info_to_metadata_file(fd)
                else:
                    with open(bs_ceph_info_file, 'a') as fd:
                        _write_info_to_metadata_file(fd)
            else:
                # one image info
                if dump_all_metadata is True:
                    with open(bs_ceph_info_file, 'w') as fd:
                        fd.write(content + '\n')
                else:
                    with open(bs_ceph_info_file, 'a') as fd:
                        fd.write(content + '\n')

        self.put_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        rsp = DumpImageMetaDataToFileResponse()
        return jsonobject.dumps(rsp)

    @in_bash
    @replyerror
    def delete_image_metadata_from_file(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        image_uuid = cmd.imageUuid
        pool_name = cmd.poolName
        bs_uuid = pool_name.split("-")[-1]
        self.get_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        bs_ceph_info_file = "/tmp/%s" % self.CEPH_METADATA_FILE
        ret, output = bash_ro("sed -i.bak '/%s/d' %s" % (image_uuid, bs_ceph_info_file))
        self.put_metadata_file(bs_uuid, self.CEPH_METADATA_FILE)
        rsp = DeleteImageMetaDataResponse()
        rsp.ret = ret
        return jsonobject.dumps(rsp)



    @replyerror
    @in_bash
    def get_facts(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetFactsRsp()

        monmap = bash_o('ceph mon dump -f json')
        rsp.monAddr = get_mon_addr(monmap, "kernel")
        if rsp.monAddr is None:
            rsp.monAddr = get_mon_addr(monmap)

        if not rsp.monAddr:
            raise Exception('cannot find mon address of the mon server[%s]' % cmd.monUuid)

        rsp.fsid = ceph.get_fsid()
        return jsonobject.dumps(rsp)

    @replyerror
    def init(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        existing_pools = shell.call('ceph osd lspools')
        missing_pool_names = []
        for pool in cmd.pools:
            if pool.name in existing_pools:
                continue

            if pool.predefined:
                raise Exception('cannot find pool[%s] in the ceph cluster, you must create it manually' % pool.name)
            if ceph.is_xsky() or ceph.is_sandstone():
                raise Exception(
                    'The ceph storage type to be added does not support auto initialize pool, please create it manually')

            missing_pool_names.append(pool.name)

        ceph.create_pools_with_compatible_pg_num(missing_pool_names)

        rsp = InitRsp()
        rsp.fsid = ceph.get_fsid()
        self._set_capacity_to_response(rsp)

        return jsonobject.dumps(rsp)

    def _parse_install_path(self, path):
        return path.lstrip('ceph:').lstrip('//').split('/')

    def _fail_task(self, task, reason):
        task.fail(reason)
        raise Exception(reason)

    # handler for multipart upload, requires:
    # - header X-IMAGE-UUID
    # - header X-IMAGE-SIZE
    # options:
    # - header X-SLICE-OFFSET
    # - header X-SLICE-SIZE
    # - header X-SLICE-HASH
    # - header X-HASH-ALGORITHM
    def upload(self, req):
        # type: (Request) -> None

        try:
            UploadHandler(req, self.upload_tasks).handle_upload()
        except Exception, e:
            logger.error("File upload failed: %s", str(e))
            raise

    def _prepare_upload(self, cmd):
        class ImageUploadDaemon(plugin.TaskDaemon):
            def __init__(self, task):
                super(ImageUploadDaemon, self).__init__(cmd, 'imageUpload')
                self.task = task
                self.task.close = self.close

            def _cancel(self):
                if self.task.completed:
                    return
                self.task.fail("image [uuid: %s] upload canceled" % cmd.imageUuid)
                shell.run('rbd rm %s' % self.task.tmpPath)

        start = len(self.UPLOAD_PROTO)
        imageUuid = cmd.url[start:start+self.LENGTH_OF_UUID]
        dstPath = self._normalize_install_path(cmd.installPath)

        pool, image_name = self._parse_install_path(cmd.installPath)
        tmp_image_name = 'tmp-%s' % image_name
        tmpPath = '%s/%s' % (pool, tmp_image_name)

        task = CephUploadTask(imageUuid, cmd.installPath, dstPath, tmpPath, self.get_ioctx(pool))
        self.upload_tasks.add_task(task)
        return ImageUploadDaemon(task).start()

    def _get_upload_path(self, req):
        host = req[http.REQUEST_HEADER]['Host']
        return 'http://' + host + self.UPLOAD_IMAGE_PATH

    @replyerror
    def get_upload_progress(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        task = self.upload_tasks.get_task(cmd.imageUuid)
        if task is None:
            raise Exception('image not found %s' % cmd.imageUuid)

        rsp = UploadProgressRsp()
        rsp.completed = task.completed
        rsp.installPath = task.installPath
        rsp.size = task.expectedSize
        rsp.actualSize = task.expectedSize
        rsp.downloadSize = task.checked_download_size()
        rsp.lastOpTime = long(task.lastOpTime) * 1000
        rsp.format = task.image_format
        if task.expectedSize == 0:
            rsp.progress = 0
        elif task.completed and not task.lastError:
            rsp.size = self._get_file_size(task.dstPath)
            rsp.progress = 100
        else:
            logger.debug("image %s uploaded range : %s" % (cmd.imageUuid, task.slice_uploaded))
            rsp.progress = min(90, len(task.slice_uploaded) * 90 // task.expectedSize)

        if task.lastError is not None:
            rsp.success = False
            rsp.error = task.lastError
        return jsonobject.dumps(rsp)

    @replyerror
    @rollback
    def download(self, req):
        rsp = DownloadRsp()

        def _get_origin_format(path):
            qcow2_length = 0x9007
            if path.startswith('http://') or path.startswith('https://') or path.startswith('ftp://'):
                resp = urllib2.urlopen(path)
                qhdr = resp.read(qcow2_length)
                resp.close()
            elif path.startswith('sftp://'):
                fd, tmp_file = tempfile.mkstemp()
                get_header_from_pipe_cmd = "timeout 60 head --bytes=%d %s > %s" % (qcow2_length, pipe_path, tmp_file)
                clean_cmd = "pkill -f %s" % pipe_path
                shell.run('%s & %s && %s' % (scp_to_pipe_cmd, get_header_from_pipe_cmd, clean_cmd))
                qhdr = os.read(fd, qcow2_length)
                os.close(fd)
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            else:
                resp = open(path)
                qhdr = resp.read(qcow2_length)
                resp.close()
            if len(qhdr) < qcow2_length:
                return "raw"

            return get_image_format_from_buf(qhdr)

        def get_origin_format(fpath, fail_if_has_backing_file=True):
            image_format = _get_origin_format(fpath)
            if image_format == "derivedQcow2" and fail_if_has_backing_file:
                raise Exception('image has backing file or %s is not exist!' % fpath)
            return image_format

        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        if plugin.TaskManager.cancellation_pending(plugin.get_api_id(cmd)):
            rsp.success = False
            rsp.error = "image download canceled before start"
            return jsonobject.dumps(rsp)

        shell = traceable_shell.get_shell(cmd)
        pool, image_name = self._parse_install_path(cmd.installPath)
        tmp_image_name = 'tmp-%s' % image_name

        @rollbackable
        def _1():
            shell.check_run('rbd rm %s/%s' % (pool, tmp_image_name))

        def _getRealSize(length):
            '''length looks like: 10245K'''
            logger.debug(length)
            if not length[-1].isalpha():
                return length
            units = {
                "g": lambda x: x * 1024 * 1024 * 1024,
                "m": lambda x: x * 1024 * 1024,
                "k": lambda x: x * 1024,
            }
            try:
                if not length[-1].isalpha():
                    return length
                return units[length[-1].lower()](int(length[:-1]))
            except:
                logger.warn(linux.get_exception_stacktrace())
                return length

        # whether we have an upload request
        if cmd.url.startswith(self.UPLOAD_PROTO):
            if not self._prepare_upload(cmd):
                rsp.success = False
                rsp.error = "image upload canceled before start"
                return jsonobject.dumps(rsp)
            rsp.size = 0
            rsp.uploadPath = self._get_upload_path(req)
            self._set_capacity_to_response(rsp)
            return jsonobject.dumps(rsp)

        if cmd.sendCommandUrl:
            Report.url = cmd.sendCommandUrl

        report = Report(cmd.threadContext, cmd.threadContextStack)
        report.processType = "AddImage"
        report.resourceUuid = cmd.imageUuid
        report.progress_report("0", "start")

        url = urlparse.urlparse(cmd.url)
        if url.scheme in ('http', 'https', 'ftp'):
            cmd.url = self.percent_encode_url(cmd.url)

        # Re-parse after percent-encoding to ensure subsequent checks use
        # the updated cmd.url value.
        url = urlparse.urlparse(cmd.url)
        if url.scheme in ('http', 'https', 'ftp'):
            image_format = get_origin_format(cmd.url, True)
            cmd.url = linux.shellquote(cmd.url)
            # roll back tmp ceph file after import it
            _1()

            PFILE = linux.create_temp_file()
            content_length = shell.call("""curl -sLI %s|awk '/[cC]ontent-[lL]ength/{print $NF}'""" % cmd.url).splitlines()[-1]
            total = _getRealSize(content_length)

            def _getProgress(synced):
                last = linux.tail_1(PFILE).strip()
                if not last or len(last.split()) < 1 or 'HTTP request sent, awaiting response' in last:
                    return synced
                logger.debug("last synced: %s" % last)
                written = _getRealSize(last.split()[0])
                if total > 0 and synced < written:
                    synced = written
                    if synced < total:
                        percent = int(round(float(synced) / float(total) * 90))
                        report.progress_report(percent, "report")
                return synced

            logger.debug("content-length is: %s" % total)

            _, _, err = shell.bash_progress_1('wget --no-check-certificate -O - %s 2>%s| rbd import '
                                              '--image-format 2 - %s/%s ' % (cmd.url, PFILE, pool, tmp_image_name)
                                              , _getProgress, pipe_fail=True)
            if err:
                raise err
            actual_size = linux.get_file_size_by_http_head(cmd.url)

            if os.path.exists(PFILE):
                os.remove(PFILE)

        elif url.scheme == 'sftp':
            port = (url.port, 22)[url.port is None]
            PFILE = linux.create_temp_file()
            ssh_pswd_file = None
            pipe_path = PFILE + "fifo"
            scp_to_pipe_cmd = "scp -P %d -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %s@%s:%s %s" % (port, url.username, url.hostname, url.path, pipe_path)
            sftp_command = "sftp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=no -P %s -b /dev/stdin %s@%s" % (port, url.username, url.hostname) + " <<EOF\n%s\nEOF\n"
            if url.password is not None:
                ssh_pswd_file = linux.write_to_temp_file(url.password)
                scp_to_pipe_cmd = 'sshpass -f %s %s' % (ssh_pswd_file, scp_to_pipe_cmd)
                sftp_command = 'sshpass -f %s %s' % (ssh_pswd_file, sftp_command)

            actual_size = shell.call(sftp_command % ("ls -l " + url.path)).splitlines()[1].strip().split()[4]
            os.mkfifo(pipe_path)
            image_format = get_origin_format(cmd.url, True)
            cmd.url = linux.shellquote(cmd.url)
            # roll back tmp ceph file after import it
            _1()

            def _get_progress(synced):
                if not os.path.exists(PFILE):
                    return synced
                last = linux.tail_1(PFILE).strip()
                if not last or not last.isdigit():
                    return synced
                report.progress_report(int(last)*90/100, "report")
                return synced

            get_content_from_pipe_cmd = "pv -s %s -n %s 2>%s" % (actual_size, pipe_path, PFILE)
            import_from_pipe_cmd = "rbd import --image-format 2 - %s/%s" % (pool, tmp_image_name)
            _, _, err = shell.bash_progress_1('%s & %s | %s' %
                                        (scp_to_pipe_cmd, get_content_from_pipe_cmd, import_from_pipe_cmd),
                                              _get_progress, pipe_fail=True)

            if ssh_pswd_file:
                linux.rm_file_force(ssh_pswd_file)

            linux.rm_file_force(PFILE)
            linux.rm_file_force(pipe_path)

            if err:
                raise err

        elif url.scheme == 'file':
            src_path = cmd.url.lstrip('file:')
            src_path = os.path.normpath(src_path)
            if not os.path.isfile(src_path):
                raise Exception('cannot find the file[%s]' % src_path)
            image_format = get_origin_format(src_path, True)
            # roll back tmp ceph file after import it
            _1()

            p_file = linux.create_temp_file()
            def _get_percent(synced):
                t = linux.tail_1(p_file, split=b"\r")
                if t:
                    for word in t.split():
                        if word.endswith('%'):
                            report.progress_report(get_exact_percent(int(word[:-1]) * 0.9, report.taskStage))
                            break
                return synced

            t_shell = traceable_shell.get_shell(cmd)
            t_shell.bash_progress_1('rbd import --image-format 2 "%s" %s/%s 2>%s ' % (src_path, pool, tmp_image_name, p_file), _get_percent)
            actual_size = os.path.getsize(src_path)
        else:
            raise Exception('unknown url[%s]' % cmd.url)

        file_format = shell.call("set -o pipefail; %s rbd:%s/%s | grep 'file format' | cut -d ':' -f 2" % (
            qemu_img.subcmd('info'), pool, tmp_image_name))
        file_format = file_format.strip()
        if file_format not in ['qcow2', 'raw', 'vmdk']:
            raise Exception('unknown image format: %s' % file_format)

        if file_format in ['qcow2', 'vmdk']:
            conf_path = None
            try:
                with open('/etc/ceph/ceph.conf', 'r') as fd:
                    conf = fd.read()
                    conf = '%s\n%s\n' % (conf, 'rbd default format = 2')
                    conf_path = linux.write_to_temp_file(conf)

                shell.check_run('%s -f %s -O rbd rbd:%s/%s rbd:%s/%s:conf=%s' % (qemu_img.subcmd('convert'),
                                                                                 file_format, pool, tmp_image_name,
                                                                                 pool, image_name, conf_path))
                shell.check_run('rbd rm %s/%s' % (pool, tmp_image_name))
            finally:
                if conf_path:
                    os.remove(conf_path)
        else:
            shell.check_run('rbd mv %s/%s %s/%s' % (pool, tmp_image_name, pool, image_name))
        report.progress_report("100", "finish")

        @rollbackable
        def _2():
            shell.check_run('rbd rm %s/%s' % (pool, image_name))
        _2()

        o = shell.call('rbd --format json info %s/%s' % (pool, image_name))
        image_stats = jsonobject.loads(o)

        rsp.size = long(image_stats.size_)
        rsp.actualSize = actual_size
        if image_format in ['qcow2', 'vmdk']:
            rsp.format = "raw"
        else:
            rsp.format = image_format

        self._set_capacity_to_response(rsp)
        return jsonobject.dumps(rsp)

    def export(self, req, rsp, **kwargs):
        def get_image_name(image):
            return image[len(image) - self.LENGTH_OF_UUID:]

        pool_name = kwargs['pool']
        image_name = get_image_name(kwargs['image'])

        if isinstance(pool_name, unicode):
            pool_name = pool_name.encode('unicode-escape').decode('string_escape')
        if isinstance(image_name, unicode):
            image_name = image_name.encode('unicode-escape').decode('string_escape')

        ioctx = self.get_ioctx(pool_name)
        try:
            token = ioctx.read(image_name + "-export")
            if 'token' not in kwargs or token != kwargs['token']:
                rsp.status = 403
                return "Forbidden"
        except rados.ObjectNotFound:
            rsp.status = 404
            return "Image not found."

        image_file_obj = ImageFileObject(rbd.Image(ioctx, image_name, read_only=True))

        rsp.headers['Content-Type'] = 'application/x-download'

        req_close = req.close

        # cherrypy cannot ensure file obj closed every time. so hack it in request close.
        def all_close():
            req_close()
            image_file_obj.close()

        req.close = all_close
        return _serve_fileobj(image_file_obj, 'application/x-download', image_file_obj.size)

    @replyerror
    def add_export_token(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool, image_name = self._parse_install_path(cmd.installPath)
        ioctx = self.get_ioctx(pool)
        ioctx.write_full(image_name + "-export", cmd.token)

        rsp = AgentResponse()
        self._set_capacity_to_response(rsp)
        return jsonobject.dumps(rsp)

    @replyerror
    def remove_export_token(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool, image_name = self._parse_install_path(cmd.installPath)
        ioctx = self.get_ioctx(pool)
        try:
            ioctx.remove_object(image_name + "-export")
        except rados.ObjectNotFound:
            pass

        rsp = AgentResponse()
        self._set_capacity_to_response(rsp)
        return jsonobject.dumps(rsp)

    @replyerror
    def ping(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = PingRsp()

        monmap = bash_o('ceph mon dump -f json')
        found = False
        for mon in jsonobject.loads(monmap).mons:
            if cmd.monAddr in mon.addr:
                found = True
                break

        if not found:
            rsp.success = False
            rsp.failure = "MonAddrChanged"
            rsp.error = 'The mon addr is changed on the mon server[uuid:%s], not %s anymore.' \
                        'Reconnect the ceph primary storage' \
                        ' may solve this issue' % (cmd.monUuid, cmd.monAddr)
            return jsonobject.dumps(rsp)

        pool, objname = cmd.testImagePath.split('/')

        create_img = shell.ShellCmd("echo zstack | timeout 60 rados -p '%s' put '%s' -" % (pool, objname))
        create_img(False)
        if create_img.return_code != 0:
            rsp.success = False
            rsp.failure = 'UnableToCreateFile'
            rsp.error = "%s %s" % (create_img.stderr, create_img.stdout)
        else:
            shell.run("timeout 60 rados -p '%s' rm '%s'" % (pool, objname))

        linux.write_uuids("cephmonbs", "cephmonbs=%s" % cmd.monUuid)
        return jsonobject.dumps(rsp)

    @replyerror
    def delete(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        pool, image_name = self._parse_install_path(cmd.installPath)

        def delete_image(_):
            # in case image is deleted, we don't have to wait for timeout
            img = "%s/%s" % (pool, image_name)
            shell.check_run('rbd info %s && rbd rm %s' % (img, img))
            return True

        # 'rbd rm' might fail due to client crash. We wait for 30 seconds as suggested by 'rbd'.
        #
        # rbd: error: image still has watchers
        # This means the image is still open or the client using it crashed. Try again after
        # closing/unmapping it or waiting 30s for the crashed client to timeout.
        linux.wait_callback_success(delete_image, interval=5, timeout=30, ignore_exception_in_callback=True)

        pool, image_name = self._parse_install_path(cmd.installPath)
        ioctx = self.get_ioctx(pool)
        try:
            ioctx.remove_object(image_name + "-export")
        except rados.ObjectNotFound:
            pass

        rsp = AgentResponse()
        self._set_capacity_to_response(rsp)
        return jsonobject.dumps(rsp)

    @replyerror
    def check_pool(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        existing_pools = shell.call('ceph osd lspools')
        for pool in cmd.pools:
            if pool.name not in existing_pools:
                raise Exception('cannot find pool[%s] in the ceph cluster, you must create it manually' % pool.name)

        return jsonobject.dumps(AgentResponse())

    @replyerror
    def get_local_file_size(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetLocalFileSizeRsp()
        rsp.size = linux.get_local_file_size(cmd.path)
        return jsonobject.dumps(rsp)

    def _migrate_image(self, image_uuid, image_size, src_install_path, dst_install_path, dst_mon_addr, dst_mon_user, dst_mon_passwd, dst_mon_port):
        src_install_path = self._normalize_install_path(src_install_path)
        dst_install_path = self._normalize_install_path(dst_install_path)

        ssh_cmd, tmp_file = linux.build_sshpass_cmd(dst_mon_addr, dst_mon_passwd, 'tee >(md5sum >/tmp/%s_dst_md5) | rbd import - %s' % (image_uuid, dst_install_path), dst_mon_user, dst_mon_port)
        rst = shell.run("rbd export %s - | tee >(md5sum >/tmp/%s_src_md5) | %s" % (src_install_path, image_uuid, ssh_cmd))
        linux.rm_file_force(tmp_file)
        if rst != 0:
            return rst

        src_md5 = self._read_file_content('/tmp/%s_src_md5' % image_uuid)
        dst_md5 = linux.sshpass_call(dst_mon_addr, dst_mon_passwd, 'cat /tmp/%s_dst_md5' % image_uuid, dst_mon_user, dst_mon_port)
        if src_md5 != dst_md5:
            return -1
        else:
            return 0

    @replyerror
    @in_bash
    def migrate_image(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        rst = self._migrate_image(cmd.imageUuid, cmd.imageSize, cmd.srcInstallPath, cmd.dstInstallPath, cmd.dstMonHostname, cmd.dstMonSshUsername, cmd.dstMonSshPassword, cmd.dstMonSshPort)
        if rst != 0:
            rsp.success = False
            rsp.error = "Failed to migrate image from one ceph backup storage to another."
        self._set_capacity_to_response(rsp)
        return jsonobject.dumps(rsp)

    @replyerror
    def cancel(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        return jsonobject.dumps(plugin.cancel_job(cmd, rsp))

    def _check_unzip_capacity(self, install_path, fallback_size):
        """Return (has_capacity, error_msg) for unzipping."""
        UNZIP_FALLBACK_RATIO = 5
        try:
            unzip_size = linux.get_tar_uncompressed_size(install_path)
        except Exception as e:
            logger.warning("failed to get uncompressed size for %s: %s, "
                           "falling back to compressed size * %d (this may be inaccurate "
                           "for high compression ratio files)" % (install_path, str(e), UNZIP_FALLBACK_RATIO))
            unzip_size = fallback_size * UNZIP_FALLBACK_RATIO

        if unzip_size <= 0:
            logger.warning("uncompressed size for %s is %d, using fallback ratio" % (install_path, unzip_size))
            unzip_size = max(fallback_size * UNZIP_FALLBACK_RATIO, fallback_size)

        if unzip_size <= 0:
            logger.warning("cannot estimate uncompressed size for %s (fallback_size=%d), "
                           "skipping capacity check" % (install_path, fallback_size))
            return True, None

        _, available_capacity = linux.get_disk_capacity_by_df(os.path.dirname(install_path))
        if available_capacity < unzip_size:
            return False, "insufficient disk space on the host to unzip the file" \
                          " (available: %s, needed: %s)" % (available_capacity, unzip_size)
        return True, None

    @replyerror
    def download_file(self, req):
        rsp = DownloadFileRsp()
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        install_path, err = validate_install_path(cmd.installPath)
        if err:
            rsp.success = False
            rsp.error = err
            return jsonobject.dumps(rsp)
        cmd.installPath = install_path

        reporter = Report.from_spec(cmd, "DownloadFile")
        fileDownloader = FileDownloader(reporter, cmd)
        success, error = fileDownloader.download()
        if not success:
            rsp.success = False
            rsp.error = error if error else 'download failed'
            return jsonobject.dumps(rsp)

        rsp.md5sum = linux.get_file_md5sum_hashlib(install_path)
        rsp.size = os.path.getsize(install_path)
        return jsonobject.dumps(rsp)

    @replyerror
    def delete_files(self, req):
        rsp = DeleteFilesRsp()
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        if not cmd.filePaths or not isinstance(cmd.filePaths, list):
            rsp.success = False
            rsp.error = "filePaths must be a non-empty list"
            return jsonobject.dumps(rsp)

        try:
            failed = linux.safe_delete_paths(cmd.filePaths)
        except ValueError as e:
            rsp.success = False
            rsp.error = str(e)
            return jsonobject.dumps(rsp)

        if failed:
            rsp.success = False
            rsp.error = "failed to delete files: %s" % "; ".join(failed)
        return jsonobject.dumps(rsp)

    @replyerror
    def unzip_file(self, req):
        rsp = UnzipFileRsp()
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        install_path, err = validate_install_path(cmd.installPath)
        if err:
            rsp.success = False
            rsp.error = err
            return jsonobject.dumps(rsp)
        cmd.installPath = install_path

        if not os.path.exists(install_path):
            rsp.success = False
            rsp.error = "file not found: %s" % install_path
            return jsonobject.dumps(rsp)

        file_size = os.path.getsize(install_path)
        has_capacity, err_msg = self._check_unzip_capacity(install_path, file_size)
        if not has_capacity:
            rsp.success = False
            rsp.error = err_msg
            return jsonobject.dumps(rsp)

        rsp.unzipInstallPath, rsp.fileSizes = self.unzip_package_and_get_files_size(install_path)
        return jsonobject.dumps(rsp)

    @staticmethod
    def percent_encode_url(url):
        if isinstance(url, bytes):
            url = url.decode('utf-8')
        parsed = urlparse.urlparse(url)
        path = parsed.path
        # In Python 2, urllib.quote() cannot handle unicode strings containing
        # non-ASCII characters (raises KeyError).  Encode to UTF-8 bytes first
        # so that non-ASCII characters are properly percent-encoded.
        if not isinstance(path, bytes):
            path = path.encode('utf-8')
        encoded_path = urllib.quote(
            path,
            safe="/-_.~!$&'()*+,;=:@%"
        )
        return urlparse.urlunparse((
            parsed.scheme, parsed.netloc, encoded_path,
            parsed.params, parsed.query, parsed.fragment
        ))

    def get_direct_upload_path(self, host):
        return 'http://' + host + self.FILE_DIRECT_UPLOAD_PATH

    @replyerror
    def upload_file(self, req):
        rsp = UploadFileRsp()
        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        install_path, err = validate_install_path(cmd.installPath)
        if err:
            rsp.success = False
            rsp.error = err
            return jsonobject.dumps(rsp)
        cmd.installPath = install_path

        def _prepare_upload():
            class FileUploadDaemon(plugin.TaskDaemon):
                def __init__(self, task):
                    super(FileUploadDaemon, self).__init__(cmd, 'fileUpload')
                    self.task = task
                    self.task.close = self.close

                def _cancel(self):
                    if self.task.completed:
                        return
                    self.task.fail("file[%s] upload canceled" % cmd.installPath)
                    linux.rm_file_force(cmd.installPath)

            task = FileSystemUploadTask(cmd.taskUuid, cmd.installPath)
            self.upload_file_tasks.add_task(task)
            return FileUploadDaemon(task).start()

        if not _prepare_upload():
            rsp.success = False
            rsp.error = "file[%s] upload canceled before start" % cmd.installPath
            return jsonobject.dumps(rsp)
        rsp.directUploadUrl = self.get_direct_upload_path(req[http.REQUEST_HEADER]['Host'])
        return jsonobject.dumps(rsp)

    def direct_upload_file(self, req):
        try:
            UploadHandler(req, self.upload_file_tasks).handle_upload()
        except Exception as e:
            logger.exception("File upload failed: %s", str(e))
            raise

    class PathEscapeError(Exception):
        """Raised when a path escape (Zip-Slip) attempt is detected."""
        pass

    @staticmethod
    def check_tar_archive_safety(archive_path):
        """Check a tar archive for path escapes, symlinks, and hardlinks.

        :raises CephAgent.PathEscapeError: if any dangerous entry is found
        :raises Exception: if the tar listing command fails
        """
        listing = shell.call("tar -tf %s" % linux.shellquote(archive_path))
        for entry in listing.strip().splitlines():
            entry = entry.strip()
            if entry.startswith('/') or linux.contains_path_traversal(entry):
                raise CephAgent.PathEscapeError("path escape detected in archive entry: %s" % entry)
        # Reject symlink/hardlink entries -- tar -tf does not reveal symlink
        # targets, so a symlink pointing outside the directory would pass
        # the name-based check above.
        verbose_listing = shell.call("tar -tvf %s" % linux.shellquote(archive_path))
        for line in verbose_listing.strip().splitlines():
            if line and line[0] in ('l', 'h'):
                raise CephAgent.PathEscapeError(
                    "symlink or hardlink entries are not allowed in archive: %s" % line.strip()
                )

    @staticmethod
    def unzip_package_and_get_files_size(install_path):
        # Zip-Slip pre-check: verify no path escapes before extracting
        try:
            CephAgent.check_tar_archive_safety(install_path)
        except CephAgent.PathEscapeError:
            raise
        except Exception as e:
            raise Exception("failed to list tar contents for pre-check: %s" % str(e))

        unzip_dir = tempfile.mkdtemp(prefix="unzip_path_", dir=os.path.dirname(install_path))
        try:
            src_dir = linux.shellquote(os.path.dirname(install_path))
            file_name = linux.shellquote(os.path.basename(install_path))
            shell.call("cd %s && tar --no-same-owner --no-same-permissions -xf %s -C %s" % (src_dir, file_name, linux.shellquote(unzip_dir)))
        except Exception:
            try:
                linux.rm_dir_force(unzip_dir)
            except Exception:
                logger.warning("failed to clean up unzip directory: %s" % unzip_dir)
            raise

        # Post-extraction: ensure all files and symlinks stay within the target directory.
        # os.path.realpath() resolves symlinks, so a single check covers both
        # regular path escapes and symlink-based Zip-Slip attacks.
        real_unzip_dir = os.path.realpath(unzip_dir)
        file_sizes = {}
        for root, dirs, files in os.walk(unzip_dir):
            for name in dirs + files:
                file_path = os.path.join(root, name)
                real_path = os.path.realpath(file_path)
                if not real_path.startswith(real_unzip_dir + os.sep) and real_path != real_unzip_dir:
                    try:
                        linux.rm_dir_force(unzip_dir)
                    except Exception:
                        logger.warning("failed to clean up unzip directory after path escape: %s" % unzip_dir)
                    if os.path.islink(file_path):
                        raise CephAgent.PathEscapeError("symlink escape detected during unzip: %s -> %s" % (file_path, real_path))
                    raise CephAgent.PathEscapeError("path escape detected during unzip: %s -> %s" % (file_path, real_path))
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    file_sizes[file_path] = os.path.getsize(file_path)
                except (OSError, IOError) as e:
                    file_sizes[file_path] = 0
                    logger.warning("get file size failed: %s", str(e))

        total_extracted = sum(file_sizes.values())
        if total_extracted > 0:
            logger.info("unzip completed: %d files, total extracted size: %d bytes" % (len(file_sizes), total_extracted))

        return unzip_dir, file_sizes

    @replyerror
    def get_upload_file_progress(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        task = self.upload_file_tasks.get_task(cmd.taskUuid)
        if task is None:
            raise Exception('task not found')

        def _get_file_size(file_path):
            return os.path.getsize(file_path) if os.path.exists(file_path) else 0

        rsp = UploadFileProgressRsp()
        rsp.apiId = cmd.taskUuid
        rsp.completed = task.completed
        rsp.size = task.expectedSize
        rsp.actualSize = task.expectedSize
        # File-upload LongJob uses downloadSize == 0 to decide whether the
        # upload session has started. Report bytes actually received here;
        # RangeSet still controls completion.
        rsp.downloadSize = task.downloadSize
        rsp.lastOpTime = long(task.lastOpTime) * 1000
        rsp.supportSuspend = True
        if task.downloadSize == 0:
            rsp.progress = 0
            rsp.installPath = self.get_direct_upload_path(req[http.REQUEST_HEADER]['Host'])
        elif task.completed and not task.lastError:
            actual_size = _get_file_size(task.installPath)
            if actual_size == 0:
                rsp.completed = True
                rsp.progress = 0
                rsp.success = False
                rsp.error = "Upload completed but file not found or empty"
                return jsonobject.dumps(rsp)
            rsp.size = actual_size
            rsp.actualSize = actual_size
            rsp.downloadSize = actual_size
            rsp.md5sum = linux.get_file_md5sum_hashlib(task.installPath)
            rsp.installPath = task.installPath
            rsp.progress = 100
        else:
            if task.expectedSize > 0:
                rsp.progress = min(90, task.downloadSize * 90 // task.expectedSize)
            else:
                rsp.progress = 0
                logger.warning("upload task not yet fully initialized (expectedSize=%s)", task.expectedSize)
            rsp.installPath = self.get_direct_upload_path(req[http.REQUEST_HEADER]['Host'])

        if task.lastError is not None:
            rsp.success = False
            rsp.error = task.lastError
        return jsonobject.dumps(rsp)

    @replyerror
    @bash.in_bash
    def deploy_and_execute_software_upgrade_package(self, req):
        rsp = SoftwareUpgradePackageResponse()

        cmd = jsonobject.loads(req[http.REQUEST_BODY])

        target_path, err = validate_install_path(cmd.upgradePackageTargetPath, param_name="upgradePackageTargetPath")
        if err:
            rsp.success = False
            rsp.error = err
            return jsonobject.dumps(rsp)
        cmd.upgradePackageTargetPath = target_path

        target_host_ssh_port = cmd.targetHostSshPort
        target_host_ssh_username = cmd.targetHostSshUsername
        try:
            if cmd.targetHostSshPassword is None:
                raise ValueError("targetHostSshPassword is None")
            b64_str = cmd.targetHostSshPassword.strip()
            if not b64_str or not self._BASE64_PATTERN.match(b64_str):
                raise ValueError("malformed base64 input")
            target_host_ssh_password = base64.b64decode(b64_str)
        except (binascii.Error, ValueError, TypeError, AttributeError) as e:
            raise Exception("targetHostSshPassword is not valid base64 encoded: %s" % str(e))
        # Keep password as raw bytes (str in Python 2) to avoid encoding issues
        # with non-ASCII passwords when writing to file or subprocess stdin.
        if not isinstance(target_host_ssh_password, bytes):
            target_host_ssh_password = target_host_ssh_password.encode('utf-8')
        target_host_ip = cmd.targetHostIp
        # Wrap IPv6 in brackets for SSH/SCP
        ssh_target_ip = "[%s]" % target_host_ip if ':' in str(target_host_ip) else target_host_ip

        def stage_error(stage, details):
            details = str(details).strip() if details is not None else ""
            details = details or "(no output)"
            return ("software upgrade deploy failed: stage=%s, targetHost=%s:%s, "
                    "script=%s, details=%s" % (
                        stage, target_host_ip, target_host_ssh_port,
                        cmd.upgradeScriptPath, details))

        upgrade_log_dir = "/var/log/zstack/zmigrate-upgrade"

        def upgrade_script_log_name():
            return "zmigrate-upgrade-%s.log" % datetime.datetime.now().strftime("%Y%m%d%H%M%S.%f")

        # Validate all SSH parameters BEFORE constructing shell commands.
        # This prevents TypeError/AttributeError when None values are used in
        # string formatting (e.g. %d with None port, os.path.basename(None)).
        try:
            validate_ssh_port(target_host_ssh_port)
            validate_ssh_username(target_host_ssh_username)
            validate_ssh_host_ip(target_host_ip)
            validate_ssh_path(cmd.upgradePackagePath, param_name="upgradePackagePath", allow_relative=False)
            validate_ssh_path(cmd.upgradePackageTargetPath, param_name="upgradePackageTargetPath", allow_relative=False)
            validate_ssh_script_path(cmd.upgradeScriptPath, param_name="upgradeScriptPath", allow_relative=False)
        except SSHValidationError as e:
            rsp.success = False
            rsp.error = "parameter validation failed: %s" % str(e)
            return jsonobject.dumps(rsp)

        target_host_ssh_port = int(target_host_ssh_port)

        if not os.path.isfile(cmd.upgradePackagePath):
            rsp.success = False
            rsp.error = "local upgrade package file not found: %s" % cmd.upgradePackagePath
            return jsonobject.dumps(rsp)

        # Zip-Slip pre-check: verify no path escapes before sending to remote host.
        try:
            CephAgent.check_tar_archive_safety(cmd.upgradePackagePath)
        except (CephAgent.PathEscapeError, Exception) as e:
            rsp.success = False
            rsp.error = "failed to verify upgrade package contents: %s" % str(e)
            return jsonobject.dumps(rsp)

        # Ensure the upgrade script is within the target directory to prevent
        # execution of arbitrary scripts on the remote host.
        norm_script = os.path.normpath(cmd.upgradeScriptPath)
        norm_target = os.path.normpath(cmd.upgradePackageTargetPath)
        if not norm_script.startswith(norm_target + '/'):
            rsp.success = False
            rsp.error = "upgradeScriptPath must be within upgradePackageTargetPath (%s not under %s)" % (
                cmd.upgradeScriptPath, cmd.upgradePackageTargetPath)
            return jsonobject.dumps(rsp)

        random_suffix = binascii.hexlify(os.urandom(4)).decode('utf-8')
        remote_sudo_passwd_file_name = "su_%d_%s" % (int(time.time()), random_suffix)
        remote_sudo_passwd_file_path = "%s/%s" % (cmd.upgradePackageTargetPath, remote_sudo_passwd_file_name)
        package_name = os.path.basename(cmd.upgradePackagePath)
        target_upgrade_package_path = "%s/%s" % (cmd.upgradePackageTargetPath, package_name)

        upgrade_package_source_path = linux.shellquote(cmd.upgradePackagePath)
        upgrade_package_target_path = linux.shellquote(cmd.upgradePackageTargetPath)
        upgrade_script_path = linux.shellquote(cmd.upgradeScriptPath)
        quoted_remote_sudo_passwd_file_path = linux.shellquote(remote_sudo_passwd_file_path)

        # Write SSH password to tmpfs (/dev/shm) to avoid touching persistent storage.
        # Use delete=False so the file is not auto-removed on close(); we manage
        # cleanup ourselves in the finally block to guarantee removal.
        shm_dir = '/dev/shm' if os.path.isdir('/dev/shm') else None
        ssh_passwd_tmp = None
        ssh_passwd_file = None
        sudo_passwd_file_cleanup_needed = False
        file_copied = False
        delete_upgrade_package = None
        delete_remote_sudo_passwd_file = None
        try:
            ssh_passwd_tmp = tempfile.NamedTemporaryFile(mode='wb', prefix='ssh_', suffix='.tmp',
                                                         dir=shm_dir, delete=False)
            ssh_passwd_file = ssh_passwd_tmp.name
            ssh_passwd_tmp.write(target_host_ssh_password)
            ssh_passwd_tmp.flush()
            ssh_passwd_tmp.close()
            os.chmod(ssh_passwd_file, 0o600)
            quoted_ssh_passwd_file = linux.shellquote(ssh_passwd_file)
            quoted_ssh_target_ip = linux.shellquote(ssh_target_ip)
            quoted_ssh_username = linux.shellquote(target_host_ssh_username)
            logger.warning("SSH connection to %s uses StrictHostKeyChecking=no; "
                           "host key verification is disabled. This is susceptible to MITM attacks "
                           "in untrusted network environments.", ssh_target_ip)
            sshpass_cmd_header = "sshpass -f %s ssh -p %d -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30 %s@%s " % (quoted_ssh_passwd_file, target_host_ssh_port,
                                                                     quoted_ssh_username, quoted_ssh_target_ip)

            def create_upgrade_package_target_path():
                logger.info("creating upgrade package target path on remote host: %s" % cmd.upgradePackageTargetPath)
                cmd_str = '%s "mkdir -p %s"' % (sshpass_cmd_header, upgrade_package_target_path)
                r, _, e = bash.bash_roe(cmd_str)
                if r != 0:
                    raise Exception(stage_error("create-target-directory", e))

            def copy_file():
                logger.info("copying upgrade package to remote host: %s" % ssh_target_ip)
                cmd_str = 'timeout 1200 sshpass -f %s scp -P %d -o ConnectTimeout=30 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %s %s@%s:%s' % (
                    quoted_ssh_passwd_file, target_host_ssh_port,
                    upgrade_package_source_path, quoted_ssh_username,
                    quoted_ssh_target_ip, linux.shellquote(target_upgrade_package_path))
                r, _, e = bash.bash_roe(cmd_str)
                if r == 124:
                    raise Exception(stage_error("copy-upgrade-package", "scp timed out after 1200 seconds"))
                if r != 0:
                    raise Exception(stage_error("copy-upgrade-package", e))

            def create_remote_sudo_passwd_file():
                logger.info("creating remote sudo passwd file on remote host")
                # Use 'umask 077' so the file is created with 0600 permissions
                # from the start, eliminating the TOCTOU race window that would
                # exist with 'touch FILE && chmod 600 FILE'.
                create_passwd_file_cmd = '{0} "umask 077 && cat > {1}"'.format(
                    sshpass_cmd_header, quoted_remote_sudo_passwd_file_path)
                passwd_process = subprocess.Popen(create_passwd_file_cmd, shell=True, stdin=subprocess.PIPE,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                # target_host_ssh_password is kept as raw bytes (str in Python 2).
                passwd_input = target_host_ssh_password

                _, passwd_error = passwd_process.communicate(input=passwd_input)
                passwd_error = passwd_error.decode('utf-8') if passwd_error else ""
                if passwd_process.returncode != 0:
                    raise Exception(stage_error("create-remote-sudo-password-file", passwd_error))

            def unzip_upgrade_package():
                logger.info("unzipping upgrade package on remote host")
                cmd_str = '%s "sudo -S < %s tar --no-same-owner --no-same-permissions -zxf %s -C %s"' % (
                    sshpass_cmd_header, quoted_remote_sudo_passwd_file_path,
                    linux.shellquote(target_upgrade_package_path),
                    upgrade_package_target_path)
                r, _, e = bash.bash_roe(cmd_str)
                if r != 0:
                    raise Exception(stage_error("extract-upgrade-package", e))

            def run_upgrade_script():
                logger.info("running upgrade script on remote host: %s" % cmd.upgradeScriptPath)
                script_timeout = getattr(cmd, 'upgradeScriptTimeout', None) or self._DEFAULT_UPGRADE_SCRIPT_TIMEOUT
                try:
                    script_timeout = int(script_timeout)
                except (ValueError, TypeError):
                    script_timeout = self._DEFAULT_UPGRADE_SCRIPT_TIMEOUT
                # cd to the script's directory so relative paths inside
                # upgrade.sh (e.g. "source cmd.sh") resolve correctly.
                upgrade_script_dir = linux.shellquote(os.path.dirname(cmd.upgradeScriptPath))
                upgrade_script_log_path = "%s/%s" % (upgrade_log_dir, upgrade_script_log_name())
                run_script_inner_cmd = linux.shellquote("timeout %d %s > %s 2>&1" % (
                    script_timeout, upgrade_script_path, linux.shellquote(upgrade_script_log_path)))
                cmd_str = (
                    '{0} "sudo -S < {1} mkdir -p {6}'
                    ' && sudo -S < {1} chmod +x {2}'
                    ' && cd {4}'
                    ' && sudo -S < {1} sh -c {5}"'
                ).format(
                    sshpass_cmd_header,
                    quoted_remote_sudo_passwd_file_path,
                    upgrade_script_path,
                    script_timeout,
                    upgrade_script_dir,
                    run_script_inner_cmd,
                    linux.shellquote(upgrade_log_dir))
                r, _, e = bash.bash_roe(cmd_str)

                def append_stage_detail(details, name, value):
                    value = str(value).strip() if value else ""
                    details = str(details).strip() if details else ""
                    if not value:
                        return details
                    if not details:
                        return "%s=%s" % (name, value)
                    return "%s; %s=%s" % (details, name, value)

                def read_upgrade_script_log_tail():
                    tail_cmd = '%s "sudo -S < %s tail -n 80 %s"' % (
                        sshpass_cmd_header,
                        quoted_remote_sudo_passwd_file_path,
                        linux.shellquote(upgrade_script_log_path))
                    tail_ret, tail, tail_err = bash.bash_roe(tail_cmd)
                    if tail_ret != 0:
                        logger.warn("failed to read remote upgrade script log %s: %s" % (
                            upgrade_script_log_path, tail_err))
                        return ""
                    return str(tail).strip()

                if r == 124:
                    logger.warn("upgrade script timed out on %s, log path: %s" % (
                        target_host_ip, upgrade_script_log_path))
                    tail = read_upgrade_script_log_tail()
                    details = "upgrade.sh timed out after %d seconds" % script_timeout
                    details = append_stage_detail(details, "logTail", tail)
                    details = append_stage_detail(details, "logPath", upgrade_script_log_path)
                    raise Exception(stage_error("execute-upgrade-script", details))
                elif r != 0:
                    logger.warn("upgrade script failed on %s, log path: %s" % (
                        target_host_ip, upgrade_script_log_path))
                    details = "upgrade.sh failed (exit code %d): %s" % (r, e)
                    tail = read_upgrade_script_log_tail()
                    if not str(e).strip() and str(tail).strip():
                        details = "upgrade.sh failed (exit code %d): %s" % (r, tail)
                    elif str(tail).strip():
                        details = append_stage_detail(details, "logTail", tail)
                    details = append_stage_detail(details, "logPath", upgrade_script_log_path)
                    raise Exception(stage_error("execute-upgrade-script", details))
                logger.info("saved upgrade script log on target host: %s" % upgrade_script_log_path)

            def delete_remote_sudo_passwd_file():
                logger.info("deleting remote sudo passwd file on remote host")
                # Retry deletion to reduce risk of leaving password file on remote host.
                # Use 'shred -u' for secure erasure when available, fall back to 'rm -f'.
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        cmd_str = '%s "shred -u %s 2>/dev/null || rm -f %s"' % (
                            sshpass_cmd_header,
                            quoted_remote_sudo_passwd_file_path,
                            quoted_remote_sudo_passwd_file_path)
                        r, _, e = bash.bash_roe(cmd_str)
                        if r == 0:
                            return
                        logger.warning("attempt %d/%d to delete remote sudo passwd file failed: %s"
                                       % (attempt + 1, max_retries, e))
                    except Exception:
                        logger.warning("attempt %d/%d to delete remote sudo passwd file failed"
                                       % (attempt + 1, max_retries))
                    if attempt < max_retries - 1:
                        time.sleep(1)
                logger.error("SECURITY WARNING: failed to delete remote sudo passwd file after %d attempts: %s"
                             % (max_retries, remote_sudo_passwd_file_path))

            def delete_upgrade_package():
                logger.info("deleting upgrade package on remote host: %s" % cmd.upgradePackageTargetPath)
                try:
                    cmd_str = ('%s "sudo -S < %s rm -rf %s"' % (
                        sshpass_cmd_header, quoted_remote_sudo_passwd_file_path, upgrade_package_target_path))
                    bash.bash_roe(cmd_str)
                except Exception:
                    logger.warning("failed to delete remote upgrade target directory: %s"
                                   % cmd.upgradePackageTargetPath)

            def verify_no_path_escape():
                """Post-extraction Zip-Slip check: ensure all extracted files
                stay within the target directory on the remote host."""
                logger.info("verifying no path escape after extraction on remote host")
                # Escape regex metacharacters (e.g. '.') in the path so that
                # grep -E treats them as literal characters.
                escaped_target = re.escape(cmd.upgradePackageTargetPath)
                verify_cmd = (
                    '%s "find %s -exec realpath {} + 2>/dev/null'
                    ' | grep -v -E \'^%s(/|$)\' || true"'
                    % (sshpass_cmd_header,
                       upgrade_package_target_path,
                       escaped_target))
                r, o, e = bash.bash_roe(verify_cmd)
                if r != 0:
                    raise Exception(stage_error("verify-extracted-paths", e))
                escaped = [line.strip() for line in o.strip().splitlines() if line.strip()]
                if escaped:
                    raise Exception(stage_error("verify-extracted-paths",
                                                "path escape detected after remote extraction, escaped paths: %s"
                                                % "; ".join(escaped[:10])))

            create_upgrade_package_target_path()
            copy_file()
            file_copied = True
            sudo_passwd_file_cleanup_needed = True
            create_remote_sudo_passwd_file()
            unzip_upgrade_package()
            verify_no_path_escape()
            run_upgrade_script()
            rsp.upgradeScriptPath = cmd.upgradeScriptPath
        except Exception as e:
            rsp.success = False
            rsp.error = "execution failed: %s" % str(e)
            return jsonobject.dumps(rsp)
        finally:
            if file_copied and delete_upgrade_package is not None:
                delete_upgrade_package()
            if sudo_passwd_file_cleanup_needed and delete_remote_sudo_passwd_file is not None:
                delete_remote_sudo_passwd_file()
            if ssh_passwd_tmp is not None:
                try:
                    ssh_passwd_tmp.close()
                except Exception:
                    pass
            if ssh_passwd_file is not None:
                try:
                    os.unlink(ssh_passwd_file)
                except OSError:
                    pass

        return jsonobject.dumps(rsp)

class CephDaemon(daemon.Daemon):
    def __init__(self, pidfile, py_process_name):
        super(CephDaemon, self).__init__(pidfile, py_process_name)

    def run(self):
        self.agent = CephAgent()
        self.agent.http_server.start()
