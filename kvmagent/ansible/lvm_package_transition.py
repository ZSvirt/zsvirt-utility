# -*- coding: utf-8 -*-
"""Replace the openEuler distro LVM family with the SharedBlock build."""
from __future__ import absolute_import

try:
    from shlex import quote
except ImportError:
    from pipes import quote


class PackageTransitionError(Exception):
    pass


LVM_PACKAGES = ('lvm2', 'lvm2-libs', 'lvm2-lockd')
DM_PACKAGES = ('device-mapper', 'device-mapper-libs',
               'device-mapper-event', 'device-mapper-event-libs')
PACKAGES = LVM_PACKAGES + DM_PACKAGES
OLD_LVM = ('8', '2.03.14', '7.oe2203sp1', 'aarch64')
OLD_DM = ('8', '1.02.181', '7.oe2203sp1', 'aarch64')
NEW_LVM = ('0', '2.03.12', '13.oe2203sp1', 'aarch64')
NEW_DM = ('0', '1.02.177', '13.oe2203sp1', 'aarch64')


def _installed_packages(host, run_remote_command):
    status, output = run_remote_command(
        "rpm -qa --qf '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\\n'",
        host, return_status=True, return_output=True)
    if not status:
        raise PackageTransitionError('cannot read installed LVM/device-mapper versions')
    installed = {}
    for line in output.splitlines():
        fields = line.split('|')
        if fields[0] not in PACKAGES:
            continue
        if len(fields) != 5 or fields[0] in installed:
            raise PackageTransitionError('ambiguous installed package: %s' % line)
        installed[fields[0]] = tuple(fields[1:])
    return installed


def migrate_legacy_packages(release, arch, repo, skipped, host, run_remote_command):
    if release != 'h2203sp1o' or arch != 'aarch64' or repo == 'false':
        return False

    installed = _installed_packages(host, run_remote_command)
    old = dict((name, OLD_LVM if name in LVM_PACKAGES else OLD_DM) for name in PACKAGES)
    target = dict((name, NEW_LVM if name in LVM_PACKAGES else NEW_DM) for name in PACKAGES)
    if not any(version == old[name] for name, version in installed.items()):
        return False
    if set(skipped).intersection(PACKAGES):
        raise PackageTransitionError('cannot partially skip the legacy LVM/device-mapper transition')
    if any(version not in (old[name], target[name]) for name, version in installed.items()):
        raise PackageTransitionError('unexpected mixed LVM/device-mapper versions; refusing replacement')

    # openEuler yum is DNF. Explicit NEVRAs allow downgrade and new split libs
    # in one transaction. By-name install retains the distro's higher Epoch.
    # Keep dependency/conflict checking enabled; never erase dependants first.
    specs = ['%s-%s:%s-%s.%s' % ((name,) + target[name]) for name in PACKAGES]
    command = ('yum --refresh --disablerepo=* --enablerepo=%s '
               '--setopt=install_weak_deps=False install -y %s' %
               (quote(repo), ' '.join(quote(spec) for spec in specs)))
    if not run_remote_command(command, host, return_status=True):
        raise PackageTransitionError('failed to replace the legacy LVM/device-mapper package set')
    if _installed_packages(host, run_remote_command) != target:
        raise PackageTransitionError('LVM/device-mapper versions do not match the recovery package set')
    return True
