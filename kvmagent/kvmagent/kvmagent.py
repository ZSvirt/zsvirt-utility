'''

@author: frank
'''

from zstacklib.utils import plugin
from zstacklib.utils import http
from zstacklib.utils import log
from zstacklib.utils import jsonobject
from zstacklib.utils import daemon
from zstacklib.utils import linux
from zstacklib.utils import qemu
from . import external_plugin

import functools
import os.path
import pprint
import platform
import re
import subprocess
import sys
import threading
import traceback

# TODO: -> python3: delete it
reload(sys)
sys.setdefaultencoding('utf-8')

try:
    from subprocess import getoutput
except ImportError:
    from commands import getoutput
# TODO: -> python3: END

logger = log.get_logger(__name__)


class KvmError(Exception):
    '''kvm error'''

class BlockJobError(Exception):
    '''block job error'''

class KvmAgent(plugin.Plugin):
    '''
    classdocs
    '''

    def __init__(self):
        '''
        Constructor
        '''
        linux.recover_fake_dead('kvmagent')
        super(KvmAgent, self).__init__()

ha_cleanup_handlers = [] # type: list[callable]

def register_ha_cleanup_handler(handler):
    # type: (callable) -> None
    logger.debug('start registered ha cleanup handler %s' % handler)
    global ha_cleanup_handlers
    ha_cleanup_handlers.append(handler)
    logger.debug('success registered ha cleanup handlers %s' % ha_cleanup_handlers)

metric_collectors = []  # type: list[callable]

def register_prometheus_collector(collector):
    # type: (callable) -> None
    logger.debug('start registered %s' % collector)
    global metric_collectors
    metric_collectors.append(collector)
    logger.debug('success registered %s' % metric_collectors)

_rest_service = None
_qemu_path = None
host_arch = linux.HOST_ARCH

def new_rest_service(config={}):
    global _rest_service
    if not _rest_service:
        _rest_service = KvmRESTService(config)
    return _rest_service

def get_http_server():
    return _rest_service.http_server

def get_host_yum_release():
    return getoutput("rpm -q zstack-release 2>/dev/null | awk -F'-' '{print $3}'").strip()

# TODO: -> python2:
def get_host_os_type():
    dist, version, _ = platform.dist()
    if dist.lower() in linux.DIST_WITH_RPM_DEB:
        dist = "%s%s" % (dist.lower(), version)
    is_debian = any(map(lambda x: x in dist.lower(), linux.DEB_BASED_OS))
    return 'debian' if is_debian else 'redhat'
# TODO: -> python2: END
# TODO: -> python3.11:
# def get_host_os_type():
#     os_info = platform.freedesktop_os_release()
#     dist, version = os_info['ID'], re.sub(r'[a-zA-Z]+$', '', os_info['VERSION_ID'])
#     if dist.lower() in linux.DIST_WITH_RPM_DEB:
#         dist = "%s%s" % (dist.lower(), version)
#     is_debian = any([x in dist.lower() for x in linux.DEB_BASED_OS])
#     return 'debian' if is_debian else 'redhat'
# TODO: -> python3.11: END

def get_qemu_path():
    global _qemu_path
    if not _qemu_path:
        try:
            _qemu_path = qemu.get_path()
        except Exception as e:
            raise KvmError(str(e))

    return _qemu_path
        
    
class KvmRESTService(object):
    http_server = http.HttpServer()
    http_server.logfile_path = log.get_logfile_path()
    
    NO_DAEMON = 'no_deamon'
    PLUGIN_PATH = 'plugin_path'
    WORKSPACE = 'workspace'
    EXTERNAL_PLUGIN_REGISTRY_ROOT = 'external_plugin_registry_root'
    EXTERNAL_PLUGIN_MANAGED_ROOT = 'external_plugin_managed_root'
    EXTERNAL_PLUGIN_EXPECTED_UID = 'external_plugin_expected_uid'
    PLUGIN_DEPENDENCY_READY_DEADLINE = 'plugin_dependency_ready_deadline'
    
    def __init__(self, config={}):
        self.config = config
        plugin_path = self._get_config(self.PLUGIN_PATH)
        if not plugin_path:
            plugin_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plugins')
        self.plugin_path = plugin_path
        self.external_plugin_rgty = external_plugin.ExternalPluginRegistry(
            self.http_server,
            registry_root=self._get_config(self.EXTERNAL_PLUGIN_REGISTRY_ROOT)
            or external_plugin.DEFAULT_REGISTRY_ROOT,
            managed_root=self._get_config(self.EXTERNAL_PLUGIN_MANAGED_ROOT)
            or external_plugin.DEFAULT_MANAGED_ROOT,
            expected_uid=self.config.get(self.EXTERNAL_PLUGIN_EXPECTED_UID, 0),
            dependency_ready_deadline=int(
                self._get_config(self.PLUGIN_DEPENDENCY_READY_DEADLINE) or 30))
        self.plugin_rgty = plugin.PluginRegistry(self.plugin_path)
        self.external_plugin_rgty.register_status_endpoint()
    
    def _get_config(self, name):
        return self.config.get(name)

    def _start_external_plugins(self, config):
        worker = threading.Thread(
            target=self.external_plugin_rgty.load_and_start,
            args=(config,))
        worker.daemon = True
        worker.start()
        self.external_plugin_loader_thread = worker
        return worker
    
    def start(self, in_thread=True):
        config = {}
        self.plugin_rgty.configure_plugins(config)
        self.plugin_rgty.start_plugins()
        if in_thread:
            self.http_server.start_in_thread(
                lambda: self._start_external_plugins(config))
        else:
            self.http_server.start(
                lambda: self._start_external_plugins(config))
    
    def stop(self):
        request_external_stop = getattr(
            self.external_plugin_rgty, 'request_stop', None)
        if request_external_stop is not None:
            request_external_stop()
        self.http_server.stop()
        self.plugin_rgty.stop_plugins()
        self.external_plugin_rgty.stop()

class AgentResponse(object):
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error if error else ''

class AgentCommand(object):
    def __init__(self):
        pass

def _build_url_for_test(paths):
    builder = http.UriBuilder('http://localhost:7070')
    for p in paths:
        builder.add_path(p)
    return builder.build()

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


def deleteImage(path):
     linux.rm_file_checked(path)
     logger.debug('successfully delete %s' % path)
     if (path.endswith('.qcow2')):
        imfFiles = [".imf",".imf2"]
        for f in imfFiles:
            filePath = path.replace(".qcow2", f)
            linux.rm_file_force(filePath)
     linux.rm_file_force('%s.json' % path)
     pdir = os.path.dirname(path)
     linux.rmdir_if_empty(pdir)


class KvmDaemon(daemon.Daemon):
    def __init__(self, pidfile, py_process_name, config={}):
        super(KvmDaemon, self).__init__(pidfile, py_process_name)
        
    def run(self):
        self.agent = new_rest_service()
        self.agent.start(in_thread=False)

SEND_COMMAND_URL = 'SEND_COMMAND_URL'
HOST_UUID = 'HOST_UUID'
VERSION = 'VERSION'
configs = {}
PHYSICALMEMORY_USAGE_ALARM_THRESHOLD = 'kvmagent.physicalmemory.usage.alarm.threshold'
PHYSICALMEMORY_USAGE_HARDLIMIT = 'kvmagent.physicalmemory.usage.hardlimit'
