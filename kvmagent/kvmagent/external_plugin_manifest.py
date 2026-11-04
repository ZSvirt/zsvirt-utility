# -*- coding: utf-8 -*-
from __future__ import absolute_import

import hashlib
import json
import os
import re
import stat

import yaml


SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RUNTIME_DEPENDENCIES = ("python", "kvmAgent", "zstacklib", "qemu", "libvirt")
MEMBERSHIP_DIMENSIONS = ("os", "architectures")
COMPATIBILITY_DIMENSIONS = RUNTIME_DEPENDENCIES + MEMBERSHIP_DIMENSIONS
DEPLOYMENT_VERIFICATION_MARKERS = frozenset((
    ".artifact-sha256", ".content-sha256"))
CANONICAL_RELEASE_MODES = frozenset((0o444, 0o555, 0o644, 0o755))
VERSION_CONSTRAINT = re.compile(r"^(==|!=|>=|<=|>|<)[^,\s]+$")
DIMENSION_VALUE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


class ManifestError(Exception):
    def __init__(self, message, code="PLUGIN_MANIFEST_MISMATCH"):
        super(ManifestError, self).__init__(message)
        self.code = code


def _canonical_release_mode(mode, relative):
    if os.name != "nt" and mode not in CANONICAL_RELEASE_MODES:
        raise ManifestError(
            "release member mode is invalid: %s mode=%04o" %
            (relative, mode))
    return 0o755 if mode & 0o111 else 0o644


def _required_map(source, name):
    value = source.get(name)
    if not isinstance(value, dict):
        raise ManifestError("manifest.%s must be an object" % name)
    return value


def _required_string(source, name):
    value = source.get(name)
    if not isinstance(value, STRING_TYPES) or not value or value != value.strip():
        raise ManifestError("manifest field %s is required" % name)
    return value


def _canonical_content_sha256(release_root):
    entries = []
    for root, dirs, files in os.walk(release_root):
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = os.path.join(root, name)
            relative = os.path.relpath(path, release_root).replace(os.sep, "/")
            if (relative == "manifest.yaml" or
                    relative in DEPLOYMENT_VERIFICATION_MARKERS):
                continue
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode):
                raise ManifestError("release member is not a regular file: %s" % relative)
            entries.append((
                relative, path,
                _canonical_release_mode(stat.S_IMODE(metadata.st_mode),
                                        relative)))
    digest = hashlib.sha256()
    for relative, path, mode in sorted(entries):
        with open(path, "rb") as stream:
            data = stream.read()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(("%04o" % mode).encode("ascii") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()


def _canonical_manifest_digest(document):
    try:
        canonical = json.dumps(
            document, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False)
    except Exception as error:
        raise ManifestError(
            "manifest cannot be canonicalized: %s" % error)
    if not isinstance(canonical, bytes):
        canonical = canonical.encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _compatibility_error(message):
    raise ManifestError(message, code="PLUGIN_COMPATIBILITY_INVALID")


def _validate_version_range(name, value):
    if (not isinstance(value, STRING_TYPES) or not value or
            value != value.strip()):
        _compatibility_error(
            "manifest compatibility.%s must be a version range" % name)
    constraints = [item.strip() for item in value.split(",")]
    if (not constraints or any(not VERSION_CONSTRAINT.match(item)
                               for item in constraints) or
            len(constraints) != len(set(constraints))):
        _compatibility_error(
            "manifest compatibility.%s is malformed" % name)


def _validate_dimension_values(name, values):
    if not isinstance(values, list) or not values:
        _compatibility_error(
            "manifest compatibility.%s must be a non-empty list" % name)
    normalized = []
    for value in values:
        if (not isinstance(value, STRING_TYPES) or not value or
                value != value.strip() or value != value.lower() or
                not DIMENSION_VALUE.match(value)):
            _compatibility_error(
                "manifest compatibility.%s contains a malformed value" %
                name)
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        _compatibility_error(
            "manifest compatibility.%s contains duplicate values" % name)


def _validate_compatibility(compatibility):
    actual = set(compatibility)
    expected = set(COMPATIBILITY_DIMENSIONS)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        _compatibility_error(
            "manifest compatibility keys mismatch; missing=%s unknown=%s" %
            (",".join(missing), ",".join(unknown)))
    for dependency in RUNTIME_DEPENDENCIES:
        _validate_version_range(dependency, compatibility[dependency])
    for dimension in MEMBERSHIP_DIMENSIONS:
        _validate_dimension_values(dimension, compatibility[dimension])


class ExternalPluginManifest(object):
    def __init__(self, document, path):
        self.document = document
        self.path = path
        identity = _required_map(document, "identity")
        loading = _required_map(document, "loading")
        compatibility = _required_map(document, "compatibility")
        interfaces = _required_map(document, "interfaces")
        security = _required_map(document, "security")

        if str(document.get("schemaVersion")) != "1":
            raise ManifestError("unsupported manifest schemaVersion")
        self.plugin_id = _required_string(identity, "pluginId")
        self.version = _required_string(identity, "version")
        self.content_sha256 = _required_string(identity, "contentSha256")
        self.entry_module = _required_string(loading, "entryModule")
        self.entry_class = _required_string(loading, "entryClass")
        self.plugin_api = loading.get("pluginApi")
        self.compatibility = compatibility
        self.interfaces = interfaces
        self.capabilities = document.get("capabilities") or {}

        if not IDENTIFIER.match(self.plugin_id):
            raise ManifestError("manifest pluginId is invalid")
        if not SHA256.match(self.content_sha256):
            raise ManifestError("manifest contentSha256 is invalid")
        if not MODULE.match(self.entry_module):
            raise ManifestError("manifest entryModule is invalid")
        if not CLASS.match(self.entry_class):
            raise ManifestError("manifest entryClass is invalid")
        if not isinstance(self.plugin_api, int) or isinstance(self.plugin_api, bool) or self.plugin_api < 1:
            raise ManifestError("manifest pluginApi is invalid")
        if security.get("dynamicDependencies") is not False:
            raise ManifestError("dynamic plugin dependencies are forbidden")
        _validate_compatibility(compatibility)
        self.manifest_digest = _canonical_manifest_digest(document)

    @property
    def namespace(self):
        return self.entry_module.split(".", 1)[0]

    def verify_content(self, release_root):
        actual = _canonical_content_sha256(release_root)
        if actual != self.content_sha256:
            raise ManifestError(
                "release content SHA-256 mismatch: expected=%s actual=%s" %
                (self.content_sha256, actual))
        return actual

    def metadata(self):
        return {
            "pluginId": self.plugin_id,
            "version": self.version,
            "sha256": self.content_sha256,
            "manifestDigest": self.manifest_digest,
            "pluginApi": self.plugin_api,
        }


def load_manifest(path):
    try:
        if os.path.getsize(path) <= 0 or os.path.getsize(path) > 1024 * 1024:
            raise ManifestError("manifest size is invalid")
        with open(path, "rb") as stream:
            document = yaml.safe_load(stream)
    except ManifestError:
        raise
    except Exception as error:
        raise ManifestError("manifest cannot be parsed: %s" % error)
    if not isinstance(document, dict):
        raise ManifestError("manifest root must be an object")
    return ExternalPluginManifest(document, path)


def dump_manifest_for_log(manifest):
    return json.dumps(manifest.metadata(), sort_keys=True)
