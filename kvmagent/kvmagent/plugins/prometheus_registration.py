#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Process-wide, thread-safe Prometheus collector registration guard."""

import os
import threading


_lock = threading.RLock()
_installed_keys = set()
_callback_keys = set()


def register_once(registry, collector, key):
    """Register collector once for key; failed registrations remain retryable."""
    with _lock:
        if key in _installed_keys:
            return False
        registry.register(collector)
        _installed_keys.add(key)
        return True


def register_callback_once(register, callback):
    """Register a callback once even if its defining file is imported by aliases."""
    code = getattr(callback, "__code__", getattr(callback, "func_code", None))
    filename = os.path.realpath(code.co_filename) if code is not None else callback.__module__
    key = (filename, callback.__name__)
    with _lock:
        if key in _callback_keys:
            return False
        register(callback)
        _callback_keys.add(key)
        return True


def _reset_for_test():
    with _lock:
        _installed_keys.clear()
        _callback_keys.clear()
