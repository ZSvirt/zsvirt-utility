# -*- coding: utf-8 -*-
from __future__ import absolute_import

import copy


STATUS_PATH = "/kvmagent/plugins/status"
SCHEMA_VERSION = "1"


def plugin_status(record):
    """Create a detached, side-effect-free status projection."""
    return {
        "id": record.plugin_id,
        "state": record.state,
        "release": copy.deepcopy(record.release_status()),
        "failure": copy.deepcopy(record.failure),
        "dependency": copy.deepcopy(record.dependency),
        "registeredRoutes": sorted(record.registered_routes),
        "capabilities": copy.deepcopy(record.capabilities),
        "loadedRuntimeVersions": dict(record.loaded_runtime_versions),
        "nextStartVersions": dict(record.next_start_versions),
        "compatibilityState": record.compatibility_state,
        "lastCompatibilityCheckAt": record.last_compatibility_check_at,
        "restartRequired": record.restart_required,
        "stale": record.stale,
        "transitioning": record.transitioning,
    }


def status_envelope(records, observed_at, initialization_state="READY"):
    return {
        "success": True,
        "schemaVersion": SCHEMA_VERSION,
        "observedAt": observed_at,
        "initializationState": initialization_state,
        "plugins": [plugin_status(record) for record in records],
    }
