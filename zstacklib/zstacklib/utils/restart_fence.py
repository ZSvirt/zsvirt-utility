# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import threading
import time


def monotonic_time():
    monotonic = getattr(time, 'monotonic', None)
    if monotonic is not None:
        return monotonic()
    # Python 2 has no time.monotonic().  KVM Agent runs on Unix, where the
    # elapsed field from os.times() advances independently of wall-clock
    # adjustments.
    return os.times()[4]


class AgentRestartFence(object):
    """Drain asynchronous Agent calls and fence admission before restart."""
    OWNER_CONFLICT = 'OWNER_CONFLICT'
    DRAIN_IN_PROGRESS = 'DRAIN_IN_PROGRESS'
    DRAIN_TIMEOUT = 'DRAIN_TIMEOUT'

    _condition = threading.Condition()
    _fenced = False
    _active_requests = 0
    _generation = 0
    _owner_operation_id = None

    @classmethod
    def _notify_waiters(cls):
        notify = getattr(cls._condition, 'notify_all', None)
        (notify or cls._condition.notifyAll)()

    @classmethod
    def enter_request(cls):
        with cls._condition:
            if cls._fenced:
                return False
            cls._active_requests += 1
            return True

    @classmethod
    def leave_request(cls):
        with cls._condition:
            if cls._active_requests > 0:
                cls._active_requests -= 1
            cls._notify_waiters()

    @classmethod
    def snapshot(cls):
        with cls._condition:
            return {
                'state': 'FENCED' if cls._fenced else
                         ('BUSY' if cls._active_requests else 'IDLE'),
                'activeRequestCount': cls._active_requests,
                'acceptingNewRequests': not cls._fenced,
            }

    @classmethod
    def acquire(cls, operation_id, drain_timeout_seconds, lease_seconds):
        deadline = monotonic_time() + drain_timeout_seconds
        with cls._condition:
            if cls._fenced:
                if operation_id != cls._owner_operation_id:
                    return False, cls.snapshot(), cls.OWNER_CONFLICT
                if cls._active_requests:
                    return False, cls.snapshot(), cls.DRAIN_IN_PROGRESS
                # A restart command may fail before the old process exits.  A
                # retry of the same operation can renew an already drained
                # fence without allowing another restart owner to take it.
                cls._generation += 1
                generation = cls._generation
            else:
                cls._fenced = True
                cls._owner_operation_id = operation_id
                cls._generation += 1
                generation = cls._generation

            while cls._active_requests:
                remaining = deadline - monotonic_time()
                if remaining <= 0:
                    cls._fenced = False
                    cls._owner_operation_id = None
                    cls._notify_waiters()
                    return False, cls.snapshot(), cls.DRAIN_TIMEOUT
                cls._condition.wait(remaining)

        timer = threading.Timer(lease_seconds, cls._release_generation,
                                args=(generation,))
        timer.daemon = True
        timer.start()
        return True, cls.snapshot(), None

    @classmethod
    def _release_generation(cls, generation):
        with cls._condition:
            if cls._fenced and cls._generation == generation:
                cls._fenced = False
                cls._owner_operation_id = None
                cls._notify_waiters()

    @classmethod
    def reset_for_test(cls):
        with cls._condition:
            cls._generation += 1
            cls._fenced = False
            cls._owner_operation_id = None
            cls._active_requests = 0
            cls._notify_waiters()
