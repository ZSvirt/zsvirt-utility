#!/bin/bash
set -euo pipefail

source_dir="${1:?missing source directory}"
zsv_edition="${2:-false}"

cd "$source_dir"

case "$zsv_edition" in
  true)
    if [ ! -f zsvirt-ee/pom.xml ]; then
      echo "ERROR: zsv.edition=true requires zsvirt-ee/pom.xml" >&2
      exit 1
    fi

    ./runMavenProfile ee
    ;;
  false)
    ./runMavenProfile premium
    ;;
  *)
    echo "ERROR: zsv.edition must be true or false, got: $zsv_edition" >&2
    exit 1
    ;;
esac

./runMavenProfile deploydb
