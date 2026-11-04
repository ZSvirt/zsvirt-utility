# -*- coding: utf-8 -*-
from __future__ import absolute_import


REGISTRY_ROOT = '/etc/zstack/kvmagent/plugins.d'


class ExternalPluginLayoutUnavailable(Exception):
    pass


def install_registry_root(host_post_info, run_remote_command):
    command = ('install -d -o root -g root -m 0755 -- %s' %
               REGISTRY_ROOT)
    status = run_remote_command(
        command, host_post_info, return_status=True)
    if not status:
        raise ExternalPluginLayoutUnavailable(
            'failed to install external plugin registry root')
