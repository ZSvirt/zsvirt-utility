# -*- coding: utf-8 -*-
from __future__ import absolute_import

import os
import platform
import inspect
import json
import re
import subprocess
import sys
import tempfile
import threading
import time

from zstacklib.utils.restart_fence import monotonic_time


DEPENDENCIES = ("python", "kvmAgent", "zstacklib", "qemu", "libvirt")
MEMBERSHIP_DIMENSIONS = ("os", "architectures")
COMPATIBILITY_DIMENSIONS = DEPENDENCIES + MEMBERSHIP_DIMENSIONS
COMPARATOR = re.compile(r"^(==|!=|>=|<=|>|<)([^,\s]+)$")
VERSION_IN_TEXT = re.compile(r"([0-9]+(?:\.[0-9]+)+(?:[-+._][0-9A-Za-z.-]+)?)")
OS_RELEASE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
HOST_DIMENSION_VALUE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


class RuntimeQueryError(Exception):
    def __init__(self, message, reason="RUNTIME_QUERY_INVALID", transient=False):
        super(RuntimeQueryError, self).__init__(message)
        self.reason = reason
        self.transient = transient


class CompatibilityError(Exception):
    def __init__(self, code, dependency=None, expected=None, actual=None,
                 reason=None, message=None):
        message = message or code
        super(CompatibilityError, self).__init__(message)
        self.code = code
        self.dependency = dependency
        self.expected = expected
        self.actual = actual
        self.reason = reason

    def failure(self, stage="COMPATIBILITY_CHECK"):
        result = {"stage": stage, "code": self.code}
        for name in ("dependency", "expected", "actual", "reason"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        result["diagnostic"] = str(self)[:4096]
        return result


def _default_os_release_reader():
    with open("/etc/os-release", "rb") as stream:
        return stream.read()


def _decode_text(value, label):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, STRING_TYPES):
        raise RuntimeQueryError(
            "%s is unavailable or malformed" % label,
            reason="RUNTIME_QUERY_INVALID")
    return value


def _os_release_values(text):
    text = _decode_text(text, "host OS release")
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeQueryError(
                "host OS release contains a malformed row",
                reason="RUNTIME_QUERY_INVALID")
        key, value = line.split("=", 1)
        if not OS_RELEASE_KEY.match(key):
            raise RuntimeQueryError(
                "host OS release contains a malformed key",
                reason="RUNTIME_QUERY_INVALID")
        if key not in ("ID", "VERSION_ID"):
            continue
        if key in values:
            raise RuntimeQueryError(
                "host OS release contains duplicate %s" % key,
                reason="RUNTIME_QUERY_INVALID")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value.startswith(("\"", "'")) or value.endswith(("\"", "'")):
            raise RuntimeQueryError(
                "host OS release contains malformed quoting",
                reason="RUNTIME_QUERY_INVALID")
        values[key] = value
    return values


def _canonical_os_token(text):
    values = _os_release_values(text)
    os_id = values.get("ID", "").lower()
    version_id = values.get("VERSION_ID", "").lower()
    if (not os_id or not HOST_DIMENSION_VALUE.match(os_id) or
            not version_id):
        raise RuntimeQueryError(
            "host OS ID and VERSION_ID are required",
            reason="RUNTIME_QUERY_INVALID")
    match = re.search(
        r"(?:^|[^a-z0-9])v?([0-9]+(?:\.[0-9]+)*(?:[a-z]+)?)",
        version_id)
    if not match:
        raise RuntimeQueryError(
            "host OS VERSION_ID is malformed",
            reason="RUNTIME_QUERY_INVALID")
    version = match.group(1)
    if os_id in ("centos", "kylin"):
        version = version.split(".", 1)[0]
    token = os_id + version
    if not HOST_DIMENSION_VALUE.match(token):
        raise RuntimeQueryError(
            "canonical host OS token is malformed",
            reason="RUNTIME_QUERY_INVALID")
    return token


