# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os


TRADITIONAL_UNIT = 'libvirtd.service'
MODULAR_SOCKET_UNIT = 'virtqemud.socket'
MODULAR_SERVICE_UNIT = 'virtqemud.service'
CONTROL_UNITS = (
    TRADITIONAL_UNIT,
    MODULAR_SOCKET_UNIT,
    MODULAR_SERVICE_UNIT,
)
USABLE_UNIT_FILE_STATES = frozenset((
    'enabled',
    'enabled-runtime',
    'static',
    'indirect',
    'generated',
))
DROPIN_DIRECTORY = '/etc/systemd/system/zstack-kvmagent.service.d'
DROPIN_PATH = DROPIN_DIRECTORY + '/10-libvirt-ordering.conf'

_DROPIN_FILENAMES = {
    TRADITIONAL_UNIT: 'zstack-kvmagent-libvirtd-ordering.conf',
    MODULAR_SOCKET_UNIT:
        'zstack-kvmagent-virtqemud-socket-ordering.conf',
    MODULAR_SERVICE_UNIT:
        'zstack-kvmagent-virtqemud-service-ordering.conf',
}

try:
    _STRING_TYPES = (basestring,)
except NameError:
    _STRING_TYPES = (str,)


class LibvirtControlError(Exception):
    pass


class LibvirtControlMalformed(LibvirtControlError):
    pass


class LibvirtControlAmbiguous(LibvirtControlError):
    pass


class LibvirtControlUnavailable(LibvirtControlError):
    pass


def _normalize_observations(observations, require_all=False):
    if not isinstance(observations, (list, tuple)):
        raise LibvirtControlMalformed('unit observations must be a list')

    normalized = []
    seen = set()
    required_fields = ('unit', 'load', 'active', 'unit_file')
    for row in observations:
        if not isinstance(row, dict):
            raise LibvirtControlMalformed('unit observation must be a mapping')
        if any(field not in row for field in required_fields):
            raise LibvirtControlMalformed(
                'unit observation is missing a required field')
        values = {}
        for field in required_fields:
            value = row[field]
            if not isinstance(value, _STRING_TYPES):
                raise LibvirtControlMalformed(
                    'unit observation fields must be strings')
            values[field] = value.strip()

        unit = values['unit']
        if unit not in CONTROL_UNITS:
            raise LibvirtControlMalformed('unknown libvirt unit: %s' % unit)
        if unit in seen:
            raise LibvirtControlMalformed(
                'duplicate libvirt unit observation: %s' % unit)
        if not values['load'] or not values['active']:
            raise LibvirtControlMalformed(
                'load and active states must not be empty')
        seen.add(unit)
        normalized.append(values)

    if require_all and seen != set(CONTROL_UNITS):
        missing = sorted(set(CONTROL_UNITS) - seen)
        raise LibvirtControlMalformed(
            'missing libvirt unit observations: %s' % ', '.join(missing))
    return normalized


def parse_unit_observations(text):
    if not isinstance(text, _STRING_TYPES):
        raise LibvirtControlMalformed('unit observation output must be text')
    observations = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        fields = [field.strip() for field in raw_line.split('|')]
        if len(fields) != 4:
            raise LibvirtControlMalformed(
                'malformed libvirt observation row %s' % line_number)
        observations.append({
            'unit': fields[0],
            'load': fields[1],
            'active': fields[2],
            'unit_file': fields[3],
        })
    return _normalize_observations(observations, require_all=True)


def _is_usable(observation):
    return (observation['load'] == 'loaded' and
            observation['unit_file'] in USABLE_UNIT_FILE_STATES)


def select_control_unit(observations):
    rows = _normalize_observations(observations)
    by_unit = dict((row['unit'], row) for row in rows)
    traditional_active = (
        by_unit.get(TRADITIONAL_UNIT, {}).get('active') == 'active')
    modular_active = [
        unit for unit in (MODULAR_SOCKET_UNIT, MODULAR_SERVICE_UNIT)
        if by_unit.get(unit, {}).get('active') == 'active'
    ]

    if traditional_active and modular_active:
        raise LibvirtControlAmbiguous(
            'traditional and modular libvirt units are both active')
    if traditional_active:
        return TRADITIONAL_UNIT
    if modular_active:
        if MODULAR_SOCKET_UNIT in modular_active:
            return MODULAR_SOCKET_UNIT
        return MODULAR_SERVICE_UNIT

    traditional_usable = (
        TRADITIONAL_UNIT in by_unit and
        _is_usable(by_unit[TRADITIONAL_UNIT]))
    modular_usable = [
        unit for unit in (MODULAR_SOCKET_UNIT, MODULAR_SERVICE_UNIT)
        if unit in by_unit and _is_usable(by_unit[unit])
    ]
    if traditional_usable and modular_usable:
        raise LibvirtControlAmbiguous(
            'traditional and modular libvirt unit families are both usable')
    if traditional_usable:
        return TRADITIONAL_UNIT
    if modular_usable:
        if MODULAR_SOCKET_UNIT in modular_usable:
            return MODULAR_SOCKET_UNIT
        return MODULAR_SERVICE_UNIT
    raise LibvirtControlUnavailable('no usable libvirt control unit found')


def dropin_filename(unit):
    try:
        return _DROPIN_FILENAMES[unit]
    except (KeyError, TypeError):
        raise LibvirtControlUnavailable(
            'unsupported libvirt control unit: %s' % unit)


def observation_command():
    units = ' '.join(CONTROL_UNITS)
    return (r'''for unit in %s; do '''
            r'''values="$(LC_ALL=C systemctl show "$unit" --no-pager '''
            r'''--property=LoadState --property=ActiveState '''
            r'''--property=UnitFileState)" || exit $?; '''
            r'''load="$(printf '%%s\n' "$values" | '''
            r'''sed -n 's/^LoadState=//p')"; '''
            r'''active="$(printf '%%s\n' "$values" | '''
            r'''sed -n 's/^ActiveState=//p')"; '''
            r'''unit_file="$(printf '%%s\n' "$values" | '''
            r'''sed -n 's/^UnitFileState=//p')"; '''
            r'''printf '%%s|%%s|%%s|%%s\n' '''
            r'''"$unit" "$load" "$active" "$unit_file"; done''' % units)


def install_ordering_dropin(file_root, host_post_info,
                            run_remote_command, copy_to_remote):
    status, output = run_remote_command(
        observation_command(), host_post_info,
        return_status=True, return_output=True)
    if not status:
        raise LibvirtControlUnavailable(
            'failed to observe libvirt systemd units')

    selected_unit = select_control_unit(parse_unit_observations(output))
    source = os.path.join(file_root, dropin_filename(selected_unit))
    run_remote_command(
        'mkdir -p %s' % DROPIN_DIRECTORY, host_post_info)
    copy_result = copy_to_remote(
        source, DROPIN_PATH, 'mode=0644', host_post_info)
    if not copy_result:
        raise LibvirtControlUnavailable(
            'failed to install libvirt ordering drop-in')
    run_remote_command('systemctl daemon-reload', host_post_info)
    return selected_unit
