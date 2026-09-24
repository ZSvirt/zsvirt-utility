# -*- coding: utf-8 -*-
from __future__ import absolute_import

import datetime
import hashlib
import importlib
import json
import logging
import os
import stat
import sys
import threading
import time
import traceback
try:
    import ConfigParser as configparser
except ImportError:
    import configparser

from kvmagent.external_plugin_context import ExternalPluginContext
from kvmagent.external_plugin_manifest import ManifestError, load_manifest
from kvmagent.external_plugin_runtime import (
    CompatibilityError,
    RuntimeQueryError,
    RuntimeVersionCollector,
    collect_with_deadline,
    collect_with_startup_retry,
    validate_compatibility,
)
from kvmagent.external_plugin_status import (
    STATUS_PATH,
    plugin_status,
    status_envelope,
)
from kvmagent.external_plugin_restart_fence import (
    HOST_OPERATIONS_STATUS_PATH,
    RESTART_FENCE_PATH,
    restart_fence_handler,
    status_handler as host_operations_status_handler,
)
from zstacklib.utils.restart_fence import monotonic_time


logger = logging.getLogger(__name__)
DEFAULT_REGISTRY_ROOT = "/etc/zstack/kvmagent/plugins.d"
DEFAULT_MANAGED_ROOT = "/var/lib/zstack/zlr"
DEFAULT_STATUS_RECONCILE_DEADLINE = 1
DEFAULT_STATUS_RECONCILE_INTERVAL = 5
SUPPORTED_PLUGIN_APIS = frozenset((1,))
try:
    _string_types = (basestring,)
except NameError:
    _string_types = (str,)


def _utc_now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_diagnostic(error):
    return str(error).replace("\r", " ").replace("\n", " ")[:4096]


def _canonical_route(path):
    if path == "/":
        return path
    return path.rstrip("/") or "/"


class ExternalPluginRecord(object):
    def __init__(self, plugin_id, registry_path=None):
        self.plugin_id = plugin_id
        self.registry_path = registry_path
        self.enabled = True
        self.release_root = None
        self.resolved_release_root = None
        self.manifest_path = None
        self.manifest = None
        self.loaded_manifest_digest = None
        self.disk_fingerprint = None
        self.disk_drifted = False
        self.state = "DISCOVERED"
        self.failure = None
        self.dependency = {
            "state": "NOT_REQUIRED",
            "waitDeadline": None,
            "retryCount": 0,
            "lastResult": None,
        }
        self.loaded_runtime_versions = dict(
            (name, None) for name in
            ("python", "kvmAgent", "zstacklib", "qemu", "libvirt",
             "os", "architectures"))
        self.next_start_versions = dict(self.loaded_runtime_versions)
        self.compatibility_state = "UNKNOWN"
        self.last_compatibility_check_at = None
        self.restart_required = False
        self.stale = True
        self.transitioning = False
        self.registered_routes = []
        self.capabilities = None
        self.instance = None
        self.context = None
        self.lifecycle_lock = threading.RLock()

    def add_route(self, path):
        if path not in self.registered_routes:
            self.registered_routes.append(path)

    def fail(self, stage, code, error=None, **details):
        self.state = "FAILED"
        self.failure = {"stage": stage, "code": code}
        self.failure.update(dict((key, value) for key, value in details.items()
                                 if value is not None))
        if error is not None:
            self.failure["diagnostic"] = _safe_diagnostic(error)

    def release_status(self):
        if self.manifest is None or self.resolved_release_root is None:
            return None
        result = self.manifest.metadata()
        return {
            "resolvedPath": self.resolved_release_root,
            "version": result["version"],
            "sha256": result["sha256"],
            "manifestDigest": self.loaded_manifest_digest,
            "pluginApi": result["pluginApi"],
        }

    def status(self):
        return plugin_status(self)


