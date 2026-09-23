#!/usr/bin/env python
import argparse
import datetime
import hashlib
import json
import os
import re


COMPONENT_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def sha256sum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Generate zns-proxy package manifest")
    parser.add_argument("--package", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--path", required=True)
    args = parser.parse_args()

    if not os.path.isfile(args.package):
        raise SystemExit("zns-proxy package not found: %s" % args.package)
    if not COMPONENT_VERSION_PATTERN.match(args.version):
        raise SystemExit(
            "zns-proxy version must be a canonical four-part version: %s"
            % args.version
        )

    manifest = {
        "component": "zns-proxy",
        "packageName": os.path.basename(args.package),
        "version": args.version,
        "arch": [item.strip() for item in args.arch.split(",") if item.strip()],
        "sha256": sha256sum(args.package),
        "path": args.path,
        "buildTime": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    if os.path.isfile(args.output):
        with open(args.output) as stream:
            existing = json.load(stream)
        if existing.get("version") == manifest["version"]:
            if existing.get("sha256") != manifest["sha256"]:
                raise SystemExit(
                    "zns-proxy version %s already exists with a different sha256"
                    % manifest["version"]
                )

    with open(args.output, "w") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
