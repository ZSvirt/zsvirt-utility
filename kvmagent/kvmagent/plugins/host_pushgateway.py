import glob
import logging
import re


DEFAULT_HOST_PUSHGATEWAY_AUTH_HEADER = "Basic YWRtaW46enN0YWNrQDEyMw=="
LIGHTTPD_CONF_GLOBS = [
    "/var/lib/zstack/userdata/*/lighttpd.conf",
    "/var/lib/zstack/tf_userdata/lighttpd.conf",
]
AUTH_HEADER_PATTERN = re.compile(r'Authorization"\s*=>\s*"([^"]+)"')
logger = logging.getLogger(__name__)


def _get_default_conf_paths():
    conf_paths = []
    seen_paths = set()
    for pattern in LIGHTTPD_CONF_GLOBS:
        for conf_path in sorted(glob.glob(pattern)):
            if conf_path in seen_paths:
                continue
            seen_paths.add(conf_path)
            conf_paths.append(conf_path)
    return conf_paths


def get_auth_header(conf_paths=None):
    if conf_paths is None:
        conf_paths = _get_default_conf_paths()

    for conf_path in conf_paths:
        try:
            with open(conf_path, "r") as fd:
                match = AUTH_HEADER_PATTERN.search(fd.read())
                if match:
                    return match.group(1)
        except (IOError, OSError) as e:
            logger.warning("failed to read host pushgateway auth config[%s]: %s" % (conf_path, e))

    return DEFAULT_HOST_PUSHGATEWAY_AUTH_HEADER


def make_push_metrics_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": get_auth_header()
    }


def make_get_metrics_headers():
    return {
        "Content-Type": "text/plain",
        "Authorization": get_auth_header()
    }


def make_delete_metric_headers():
    return {
        "Authorization": get_auth_header()
    }