def _canonical_architecture(value):
    value = _decode_text(value, "host CPU architecture")
    if not value or value != value.strip():
        raise RuntimeQueryError(
            "host CPU architecture is unavailable or malformed",
            reason="RUNTIME_QUERY_INVALID")
    value = value.lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "arm64": "aarch64",
    }
    value = aliases.get(value, value)
    if not HOST_DIMENSION_VALUE.match(value):
        raise RuntimeQueryError(
            "host CPU architecture is unavailable or malformed",
            reason="RUNTIME_QUERY_INVALID")
    return value


def _distribution_version(name):
    try:
        import pkg_resources
        return pkg_resources.get_distribution(name).version
    except Exception as error:
        environment_name = name.upper().replace("-", "_") + "_VERSION"
        fallback = os.environ.get(environment_name)
        if fallback:
            return fallback
        raise RuntimeQueryError(
            "%s distribution version is unavailable: %s" % (name, error),
            reason="RUNTIME_QUERY_INVALID")


def _extract_version(text, label):
    match = VERSION_IN_TEXT.search(text or "")
    if not match:
        raise RuntimeQueryError("%s version response is invalid" % label,
                                reason="RUNTIME_QUERY_INVALID")
    return match.group(1)


def _timeout_error(message):
    return RuntimeQueryError(
        message, reason="DEPENDENCY_NOT_READY", transient=True)


def _call_with_timeout(function, timeout_seconds, message):
    if timeout_seconds is None:
        return function()
    timeout_seconds = max(0, float(timeout_seconds))
    result = []
    errors = []

    def invoke():
        try:
            result.append(function())
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.daemon = True
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise _timeout_error(message)
    if errors:
        raise errors[0]
    return result[0]


def _terminate_and_reap(process):
    if process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass
    grace_deadline = monotonic_time() + 0.1
    while process.poll() is None and monotonic_time() < grace_deadline:
        time.sleep(0.005)
    if process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait()
    except Exception:
        pass


def _popen_output(command, timeout_seconds=None):
    output_stream = tempfile.TemporaryFile()
    process = None
    try:
        process = subprocess.Popen(
            command, stdout=output_stream, stderr=subprocess.STDOUT)
        deadline = None
        if timeout_seconds is not None:
            deadline = monotonic_time() + max(0, float(timeout_seconds))
        while process.poll() is None:
            if deadline is not None and monotonic_time() >= deadline:
                _terminate_and_reap(process)
                output_stream.seek(0)
                detail = output_stream.read()
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", "replace")
                raise _timeout_error(
                    "command timed out: %s%s" %
                    (command[0], (": " + detail[:1024]) if detail else ""))
            time.sleep(0.005)
        return_code = process.wait()
        output_stream.seek(0)
        output = output_stream.read()
        if return_code:
            raise subprocess.CalledProcessError(
                return_code, command, output=output)
        return output
    finally:
        if process is not None and process.poll() is None:
            _terminate_and_reap(process)
        output_stream.close()


def _virsh_versions(command_runner=None, timeout_seconds=None):
    try:
        output = _command_output(
            ["virsh", "version"], command_runner, timeout_seconds)
    except RuntimeQueryError:
        raise
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "output", None) or str(error)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        lowered = detail.lower()
        if any(token in lowered for token in (
                "failed to connect", "connection refused", "no such file",
                "socket", "service unavailable")):
            raise RuntimeQueryError(detail, reason="DEPENDENCY_NOT_READY", transient=True)
        if any(token in lowered for token in ("permission denied", "authentication")):
            raise RuntimeQueryError(detail, reason="RUNTIME_QUERY_DENIED")
        raise RuntimeQueryError(detail, reason="RUNTIME_QUERY_INVALID")
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    daemon = re.search(r"Running against daemon:\s*([^\s]+)", output, re.I)
    hypervisor = re.search(r"Running hypervisor:\s*QEMU\s+([^\s]+)", output, re.I)
    if not daemon:
        daemon = re.search(r"Using library:\s*libvirt\s+([^\s]+)", output, re.I)
    if not daemon or not hypervisor:
        raise RuntimeQueryError("virsh version response lacks daemon or QEMU version",
                                reason="RUNTIME_QUERY_INVALID")
    return _extract_version(daemon.group(1), "libvirt"), _extract_version(
        hypervisor.group(1), "qemu")