class ExternalPluginRegistry(object):
    def __init__(self, http_server, registry_root=DEFAULT_REGISTRY_ROOT,
                 managed_root=DEFAULT_MANAGED_ROOT, expected_uid=0,
                 runtime_collector=None, dependency_ready_deadline=30,
                 protected_namespaces=None, sleep=None, monotonic=None,
                 stop_timeout_seconds=1, utcnow=None,
                 status_reconcile_deadline=DEFAULT_STATUS_RECONCILE_DEADLINE):
        self.http_server = http_server
        self.registry_root = os.path.abspath(registry_root)
        self.managed_root = os.path.realpath(os.path.abspath(managed_root))
        self.expected_uid = expected_uid
        self.runtime_collector = runtime_collector or RuntimeVersionCollector()
        self.dependency_ready_deadline = dependency_ready_deadline
        self.protected_namespaces = set(protected_namespaces or (
            "kvmagent", "zstacklib", "libvirt", "yaml", "requests"))
        self.sleep = sleep
        self.monotonic = monotonic or monotonic_time
        self.utcnow = utcnow or datetime.datetime.utcnow
        self.stop_timeout_seconds = max(0, float(stop_timeout_seconds))
        self.status_reconcile_deadline = max(
            0, float(status_reconcile_deadline))
        self.status_reconcile_interval = DEFAULT_STATUS_RECONCILE_INTERVAL
        self._last_status_reconcile_at = None
        self._status_reconcile_lock = threading.Lock()
        self.initialization_state = "PENDING"
        self._discovery_completed = False
        self.records = []
        self._by_id = {}
        self._route_owners = {STATUS_PATH: "kvmagent"}
        self._route_lock = threading.RLock()
        self._stop_requested = threading.Event()

    def _check_secure_regular_file(self, path, label):
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or os.path.islink(path):
            raise ValueError("%s must be a regular non-symlink file" % label)
        if self.expected_uid is not None and hasattr(metadata, "st_uid"):
            if metadata.st_uid != self.expected_uid:
                raise ValueError("%s must be owned by uid %s" %
                                 (label, self.expected_uid))
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError("%s must not be group/other writable" % label)

    def _check_secure_directory(self, path, label):
        metadata = os.lstat(path)
        if not stat.S_ISDIR(metadata.st_mode) or os.path.islink(path):
            raise ValueError("%s must be a non-symlink directory" % label)
        if self.expected_uid is not None and hasattr(metadata, "st_uid"):
            if metadata.st_uid != self.expected_uid:
                raise ValueError("%s must be owned by uid %s" %
                                 (label, self.expected_uid))
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError("%s must not be group/other writable" % label)

    def _check_release_tree(self, release_root):
        self._check_secure_directory(release_root, "release root")
        for root, directories, files in os.walk(release_root):
            for name in directories:
                self._check_secure_directory(
                    os.path.join(root, name), "release directory")
            for name in files:
                self._check_secure_regular_file(
                    os.path.join(root, name), "release file")

    @staticmethod
    def _metadata_timestamp_ns(metadata, name):
        nanoseconds = getattr(metadata, name + "_ns", None)
        if nanoseconds is not None:
            return int(nanoseconds)
        return int(getattr(metadata, name) * 1000000000)

    def _update_metadata_fingerprint(self, digest, label, path):
        try:
            metadata = os.lstat(path)
            values = (
                label,
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                getattr(metadata, "st_dev", -1),
                getattr(metadata, "st_ino", -1),
                getattr(metadata, "st_uid", -1),
                getattr(metadata, "st_gid", -1),
                metadata.st_size,
                self._metadata_timestamp_ns(metadata, "st_mtime"),
                self._metadata_timestamp_ns(metadata, "st_ctime"),
            )
        except OSError as error:
            values = (label, "ERROR", getattr(error, "errno", None))
        digest.update("\0".join(str(value) for value in values).encode("utf-8"))
        digest.update(b"\0")

    def _path_metadata_fingerprint(self, label, path):
        digest = hashlib.sha256()
        self._update_metadata_fingerprint(digest, label, path)
        return digest.hexdigest()

    def _disk_metadata_fingerprint(self, registry_path, release_root,
                                   resolved_release=None):
        digest = hashlib.sha256()
        self._update_metadata_fingerprint(digest, "registry", registry_path)
        self._update_metadata_fingerprint(digest, "release-pointer", release_root)
        if resolved_release is None:
            resolved_release = os.path.realpath(release_root)
        digest.update(resolved_release.encode("utf-8") + b"\0")
        self._update_metadata_fingerprint(digest, "release/.", resolved_release)

        def record_walk_error(error):
            label = "release-error/%s" % getattr(error, "filename", "")
            values = (label, "ERROR", getattr(error, "errno", None))
            digest.update(
                "\0".join(str(value) for value in values).encode("utf-8"))
            digest.update(b"\0")

        for root, directories, files in os.walk(
                resolved_release, topdown=True, onerror=record_walk_error):
            directories.sort()
            files.sort()
            for name in directories + files:
                path = os.path.join(root, name)
                relative = os.path.relpath(path, resolved_release).replace(
                    os.sep, "/")
                self._update_metadata_fingerprint(
                    digest, "release/%s" % relative, path)
        return digest.hexdigest()

    def _resolve_managed_path(self, path, label):
        if not os.path.isabs(path):
            raise ValueError("%s must be absolute" % label)
        resolved = os.path.realpath(path)
        if resolved != self.managed_root and not resolved.startswith(
                self.managed_root + os.sep):
            raise ValueError("%s escapes managed root" % label)
        return resolved

    def _parse_registry(self, path):
        plugin_id = os.path.splitext(os.path.basename(path))[0]
        record = ExternalPluginRecord(plugin_id, path)
        try:
            registry_fingerprint = self._path_metadata_fingerprint(
                "registry", path)
            self._check_secure_regular_file(path, "plugin registry")
            parser_class = getattr(configparser, "SafeConfigParser",
                                   configparser.ConfigParser)
            parser = parser_class()
            with open(path, "r") as stream:
                if hasattr(parser, "read_file"):
                    parser.read_file(stream)
                else:
                    parser.readfp(stream)
            section = "external-plugin"
            if not parser.has_section(section):
                raise ValueError("registry lacks [external-plugin]")
            allowed = set(("id", "release_root", "entry_module", "entry_class",
                           "manifest", "plugin_api", "enabled"))
            unknown = set(name for name, unused in parser.items(section)) - allowed
            if unknown:
                raise ValueError("registry has unknown options: %s" %
                                 ",".join(sorted(unknown)))
            plugin_id = parser.get(section, "id").strip()
            record.plugin_id = plugin_id
            record.enabled = parser.getboolean(section, "enabled")
            if not record.enabled:
                if registry_fingerprint != self._path_metadata_fingerprint(
                        "registry", path):
                    raise ManifestError(
                        "plugin registry metadata changed during parsing")
                record.state = "DISABLED"
                return record
            release_root = parser.get(section, "release_root").strip()
            manifest_path = parser.get(section, "manifest").strip()
            resolved_release = self._resolve_managed_path(release_root, "release_root")
            resolved_manifest = self._resolve_managed_path(manifest_path, "manifest")
            fingerprint_before = self._disk_metadata_fingerprint(
                path, release_root, resolved_release)
            if registry_fingerprint != self._path_metadata_fingerprint(
                    "registry", path):
                raise ManifestError(
                    "plugin registry metadata changed during parsing")
            if (resolved_release != os.path.realpath(release_root) or
                    resolved_manifest != os.path.realpath(manifest_path)):
                raise ManifestError(
                    "plugin release pointer changed during parsing")
            if not os.path.isdir(resolved_release):
                raise ValueError("release_root is not a directory")
            self._check_release_tree(resolved_release)
            expected_manifest = os.path.realpath(
                os.path.join(resolved_release, "manifest.yaml"))
            if resolved_manifest != expected_manifest:
                raise ValueError("registry manifest must be release_root/manifest.yaml")
            self._check_secure_regular_file(resolved_manifest, "plugin manifest")
            manifest = load_manifest(resolved_manifest)
            if manifest.plugin_id != plugin_id:
                raise ManifestError("registry and manifest plugin IDs differ")
            if parser.get(section, "entry_module").strip() != manifest.entry_module:
                raise ManifestError("registry and manifest entry modules differ")
            if parser.get(section, "entry_class").strip() != manifest.entry_class:
                raise ManifestError("registry and manifest entry classes differ")
            if parser.getint(section, "plugin_api") != manifest.plugin_api:
                raise ManifestError("registry and manifest plugin APIs differ")
            if manifest.plugin_api not in SUPPORTED_PLUGIN_APIS:
                raise ManifestError(
                    "unsupported external plugin API: %s" % manifest.plugin_api,
                    code="PLUGIN_API_UNSUPPORTED")
            manifest.verify_content(resolved_release)
            fingerprint_after = self._disk_metadata_fingerprint(
                path, release_root, resolved_release)
            if fingerprint_before != fingerprint_after:
                raise ManifestError(
                    "release metadata changed during content verification")
            record.release_root = release_root
            record.resolved_release_root = resolved_release
            record.manifest_path = resolved_manifest
            record.manifest = manifest
            record.loaded_manifest_digest = manifest.manifest_digest
            record.disk_fingerprint = fingerprint_after
        except ManifestError as error:
            record.fail("PRE_IMPORT_VALIDATION", error.code, error)
        except Exception as error:
            record.fail("PRE_IMPORT_VALIDATION", "PLUGIN_REGISTRY_INVALID", error)
        return record

    def discover(self):
        self._discovery_completed = False
        self.initialization_state = "DISCOVERING"
        try:
            records = self._discover_records()
        except Exception:
            self.initialization_state = "FAILED"
            raise
        self._discovery_completed = True
        self.initialization_state = "LOADING"
        return records

    def _discover_records(self):
        self.records = []
        self._by_id = {}
        if not os.path.isdir(self.registry_root):
            return self.records
        try:
            registry_entries = sorted(os.listdir(self.registry_root))
        except Exception as error:
            logger.error("external plugin registry scan failed: %s",
                         _safe_diagnostic(error))
            return self.records
        for name in registry_entries:
            if not name.endswith(".ini"):
                continue
            record = self._parse_registry(os.path.join(self.registry_root, name))
            self.records.append(record)
            if record.plugin_id in self._by_id:
                record.fail("PRE_IMPORT_VALIDATION", "PLUGIN_REGISTRY_INVALID",
                            "duplicate plugin ID")
                self._by_id[record.plugin_id].fail(
                    "PRE_IMPORT_VALIDATION", "PLUGIN_REGISTRY_INVALID",
                    "duplicate plugin ID")
            else:
                self._by_id[record.plugin_id] = record
        self._validate_namespaces()
        return self.records

    def _validate_namespaces(self):
        namespaces = {}
        loaded_modules = sys.modules.copy()
        loaded_top_levels = set(
            name.split(".", 1)[0] for name in loaded_modules)
        for record in self.records:
            if not record.enabled or record.manifest is None:
                continue
            namespace = record.manifest.namespace
            namespaces.setdefault(namespace, []).append(record)
        for namespace, records in namespaces.items():
            conflict = (len(records) > 1 or namespace in self.protected_namespaces or
                        namespace in loaded_top_levels)
            if conflict:
                for record in records:
                    record.fail("PRE_IMPORT_VALIDATION",
                                "PLUGIN_NAMESPACE_CONFLICT",
                                "top-level namespace %s conflicts" % namespace)

    def enabled_plugin_ids(self):
        return set(record.plugin_id for record in self.records if record.enabled)

    def claim_route(self, plugin_id, path):
        if not isinstance(path, _string_types) or not path.startswith("/"):
            raise ValueError("external plugin route must be an absolute URI")
        path = _canonical_route(path)
        aliases = (path,) if path == "/" else (path, path + "/")
        with self._route_lock:
            owner = self._route_owners.get(path)
            if owner is None and any(
                    alias in getattr(self.http_server, attribute, {})
                    for alias in aliases
                    for attribute in (
                        "sync_uri_handlers", "async_uri_handlers",
                        "raw_uri_handlers", "sync", "async_", "raw")):
                owner = "kvmagent"
            if owner is not None and owner != plugin_id:
                raise ValueError("external plugin route %s is owned by %s" %
                                 (path, owner))
            self._route_owners[path] = plugin_id
        return path

    def release_route(self, plugin_id, path):
        path = _canonical_route(path)
        with self._route_lock:
            if self._route_owners.get(path) == plugin_id:
                self._route_owners.pop(path, None)

    def _remove_record_routes(self, record):
        for path in list(record.registered_routes):
            with self._route_lock:
                if self._route_owners.get(path) != record.plugin_id:
                    continue
                unregister = getattr(self.http_server, "unregister_uri", None)
                if unregister is not None:
                    unregister(path)
                else:
                    for attribute in (
                            "sync_uri_handlers", "async_uri_handlers",
                            "raw_uri_handlers", "sync", "async_", "raw"):
                        getattr(self.http_server, attribute, {}).pop(path, None)
                self._route_owners.pop(path, None)
        record.registered_routes = []

    def _stop_instances_bounded(self, instances):
        workers = []
        for instance in instances:
            outcome = {"error": None}

            def stop_instance(current=instance, current_outcome=outcome):
                try:
                    current.stop()
                except Exception as error:
                    current_outcome["error"] = error

            worker = threading.Thread(target=stop_instance)
            worker.daemon = True
            worker.start()
            workers.append((worker, outcome))

        monotonic = self.monotonic or monotonic_time
        deadline = monotonic() + self.stop_timeout_seconds
        for worker, unused_outcome in workers:
            remaining = deadline - monotonic()
            if remaining > 0:
                worker.join(remaining)

        failures = []
        for worker, outcome in workers:
            if worker.is_alive():
                failure = {
                    "code": "PLUGIN_STOP_TIMEOUT",
                    "diagnostic": "external plugin stop exceeded %.3f seconds" %
                                  self.stop_timeout_seconds,
                }
                logger.warning(failure["diagnostic"])
            elif outcome["error"] is not None:
                failure = {
                    "code": "PLUGIN_STOP_FAILED",
                    "diagnostic": _safe_diagnostic(outcome["error"]),
                }
                logger.warning("external plugin stop failed: %s" %
                               failure["diagnostic"])
            else:
                failure = None
            failures.append(failure)
        return failures

    @staticmethod
    def _cleanup_failure_details(failure):
        if failure is None:
            return {}
        return {
            "cleanupCode": failure["code"],
            "cleanupDiagnostic": failure["diagnostic"],
        }

    def _take_record_lifecycle(self, record, context=None):
        with record.lifecycle_lock:
            try:
                self._remove_record_routes(record)
            except Exception:
                logger.warn("external plugin route cleanup failed: %s" %
                            traceback.format_exc())
            instance = record.instance
            record.instance = None
            current_context = record.context or context
            record.context = None
            if record.state == "STARTED":
                record.state = "STOPPED"
            if current_context is not None:
                try:
                    current_context.rollback()
                except Exception:
                    logger.warn("external plugin context cleanup failed: %s" %
                                traceback.format_exc())
            return instance

    def _rollback_record(self, record, context=None):
        instance = self._take_record_lifecycle(record, context)
        failures = (self._stop_instances_bounded([instance])
                    if instance is not None else [None])
        return failures[0]

    def _on_wait(self, record, retry_count, deadline, detail):
        wait_deadline = record.dependency.get("waitDeadline")
        if wait_deadline is None:
            monotonic = self.monotonic or monotonic_time
            remaining = max(0, deadline - monotonic())
            wait_deadline = (self.utcnow() + datetime.timedelta(
                seconds=remaining)).replace(
                    microsecond=0).isoformat() + "Z"
        record.dependency.update({
            "state": "WAITING_DEPENDENCY",
            "waitDeadline": wait_deadline,
            "retryCount": retry_count,
            "lastResult": detail[:1024],
        })
        record.failure = {
            "stage": "COMPATIBILITY_CHECK",
            "code": "PLUGIN_DEPENDENCY_NOT_READY",
            "reason": "DEPENDENCY_NOT_READY",
        }

    def _import_record(self, record, config):
        namespace = record.manifest.namespace
        previous_modules = set(sys.modules)
        previous_bytecode_policy = sys.dont_write_bytecode
        sys.path.insert(0, record.resolved_release_root)
        # A verified release is immutable.  Suppress __pycache__ writes while
        # importing it so root-owned Agent startup cannot mutate the content
        # tree and manufacture a false content-drift result later.
        sys.dont_write_bytecode = True
        try:
            module = importlib.import_module(record.manifest.entry_module)
            entry_class = getattr(module, record.manifest.entry_class)
            instance = entry_class()
        except Exception:
            for name in list(sys.modules):
                if name not in previous_modules and (name == namespace or
                                                     name.startswith(namespace + ".")):
                    sys.modules.pop(name, None)
            raise
        finally:
            sys.dont_write_bytecode = previous_bytecode_policy
            try:
                sys.path.remove(record.resolved_release_root)
            except ValueError:
                pass
        context = ExternalPluginContext(self, record, self.http_server)
        with record.lifecycle_lock:
            cancelled = self.is_stop_requested()
            if not cancelled:
                record.instance = instance
                record.state = "LOADED"
                record.context = context
        if cancelled:
            cleanup = self._stop_instances_bounded([instance])[0]
            context.rollback()
            record.fail("START", "PLUGIN_START_CANCELLED",
                        **self._cleanup_failure_details(cleanup))
            return
        plugin_config = dict(config or {})
        plugin_config["externalPluginContext"] = context
        if self.is_stop_requested():
            cleanup = self._rollback_record(record, context)
            record.fail("START", "PLUGIN_START_CANCELLED",
                        **self._cleanup_failure_details(cleanup))
            return
        try:
            instance.configure(plugin_config)
        except Exception as error:
            cleanup = self._rollback_record(record, context)
            record.fail("CONFIGURE", "PLUGIN_CONFIGURE_FAILED", error,
                        **self._cleanup_failure_details(cleanup))
            return
        if self.is_stop_requested():
            cleanup = self._rollback_record(record, context)
            record.fail("START", "PLUGIN_START_CANCELLED",
                        **self._cleanup_failure_details(cleanup))
            return
        try:
            instance.start()
            if self.is_stop_requested():
                cleanup = self._rollback_record(record, context)
                record.fail("START", "PLUGIN_START_CANCELLED",
                            **self._cleanup_failure_details(cleanup))
                return
            context.activate()
            if self.is_stop_requested():
                cleanup = self._rollback_record(record, context)
                record.fail("START", "PLUGIN_START_CANCELLED",
                            **self._cleanup_failure_details(cleanup))
        except Exception as error:
            cleanup = self._rollback_record(record, context)
            record.fail(
                "START",
                ("PLUGIN_START_CANCELLED" if self.is_stop_requested()
                 else "PLUGIN_START_FAILED"),
                error, **self._cleanup_failure_details(cleanup))

    def _verify_record_snapshot(self, record):
        if (record.disk_fingerprint is None or not record.registry_path or
                not record.release_root):
            return
        current = self._disk_metadata_fingerprint(
            record.registry_path, record.release_root)
        if current != record.disk_fingerprint:
            raise ManifestError(
                "plugin registry or release metadata changed before import")

    def load_and_start(self, config=None):
        try:
            if not self._discovery_completed:
                if self.records:
                    self._discovery_completed = True
                else:
                    self.discover()
            self.initialization_state = "LOADING"
            records = self._load_discovered(config)
        except Exception:
            self.initialization_state = "FAILED"
            raise
        self.initialization_state = "READY"
        return records

    def _load_discovered(self, config=None):
        for record in self.records:
            if self.is_stop_requested():
                break
            if not record.enabled or record.failure is not None:
                continue
            with record.lifecycle_lock:
                record.transitioning = True
            try:
                versions, retries = collect_with_startup_retry(
                    self.runtime_collector,
                    deadline_seconds=self.dependency_ready_deadline,
                    sleep=self.sleep, monotonic=self.monotonic,
                    on_wait=lambda count, deadline, detail, current=record:
                    self._on_wait(current, count, deadline, detail))
                validate_compatibility(record.manifest.compatibility, versions)
                record.loaded_runtime_versions = dict(versions)
                record.next_start_versions = dict(versions)
                record.dependency.update({
                    "state": "READY", "waitDeadline": None,
                    "retryCount": retries, "lastResult": "runtime versions available"})
                record.compatibility_state = "COMPATIBLE"
                record.last_compatibility_check_at = _utc_now()
                record.restart_required = False
                record.stale = False
                record.failure = None
                if self.is_stop_requested():
                    break
                self._verify_record_snapshot(record)
                self._import_record(record, config)
            except ManifestError as error:
                record.fail("PRE_IMPORT_VALIDATION", error.code, error)
            except CompatibilityError as error:
                record.dependency["state"] = (
                    "UNAVAILABLE" if error.code == "PLUGIN_RUNTIME_VERSION_UNAVAILABLE"
                    else "READY")
                record.fail("COMPATIBILITY_CHECK", error.code, error,
                            dependency=error.dependency, expected=error.expected,
                            actual=error.actual, reason=error.reason)
                record.compatibility_state = (
                    "INCOMPATIBLE_RUNTIME"
                    if error.code == "PLUGIN_RUNTIME_INCOMPATIBLE" else "UNKNOWN")
                record.last_compatibility_check_at = _utc_now()
                record.stale = error.code != "PLUGIN_RUNTIME_INCOMPATIBLE"
            except Exception as error:
                logger.warn("external plugin load failed: %s" % traceback.format_exc())
                record.fail("IMPORT", "PLUGIN_IMPORT_FAILED", error)
            finally:
                with record.lifecycle_lock:
                    record.transitioning = False
        return self.records

    def request_stop(self):
        self._stop_requested.set()

    def is_stop_requested(self):
        return self._stop_requested.is_set()

    def stop(self):
        self.request_stop()
        instances = []
        for record in reversed(self.records):
            instance = self._take_record_lifecycle(record)
            if instance is not None:
                instances.append(instance)
        self._stop_instances_bounded(instances)

    def can_mutate(self, plugin_id):
        record = self._by_id.get(plugin_id)
        return bool(record and record.state == "STARTED" and
                    record.compatibility_state == "COMPATIBLE")

    @staticmethod
    def _records_have_disk_drift(loaded_record, disk_record):
        drift = disk_record.failure is not None or not disk_record.enabled
        if disk_record.manifest is not None:
            drift = drift or any((
                disk_record.plugin_id != loaded_record.plugin_id,
                disk_record.resolved_release_root !=
                loaded_record.resolved_release_root,
                disk_record.manifest.content_sha256 !=
                loaded_record.manifest.content_sha256,
                disk_record.manifest.manifest_digest !=
                loaded_record.loaded_manifest_digest,
                disk_record.manifest.entry_module !=
                loaded_record.manifest.entry_module,
                disk_record.manifest.entry_class !=
                loaded_record.manifest.entry_class,
                disk_record.manifest.plugin_api !=
                loaded_record.manifest.plugin_api,
            ))
        return drift

    def _reconcile_disk_drift(self, record):
        fingerprint = self._disk_metadata_fingerprint(
            record.registry_path, record.release_root)
        if fingerprint == record.disk_fingerprint:
            return record.disk_drifted

        drift = (os.path.realpath(record.release_root) !=
                 record.resolved_release_root)
        try:
            disk_record = self._parse_registry(record.registry_path)
            drift = drift or self._records_have_disk_drift(
                record, disk_record)
        except Exception:
            drift = True
        record.disk_fingerprint = fingerprint
        record.disk_drifted = drift
        return drift

    def _reconcile_record(self, record, deadline):
        if (not record.enabled or record.manifest is None or
                record.transitioning):
            return
        try:
            live = collect_with_deadline(
                self.runtime_collector, "collect", deadline,
                monotonic=self.monotonic)
            loaded = dict(record.loaded_runtime_versions)
            has_loaded_snapshot = all(
                value is not None and value != ""
                for value in loaded.values())
            if has_loaded_snapshot:
                loaded["qemu"] = live["qemu"]
                loaded["libvirt"] = live["libvirt"]
                current = loaded
            else:
                current = dict(live)
            # Current-process compatibility is authoritative for mutation
            # fencing.  Validate it before collecting the more failure-prone
            # next-start projection so a disk/package query failure cannot
            # hide a live QEMU/libvirt incompatibility as UNKNOWN.
            validate_compatibility(record.manifest.compatibility, current)
            if has_loaded_snapshot:
                record.loaded_runtime_versions = loaded
            next_start = collect_with_deadline(
                self.runtime_collector, "collect_next_start", deadline,
                monotonic=self.monotonic)
            record.next_start_versions = dict(next_start)
            drift = False
            try:
                validate_compatibility(record.manifest.compatibility, next_start)
            except CompatibilityError:
                drift = True
            try:
                drift = drift or self._reconcile_disk_drift(record)
            except Exception:
                drift = True
            record.compatibility_state = (
                "DRIFTED_NEXT_START" if drift else "COMPATIBLE")
            # A failed plugin is never made usable in place.  If its
            # dependency later becomes available (or an operator repairs the
            # disk state), report the need for a controlled full Agent
            # restart instead of implying that status reconciliation hot
            # loaded the plugin.
            record.restart_required = drift or record.state == "FAILED"
            record.stale = False
            record.last_compatibility_check_at = _utc_now()
            record.dependency.update({
                "state": "READY",
                "waitDeadline": None,
                "lastResult": "runtime versions available",
            })
            if record.state == "STARTED":
                record.failure = None
        except CompatibilityError as error:
            record.compatibility_state = "INCOMPATIBLE_RUNTIME"
            record.restart_required = True
            record.stale = False
            record.fail("RUNTIME_RECONCILIATION", error.code, error,
                        dependency=error.dependency, expected=error.expected,
                        actual=error.actual, reason=error.reason)
            record.last_compatibility_check_at = _utc_now()
        except RuntimeQueryError as error:
            record.compatibility_state = "UNKNOWN"
            record.stale = True
            record.dependency.update({"state": "UNAVAILABLE",
                                      "lastResult": _safe_diagnostic(error)})
        except Exception as error:
            record.compatibility_state = "UNKNOWN"
            record.stale = True
            record.dependency.update({"state": "UNAVAILABLE",
                                      "lastResult": _safe_diagnostic(error)})

    def status_response(self, reconcile=True):
        if reconcile and self.initialization_state == "READY":
            monotonic = self.monotonic or monotonic_time
            deadline = monotonic() + self.status_reconcile_deadline
            for record in self.records:
                self._reconcile_record(record, deadline)
        return status_envelope(
            self.records, _utc_now(), self.initialization_state)

    def _status_response_for_poll(self):
        with self._status_reconcile_lock:
            monotonic = self.monotonic or monotonic_time
            now = monotonic()
            reconcile = (
                self._last_status_reconcile_at is None or
                now - self._last_status_reconcile_at >=
                self.status_reconcile_interval)
            response = self.status_response(reconcile=reconcile)
            if reconcile:
                self._last_status_reconcile_at = now
            return response

    def register_status_endpoint(self):
        def status_handler(unused_request):
            return json.dumps(self._status_response_for_poll(), sort_keys=True)
        self.http_server.register_sync_uri(STATUS_PATH, status_handler)
        self.http_server.register_sync_uri(
            HOST_OPERATIONS_STATUS_PATH, host_operations_status_handler)
        self.http_server.register_raw_uri(RESTART_FENCE_PATH, restart_fence_handler)
