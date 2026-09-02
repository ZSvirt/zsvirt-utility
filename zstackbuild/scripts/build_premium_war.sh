#!/bin/bash
set -euo pipefail

source_dir="${1:?missing source directory}"
enterprise_edition="${2:-false}"

cd "$source_dir"

case "$enterprise_edition" in
  true)
    if [ ! -f zsvirt-ee/pom.xml ]; then
      echo "ERROR: enterprise_edition=true requires zsvirt-ee/pom.xml" >&2
      exit 1
    fi

    ./runMavenProfile ee
    ;;
  false)
    ./runMavenProfile premium
    ;;
  *)
    echo "ERROR: enterprise_edition must be true or false, got: $enterprise_edition" >&2
    exit 1
    ;;
esac

./runMavenProfile deploydb