def _command_output(command, command_runner=None, timeout_seconds=None):
    if command_runner is None:
        output = _popen_output(command, timeout_seconds)
    else:
        output = _call_with_timeout(
            lambda: command_runner(command, stderr=subprocess.STDOUT),
            timeout_seconds,
            "injected command runner timed out: %s" % command[0])
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return output


def _next_process_versions(command_runner=None, timeout_seconds=None):
    script = (
        "import json,platform,pkg_resources;"
        "print(json.dumps({'python':platform.python_version(),"
        "'kvmAgent':pkg_resources.get_distribution('kvmagent').version,"
        "'zstacklib':pkg_resources.get_distribution('zstacklib').version}))")
    try:
        output = _command_output(
            [sys.executable, "-c", script], command_runner, timeout_seconds)
        result = json.loads(output)
        if not isinstance(result, dict):
            raise RuntimeQueryError(
                "next-start Python package version response is not an object",
                reason="RUNTIME_QUERY_INVALID")
    except RuntimeQueryError:
        raise
    except Exception as error:
        raise RuntimeQueryError(
            "next-start Python package versions are unavailable: %s" % error,
            reason="RUNTIME_QUERY_INVALID")
    if not all(result.get(name) for name in ("python", "kvmAgent", "zstacklib")):
        raise RuntimeQueryError(
            "next-start Python package version response is incomplete",
            reason="RUNTIME_QUERY_INVALID")
    return result


def _find_executable(names):
    for name in names:
        if os.path.isabs(name) and os.path.isfile(name) and os.access(name, os.X_OK):
            return name
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _next_hypervisor_versions(command_runner=None, timeout_seconds=None):
    try:
        virsh = _command_output(
            ["virsh", "version"], command_runner, timeout_seconds)
    except RuntimeQueryError:
        raise
    except Exception as error:
        raise RuntimeQueryError(
            "next-start libvirt version is unavailable: %s" % error,
            reason="RUNTIME_QUERY_INVALID")
    library = re.search(r"Using library:\s*libvirt\s+([^\s]+)", virsh, re.I)
    if not library:
        library = re.search(
            r"Compiled against library:\s*libvirt\s+([^\s]+)", virsh, re.I)
    qemu_binary = _find_executable((
        "qemu-system-x86_64", "qemu-system-aarch64", "qemu-kvm",
        "/usr/libexec/qemu-kvm"))
    if not library or qemu_binary is None:
        raise RuntimeQueryError(
            "next-start QEMU/libvirt binary versions are unavailable",
            reason="RUNTIME_QUERY_INVALID")
    try:
        qemu_output = _command_output(
            [qemu_binary, "--version"], command_runner, timeout_seconds)
    except RuntimeQueryError:
        raise
    except Exception as error:
        raise RuntimeQueryError(
            "next-start QEMU version is unavailable: %s" % error,
            reason="RUNTIME_QUERY_INVALID")
    return (_extract_version(library.group(1), "next-start libvirt"),
            _extract_version(qemu_output, "next-start qemu"))


