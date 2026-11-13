#!/usr/bin/env python
import argparse
import hashlib
import json
import os
import re


COMPONENT_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


def sha256sum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fail(message):
    raise SystemExit("zns-proxy ISO package guard failed: %s" % message)


def main():
    parser = argparse.ArgumentParser(description="Validate zns-proxy ISO package content")
    parser.add_argument("--ansible-dir", required=True)
    args = parser.parse_args()

    znsproxy_dir = os.path.join(args.ansible_dir, "znsproxy")
    package_path = os.path.join(znsproxy_dir, "zns-proxy.bin")
    manifest_path = os.path.join(znsproxy_dir, "zns-proxy-manifest.json")

    if not os.path.isfile(package_path):
        fail("missing znsproxy/zns-proxy.bin")
    if not os.path.isfile(manifest_path):
        fail("missing znsproxy/zns-proxy-manifest.json")

    with open(manifest_path) as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        fail("manifest must be a JSON object")

    expected = {
        "component": "zns-proxy",
        "packageName": "zns-proxy.bin",
        "path": "zns-proxy.bin",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            fail("manifest %s must be %s, got %s" % (key, value, manifest.get(key)))

    version = manifest.get("version")
    if not isinstance(version, STRING_TYPES) or not version.strip():
        fail("manifest version must be a non-empty string")
    if not COMPONENT_VERSION_PATTERN.match(version):
        fail("manifest version must be a canonical four-part version")
    sha256 = manifest.get("sha256")
    if not isinstance(sha256, STRING_TYPES) or not sha256.strip():
        fail("manifest sha256 must be a non-empty string")
    if not SHA256_PATTERN.match(sha256):
        fail("manifest sha256 must be lowercase hexadecimal")
    arch = manifest.get("arch")
    if (
        not isinstance(arch, list)
        or not arch
        or any(not isinstance(item, STRING_TYPES) or not item.strip() for item in arch)
    ):
        fail("manifest arch must be a non-empty list of strings")
    build_time = manifest.get("buildTime")
    if not isinstance(build_time, STRING_TYPES) or not build_time.strip():
        fail("manifest buildTime must be a non-empty string")

    if manifest.get("sha256") != sha256sum(package_path):
        fail("manifest sha256 does not match znsproxy/zns-proxy.bin")

    if "znsagentansible" in os.listdir(args.ansible_dir):
        fail("znsagentansible must not be packaged in Cloud ISO")

    forbidden = []
    for root, _, files in os.walk(args.ansible_dir):
        for name in files:
            if name.startswith("zns-agent") and name.endswith(".bin"):
                forbidden.append(os.path.join(root, name))
    if forbidden:
        fail("zns-agent binary must not be packaged: %s" % ", ".join(forbidden))


if __name__ == "__main__":
    main()
