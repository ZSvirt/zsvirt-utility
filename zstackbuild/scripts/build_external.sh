#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ZSTACKBUILD_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
BUILD_ROOT=${ZSTACK_BUILD_ROOT:-$(cd -- "${ZSTACKBUILD_DIR}/../.." && pwd)}

ZWATCH_SOURCE=${ZSTACK_ZWATCH_SOURCE:-${BUILD_ROOT}/zstack-zwatch}
STORE_SOURCE=${ZSTACK_STORE_SOURCE:-${BUILD_ROOT}/zstack-store}
SHAREDBLOCK_SOURCE=${ZSTACK_SHAREDBLOCK_SOURCE:-${BUILD_ROOT}/zstack-sharedblock}
ZSTACK_SOURCE=${ZSTACK_SOURCE:-${BUILD_ROOT}/zstack}

GOROOT=${ZSV_GOROOT:-/usr/lib/golang1.18}
ZWATCH_ARCH=${ZWATCH_ARCH:-amd64}
STORE_ARCH=${STORE_ARCH:-amd64 arm64}
SHAREDBLOCK_ARCH=${SHAREDBLOCK_ARCH:-amd64 arm64}
PYTHON_BIN=${PYTHON_BIN:-/bin/python}

TARGET_DIR=${ZSTACKBUILD_DIR}/target
EXTERNAL_DIR=${ZSTACKBUILD_DIR}/external
AGENT_VERSION_GENERATOR=${SCRIPT_DIR}/agnet_version_generator_py2.py

FROM_TARGET=0
VERSION_ARG=

while [ $# -gt 0 ]; do
    case "$1" in
        --from-target)
            FROM_TARGET=1
            shift
            ;;
        -*)
            echo "ERROR: unknown option: $1" >&2
            echo "usage: $0 [--from-target] [VERSION]" >&2
            exit 1
            ;;
        *)
            VERSION_ARG=$1
            shift
            ;;
    esac
done

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_dir() {
    [ -d "$1" ] || fail "directory does not exist: $1"
}

require_file() {
    [ -f "$1" ] || fail "file does not exist: $1"
}

read_version_value() {
    local key=$1
    awk -F= -v key="${key}" '$1 == key { gsub(/[[:space:]]/, "", $2); print $2; exit }' "${ZSTACK_SOURCE}/VERSION"
}

resolve_version() {
    if [ -n "${1:-}" ]; then
        printf '%s\n' "$1"
        return
    fi
    if [ -n "${EXTERNAL_VERSION:-}" ]; then
        printf '%s\n' "${EXTERNAL_VERSION}"
        return
    fi

    require_file "${ZSTACK_SOURCE}/VERSION"
    local major minor update version
    major=$(read_version_value MAJOR)
    minor=$(read_version_value MINOR)
    update=$(read_version_value UPDATE)
    if [ -z "${major}" ] || [ -z "${minor}" ] || [ -z "${update}" ]; then
        fail "invalid VERSION file: ${ZSTACK_SOURCE}/VERSION"
    fi
    version="${major}.${minor}.${update}"
    if [ -n "${BUILD_NUM:-}" ]; then
        version="${version}.${BUILD_NUM}"
    fi
    printf '%s\n' "${version}"
}

replace_output_dir() {
    local destination=$1
    local source=$2
    case "${destination}" in
        "${EXTERNAL_DIR}"|"${TARGET_DIR}/zwatch-vm-agent"|"${TARGET_DIR}/guesttools"|"${TARGET_DIR}/zstack-store"|"${TARGET_DIR}/zsblk-agent")
            ;;
        *)
            fail "refusing to replace unexpected directory: ${destination}"
            ;;
    esac
    rm -rf -- "${destination}"
    mkdir -p -- "${destination}"
    cp -a -- "${source}/." "${destination}/"
}

write_archive() {
    local staging_dir=$1
    replace_output_dir "${EXTERNAL_DIR}" "${staging_dir}"
    if [ "${FROM_TARGET}" -eq 0 ]; then
        replace_output_dir "${TARGET_DIR}/zwatch-vm-agent" "${staging_dir}/zwatch-vm-agent"
        replace_output_dir "${TARGET_DIR}/guesttools" "${staging_dir}/guesttools"
        replace_output_dir "${TARGET_DIR}/zstack-store" "${staging_dir}/zstack-store"
        replace_output_dir "${TARGET_DIR}/zsblk-agent" "${staging_dir}/zsblk-agent"
    fi

    rm -f -- "${ARCHIVE_PATH}" "${SHA256_PATH}"
    tar -cf "${ARCHIVE_PATH}" -C "${EXTERNAL_DIR}" .
    (
        cd -- "${TARGET_DIR}"
        sha256sum "${ARCHIVE_NAME}" > "${ARCHIVE_NAME}.sha256"
    )

    echo "External runtime artifacts copied to:"
    echo "  ${EXTERNAL_DIR}"
    echo "  ${TARGET_DIR}"
    echo "External archive created:"
    echo "  ${ARCHIVE_PATH}"
    echo "  ${SHA256_PATH}"
}