class RuntimeVersionCollector(object):
    def __init__(self, command_runner=None, distribution_version=None,
                 os_release_reader=None, machine=None):
        self.command_runner = command_runner
        self.distribution_version = distribution_version or _distribution_version
        self.os_release_reader = os_release_reader or _default_os_release_reader
        self.machine = platform.machine if machine is None else lambda: machine

    def _host_facts(self):
        try:
            os_release = self.os_release_reader()
            machine = self.machine()
        except RuntimeQueryError:
            raise
        except Exception as error:
            raise RuntimeQueryError(
                "host platform facts are unavailable: %s" % error,
                reason="RUNTIME_QUERY_INVALID")
        return {
            "os": _canonical_os_token(os_release),
            "architectures": _canonical_architecture(machine),
        }

    def collect(self, timeout_seconds=None):
        host_facts = self._host_facts()
        libvirt_version, qemu_version = _virsh_versions(
            self.command_runner, timeout_seconds)
        versions = {
            "python": platform.python_version(),
            "kvmAgent": self.distribution_version("kvmagent"),
            "zstacklib": self.distribution_version("zstacklib"),
            "qemu": qemu_version,
            "libvirt": libvirt_version,
        }
        versions.update(host_facts)
        return versions

    def collect_next_start(self, timeout_seconds=None):
        host_facts = self._host_facts()
        process_versions = _next_process_versions(
            self.command_runner, timeout_seconds)
        libvirt_version, qemu_version = _next_hypervisor_versions(
            self.command_runner, timeout_seconds)
        process_versions.update({
            "qemu": qemu_version,
            "libvirt": libvirt_version,
        })
        process_versions.update(host_facts)
        return process_versions


def _validate_range(expression):
    if not isinstance(expression, STRING_TYPES) or not expression:
        raise ValueError("version range is empty")
    constraints = []
    for item in expression.split(","):
        match = COMPARATOR.match(item.strip())
        if not match:
            raise ValueError("invalid version constraint: %s" % item)
        constraints.append((match.group(1), _version_key(match.group(2))))
    return constraints


