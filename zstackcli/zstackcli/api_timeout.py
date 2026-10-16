"""Resolve CLI waiting budgets without overriding backend timeout selection."""

import re

from apibinding import api, inventory


def parse_api_timeout(value):
    """Parse the server's integer milliseconds or s/m/h/d/w/y time format."""
    match = re.match(r'^([0-9]+)(.*)\Z', str(value), re.S)
    multipliers = {'': 1, 's': 1000, 'm': 60000, 'h': 3600000,
                   'd': 86400000, 'w': 604800000, 'y': 31536000000}
    if match:
        unit = match.group(2).strip().lower()
        if unit in multipliers:
            timeout = int(match.group(1)) * multipliers[unit]
            if timeout > 0:
                return timeout
    raise api.ApiError('Invalid apiTimeout value[%s]: expected positive milliseconds '
                       'or an integer with s/m/h/d/w/y' % value)


def call_with_timeout(client, msg, apievent=None, skip_config=False):
    wait_timeout = None
    if msg.timeout is not None:
        if not re.match(r'^[0-9]+\Z', str(msg.timeout)) or int(msg.timeout) <= 0:
            raise api.ApiError('timeout must be a positive integer in milliseconds')
        msg.timeout = int(msg.timeout)
        wait_timeout = msg.timeout
    elif (not skip_config and getattr(getattr(msg, 'session', None), 'uuid', None)
          and getattr(msg, 'RESPONSE_NAME', '').endswith('Event')):
        # Generated messages are flat classes; RESPONSE_NAME distinguishes
        # asynchronous Event responses from synchronous Reply responses.
        query = inventory.APIQueryGlobalConfigMsg()
        query.session = msg.session
        query.conditions = [
            {'name': 'category', 'op': '=', 'value': 'apiTimeout'},
            {'name': 'name', 'op': '=', 'value': msg.FULL_NAME},
        ]
        query.limit = 2
        try:
            _, reply = client.sync_call(query, fail_soon=True)
        except Exception as error:
            raise api.ApiError('Cannot read apiTimeout for API[%s]: %s' % (msg.FULL_NAME, error))
        rows = reply.inventories or []
        if len(rows) != 1:
            raise api.ApiError('Expected one apiTimeout configuration for API[%s], found %d' %
                               (msg.FULL_NAME, len(rows)))
        wait_timeout = parse_api_timeout(rows[0].value)

    # Only explicitly supplied timeout belongs in the business request.
    return client.async_call_wait_for_complete(msg, apievent=apievent,
                                              fail_soon=True, wait_timeout=wait_timeout)