VERSION=$(resolve_version "${VERSION_ARG}")
ARCHIVE_NAME=external-${VERSION}.tar
ARCHIVE_PATH=${TARGET_DIR}/${ARCHIVE_NAME}
SHA256_PATH=${ARCHIVE_PATH}.sha256

mkdir -p -- "${TARGET_DIR}"
STAGING_DIR=$(mktemp -d "${ZSTACKBUILD_DIR}/.external-staging.XXXXXX")
trap 'rm -rf -- "${STAGING_DIR}"' EXIT

if [ "${FROM_TARGET}" -eq 1 ]; then
    # Package ant-built outputs under target/ (after all-in-one).
    # Avoids depending on a pre-ant build_external run that prepare would wipe.
    echo "Packaging external archive from existing ${TARGET_DIR} outputs"
    for d in zwatch-vm-agent guesttools zstack-store zsblk-agent; do
        require_dir "${TARGET_DIR}/${d}"
    done
    mkdir -p -- \
        "${STAGING_DIR}/zwatch-vm-agent" \
        "${STAGING_DIR}/guesttools" \
        "${STAGING_DIR}/zstack-store" \
        "${STAGING_DIR}/zsblk-agent"
    cp -a -- "${TARGET_DIR}/zwatch-vm-agent/." "${STAGING_DIR}/zwatch-vm-agent/"
    cp -a -- "${TARGET_DIR}/guesttools/." "${STAGING_DIR}/guesttools/"
    cp -a -- "${TARGET_DIR}/zstack-store/." "${STAGING_DIR}/zstack-store/"
    cp -a -- "${TARGET_DIR}/zsblk-agent/." "${STAGING_DIR}/zsblk-agent/"
    write_archive "${STAGING_DIR}"
    exit 0
fi

require_dir "${ZWATCH_SOURCE}"
require_dir "${STORE_SOURCE}"
require_dir "${SHAREDBLOCK_SOURCE}"
require_file "${AGENT_VERSION_GENERATOR}"
require_file "${GOROOT}/bin/go"

export GOROOT
export PATH="${GOROOT}/bin:${PATH}"
echo "Using Go toolchain: $("${GOROOT}/bin/go" version)"

echo "Building zstack-zwatch from ${ZWATCH_SOURCE}"
make -C "${ZWATCH_SOURCE}" clean
GOROOT="${GOROOT}" make -C "${ZWATCH_SOURCE}" all "ARCH=${ZWATCH_ARCH}"
require_dir "${ZWATCH_SOURCE}/target/zwatch_bin"
require_dir "${ZWATCH_SOURCE}/target/iso"
mkdir -p -- "${STAGING_DIR}/zwatch-vm-agent" "${STAGING_DIR}/guesttools"
cp -a -- "${ZWATCH_SOURCE}/target/zwatch_bin/." "${STAGING_DIR}/zwatch-vm-agent/"
find "${ZWATCH_SOURCE}/target/iso" -maxdepth 1 -type f -name '*.iso' -exec cp -a -t "${STAGING_DIR}/guesttools/" -- {} +
if [ -z "$(find "${STAGING_DIR}/guesttools" -maxdepth 1 -type f -name '*.iso' -print -quit)" ]; then
    fail "no GuestTools ISO found in ${ZWATCH_SOURCE}/target/iso"
fi
(
    cd -- "${STAGING_DIR}/zwatch-vm-agent"
    "${PYTHON_BIN}" "${AGENT_VERSION_GENERATOR}" -s "${ZWATCH_SOURCE}" -o agent_version
)

echo "Building zstack-store from ${STORE_SOURCE}"
make -C "${STORE_SOURCE}" clean
GOROOT="${GOROOT}" make -C "${STORE_SOURCE}" package "ARCH=${STORE_ARCH}"
require_dir "${STORE_SOURCE}/target/package"
mkdir -p -- "${STAGING_DIR}/zstack-store"
cp -a -- "${STORE_SOURCE}/target/package/." "${STAGING_DIR}/zstack-store/"

echo "Building zstack-sharedblock from ${SHAREDBLOCK_SOURCE}"
make -C "${SHAREDBLOCK_SOURCE}" clean
GOROOT="${GOROOT}" make -C "${SHAREDBLOCK_SOURCE}" package "ARCH=${SHAREDBLOCK_ARCH}"
require_dir "${SHAREDBLOCK_SOURCE}/target/package/zsblk-agent"
mkdir -p -- "${STAGING_DIR}/zsblk-agent"
cp -a -- "${SHAREDBLOCK_SOURCE}/target/package/zsblk-agent/." "${STAGING_DIR}/zsblk-agent/"

write_archive "${STAGING_DIR}"