def _version_key(value):
    public = str(value).strip().split("+", 1)[0]
    match = re.match(r"^([0-9]+(?:\.[0-9]+)*)(.*)$", public)
    if not match:
        raise ValueError("invalid version: %s" % value)

    release = [int(component) for component in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()

    suffix = match.group(2).lstrip("._-~")
    if not suffix or re.match(r"^0+(?:[._-]0+)*$", suffix):
        return tuple(release), 0, ()

    prerelease = re.match(
        r"^(dev|a|alpha|b|beta|pre|preview|rc)[._-]?([0-9]*)(.*)$",
        suffix, re.IGNORECASE)
    if prerelease:
        phases = {
            "dev": -4,
            "a": -3,
            "alpha": -3,
            "b": -2,
            "beta": -2,
            "pre": -1,
            "preview": -1,
            "rc": -1,
        }
        number = int(prerelease.group(2) or 0)
        remainder = prerelease.group(3)
        return (tuple(release), phases[prerelease.group(1).lower()],
                ((0, number),) + _suffix_key(remainder))

    return tuple(release), 1, _suffix_key(suffix)


def _suffix_key(value):
    parts = []
    for component in re.findall(r"[0-9]+|[A-Za-z]+", value):
        parts.append((0, int(component)) if component.isdigit()
                     else (1, component.lower()))
    return tuple(parts)


def version_in_range(version, expression):
    candidate = _version_key(version)
    for operator, expected in _validate_range(expression):
        matches = {
            "==": candidate == expected,
            "!=": candidate != expected,
            ">=": candidate >= expected,
            "<=": candidate <= expected,
            ">": candidate > expected,
            "<": candidate < expected,
        }[operator]
        if not matches:
            return False
    return True


def validate_compatibility(compatibility, versions):
    expected_dimensions = set(COMPATIBILITY_DIMENSIONS)
    actual_dimensions = set(compatibility)
    if actual_dimensions != expected_dimensions:
        missing = sorted(expected_dimensions - actual_dimensions)
        unknown = sorted(actual_dimensions - expected_dimensions)
        raise CompatibilityError(
            "PLUGIN_COMPATIBILITY_INVALID",
            reason="missing=%s unknown=%s" %
            (",".join(missing), ",".join(unknown)))
    for dependency in DEPENDENCIES:
        expression = compatibility.get(dependency)
        try:
            _validate_range(expression)
        except Exception as error:
            raise CompatibilityError(
                "PLUGIN_COMPATIBILITY_INVALID", dependency=dependency,
                expected=expression, actual=versions.get(dependency), message=str(error))
        actual = versions.get(dependency)
        if actual is None or actual == "":
            raise CompatibilityError(
                "PLUGIN_RUNTIME_VERSION_UNAVAILABLE", dependency=dependency,
                expected=expression, actual=None)
        if not version_in_range(actual, expression):
            raise CompatibilityError(
                "PLUGIN_RUNTIME_INCOMPATIBLE", dependency=dependency,
                expected=expression, actual=str(actual))
    for dimension in MEMBERSHIP_DIMENSIONS:
        allowed = compatibility.get(dimension)
        if (not isinstance(allowed, list) or not allowed or
                any(not isinstance(item, STRING_TYPES) or not item
                    for item in allowed) or len(allowed) != len(set(allowed))):
            raise CompatibilityError(
                "PLUGIN_COMPATIBILITY_INVALID", dependency=dimension,
                expected=allowed)
        actual = versions.get(dimension)
        if (not isinstance(actual, STRING_TYPES) or not actual or
                actual != actual.strip()):
            raise CompatibilityError(
                "PLUGIN_RUNTIME_VERSION_UNAVAILABLE", dependency=dimension,
                expected=allowed, actual=None)
        if actual not in allowed:
            raise CompatibilityError(
                "PLUGIN_RUNTIME_INCOMPATIBLE", dependency=dimension,
                expected=allowed, actual=actual)
    return True


def _method_accepts_timeout(method):
    try:
        try:
            specification = inspect.getfullargspec(method)
            keyword_arguments = specification.varkw
        except AttributeError:
            specification = inspect.getargspec(method)
            keyword_arguments = specification.keywords
        return ("timeout_seconds" in specification.args or
                keyword_arguments is not None)
    except Exception:
        return False


def _collector_method_with_timeout(collector, method_name, timeout_seconds):
    method = getattr(collector, method_name)

    def invoke():
        if _method_accepts_timeout(method):
            return method(timeout_seconds=timeout_seconds)
        return method()
    return _call_with_timeout(
        invoke, timeout_seconds, "runtime %s probe timed out" % method_name)


def collect_with_deadline(collector, method_name, deadline, monotonic=None):
    monotonic = monotonic or monotonic_time
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _timeout_error("runtime %s probe deadline expired" % method_name)
    return _collector_method_with_timeout(
        collector, method_name, remaining)


def _collect_with_timeout(collector, timeout_seconds):
    return _collector_method_with_timeout(
        collector, "collect", timeout_seconds)


def collect_with_startup_retry(collector, deadline_seconds=30, sleep=None,
                               monotonic=None, on_wait=None):
    sleep = sleep or time.sleep
    monotonic = monotonic or monotonic_time
    deadline = monotonic() + deadline_seconds
    delay = 0.25
    retry_count = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CompatibilityError(
                "PLUGIN_RUNTIME_VERSION_UNAVAILABLE",
                reason="DEPENDENCY_STARTUP_TIMEOUT",
                message="runtime dependency probe deadline expired")
        try:
            return _collect_with_timeout(collector, remaining), retry_count
        except RuntimeQueryError as error:
            if not error.transient:
                raise CompatibilityError(
                    "PLUGIN_RUNTIME_VERSION_UNAVAILABLE", reason=error.reason,
                    message=str(error))
            now = monotonic()
            if now >= deadline:
                raise CompatibilityError(
                    "PLUGIN_RUNTIME_VERSION_UNAVAILABLE",
                    reason="DEPENDENCY_STARTUP_TIMEOUT", message=str(error))
            retry_count += 1
            if on_wait:
                on_wait(retry_count, deadline, str(error))
            wait = min(delay, max(0, deadline - now))
            sleep(wait)
            delay = min(delay * 2, 5.0)
