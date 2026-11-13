#!/usr/bin/env python
# encoding: utf-8
import argparse
import ast
import datetime
import hashlib
import json
import os
import sys

from zstacklib import *


def shell_quote(value):
    if value is None:
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_argument_dict(value, argument_parser):
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            argument_parser.error("-e must be a JSON or Python dictionary")
    if not isinstance(parsed, dict):
        argument_parser.error("-e must contain a dictionary")
    return parsed


logger_dir = "/var/log/zstack/"
create_log(logger_dir)
banner("Starting to deploy zns proxy")
start_time = datetime.datetime.now()

znsproxy_action = "install"
src_pkg_znsproxy = ""
dst_pkg_znsproxy = "/var/lib/zstack/zns-proxy/package/zns-proxy.bin"
znsproxy_health_url = "http://127.0.0.1:7890/zns-proxy/api/v1/health"
post_url = ""
chrony_servers = None
remote_user = "root"
remote_pass = None
remote_port = None
host_uuid = None

parser = argparse.ArgumentParser(description="Deploy zns proxy to host")
parser.add_argument("-i", type=str, help="specify inventory host file default=/etc/ansible/hosts")
parser.add_argument("--private-key", type=str, help="use this file to authenticate the connection")
parser.add_argument("-e", type=str, help="set additional variables as key=value or YAML/JSON")
args = parser.parse_args()
argument_dict = parse_argument_dict(args.e, parser)
locals().update(argument_dict)

host_post_info = HostPostInfo()
host_post_info.host_inventory = args.i
host_post_info.host = host
host_post_info.host_uuid = host_uuid
host_post_info.post_url = post_url
host_post_info.chrony_servers = chrony_servers
host_post_info.private_key = args.private_key
host_post_info.remote_user = remote_user
host_post_info.remote_pass = remote_pass
host_post_info.remote_port = remote_port
if remote_pass is not None and remote_user != "root":
    host_post_info.become = True

if znsproxy_action in ["purge", "cleanup"]:
    if znsproxy_action == "cleanup":
        handle_ansible_info(
            "WARNING: znsproxy_action=cleanup is deprecated; use purge for Cloud-side full uninstall only",
            host_post_info,
            "WARNING"
        )
    # Cloud-side full uninstall only. Do not use this action for ZNS host
    # deprovision, because zns-proxy is owned by the Cloud/MN host lifecycle.
    purge_cmd = (
        "systemctl stop zstack-zns-agent.service || true; "
        "systemctl disable zstack-zns-agent.service || true; "
        "rm -f /usr/lib/systemd/system/zstack-zns-agent.service; "
        "rm -f /etc/systemd/system/zstack-zns-agent.service; "
        "rm -f /etc/zstack-zns/zns-agent.toml; "
        "rm -f /etc/logrotate.d/zns-agent; "
        "rm -rf /usr/local/zstack/zns-agent; "
        "rm -rf /var/lib/zstack/zns-agent/package; "
        "systemctl stop zstack-zns-proxy.service || true; "
        "systemctl disable zstack-zns-proxy.service || true; "
        "rm -f /usr/lib/systemd/system/zstack-zns-proxy.service; "
        "rm -f /etc/systemd/system/zstack-zns-proxy.service; "
        "rm -f /etc/zstack-zns/zns-proxy.toml; "
        "rm -f /etc/logrotate.d/zns-proxy; "
        "rm -rf /usr/local/zstack/zns-proxy; "
        "rm -rf /var/lib/zstack/zns-proxy/package; "
        "systemctl daemon-reload"
    )
    run_remote_command(purge_cmd, host_post_info)
    host_post_info.start_time = start_time
    handle_ansible_info("SUCC: Purge zns proxy and agent successful", host_post_info, "INFO")
    sys.exit(0)

if znsproxy_action != "install":
    error("unsupported znsproxy_action: %s, supported actions: install, purge" % znsproxy_action)

if not src_pkg_znsproxy:
    error("src_pkg_znsproxy is empty")

package_digest = hashlib.sha256()
with open(src_pkg_znsproxy, 'rb') as package_file:
    for chunk in iter(lambda: package_file.read(1024 * 1024), b''):
        package_digest.update(chunk)
package_sha256 = package_digest.hexdigest()
installed_sha256_path = dst_pkg_znsproxy + '.installed.sha256'

package_dir = os.path.dirname(dst_pkg_znsproxy)
run_remote_command("mkdir -p %s" % shell_quote(package_dir), host_post_info)

copy_arg = CopyArg()
copy_arg.src = src_pkg_znsproxy
copy_arg.dest = dst_pkg_znsproxy
copy_arg.args = "force=yes mode=0755"
copy(copy_arg, host_post_info)

package_installed = run_remote_command(
    'test -f %s && test "$(cat %s)" = %s' % (
        shell_quote(installed_sha256_path), shell_quote(installed_sha256_path),
        shell_quote(package_sha256)),
    host_post_info,
    return_status=True
)

health_ok = run_remote_command(
    "curl -fsS --max-time 10 %s" % shell_quote(znsproxy_health_url),
    host_post_info,
    return_status=True
)

if package_installed and health_ok:
    host_post_info.start_time = start_time
    handle_ansible_info("SUCC: zns proxy is already ready", host_post_info, "INFO")
    sys.exit(0)

run_remote_command("rm -f %s" % shell_quote(installed_sha256_path), host_post_info)
run_remote_command("chmod 0755 %s" % shell_quote(dst_pkg_znsproxy), host_post_info)
run_remote_command("%s install --listen-address 0.0.0.0:7890" % shell_quote(dst_pkg_znsproxy), host_post_info)
run_remote_command("curl -fsS --max-time 10 %s" % shell_quote(znsproxy_health_url), host_post_info)
run_remote_command("printf '%%s\\n' %s > %s && mv -f %s %s" % (
    shell_quote(package_sha256), shell_quote(installed_sha256_path + '.tmp'),
    shell_quote(installed_sha256_path + '.tmp'), shell_quote(installed_sha256_path)), host_post_info)

host_post_info.start_time = start_time
handle_ansible_info("SUCC: Deploy zns proxy successful", host_post_info, "INFO")
sys.exit(0)
