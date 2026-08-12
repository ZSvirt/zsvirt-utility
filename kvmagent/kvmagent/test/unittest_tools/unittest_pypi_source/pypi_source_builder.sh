#!/bin/bash

call_help() {
    echo "Usage: bash pypi_source_builder.sh <command> [options]"
    echo "Command:"
    echo -e "  build                              build pypi_source"
    echo -e "  help                               show help message"
    echo "Options:"
    echo -e "  --minio <url>                      minio storage source URL."
    echo -e "                                     defaults: http://minio.zstack.io:9001/download/devops_dependencies/utility_ut/pypi_source"
    echo -e "  --package-list <file>              source package list file path"
    echo -e "  --target-path <dir>                pypi local storage directory"
    echo -e "  --requirement-output-path <file>   pypi requirements output file path"
    echo -e "  -h|--help                          show help message"
}

CMD="$1"
if [[ -z $CMD ]]; then
    call_help
    exit 1
elif [[ x"$CMD" = x"help" ]]; then
    call_help
    exit 0
fi

# bash pypi_source_builder.sh build --minio <url> --package-list <file> --target-path <dir>
MINIO_URL="http://minio.zstack.io:9001/download/devops_dependencies/utility_ut/pypi_source"
PACKAGE_LIST_PATH=""
BUILD_TARGET_PATH=""
PYPI_REQUIREMENT_OUTPUT_PATH=""
PYPI_REQUIREMENT_OUTPUT="false"

OPTS=`getopt -o h --long minio:,package-list:,target-path:,requirement-output-path:,help -- "$@"`
eval set -- "$OPTS"
while true; do
    case "$1" in
        --minio) MINIO_URL="$2"; shift 2;;
        --package-list) PACKAGE_LIST_PATH="$2"; shift 2;;
        --target-path) BUILD_TARGET_PATH="$2"; shift 2;;
        --requirement-output-path) PYPI_REQUIREMENT_OUTPUT_PATH="$2"; PYPI_REQUIREMENT_OUTPUT="true"; shift 2;;
        -h|--help) call_help; exit 0;;
        *) shift; break;;
    esac
done


echo "PACKAGE_LIST_PATH=$PACKAGE_LIST_PATH"
echo "BUILD_TARGET_PATH=$BUILD_TARGET_PATH"
echo "PYPI_REQUIREMENT_OUTPUT_PATH=$PYPI_REQUIREMENT_OUTPUT_PATH"
[ ! -z $PYPI_REQUIREMENT_OUTPUT_PATH ] && mkdir -p $(dirname "$PYPI_REQUIREMENT_OUTPUT_PATH")


# find package_name from pip-package/pip-wheel file.
# ex:  filename="Cython-0.29.37.tar.gz"       output="package-name"   return "cython"
# ex:  filename="more-itertools-5.0.0.tar.gz" output="package-name"   return "more-itertool"
# ex:  filename="attrs-21.4.0.tar.gz"         output="package-name"   return "attrs"
# ex:  filename="setuptools_scm-5.0.2.tar.gz" output="package-name"   return "setuptools-scm"
# ex:  filename="repoze.lru-0.6.tar.gz"       output="package-name"   return "repoze-lru"
# ex:  filename="attrs-21.4.0.tar.gz"         output="version"        return "21.4.0"
extract_package_name_from_filename() {
    local filename="$1"
    local output="$2"  # default: package-name

    local clean="${filename%.*}"
    if [[ "$clean" == *.tar ]]; then
        clean="${clean%.tar}"
    fi

    IFS='-' read -ra parts <<< "$clean"
    local len=${#parts[@]}

    local split_at=$len
    for ((i = 1; i < len; i++)); do
        if [[ "${parts[i]}" =~ ^[0-9] ]]; then
            split_at=$i
            break
        fi
    done

    if [[ x"$output" = x"version" ]]; then  # output=version
        echo "${parts[$split_at]}"
    else  # output=package-name
        if [[ $split_at -ge len ]]; then
            echo "$clean" | tr '[:upper:]' '[:lower:]'
            return
        fi
        local name_parts=("${parts[@]:0:split_at}")
        local name=$(IFS=-; echo "${name_parts[*]}")
        name="${name//_/-}"  # setuptools_scm  =>  setuptools-scm
        name="${name//./-}"  # repoze.lru  =>  repoze-lru
        echo "$name" | tr '[:upper:]' '[:lower:]'
    fi
}


build_pipy_source_directory() {
    local minio_path=$MINIO_URL
    local requirement_file_path="$PACKAGE_LIST_PATH"  # requirements/requirements1.txt
    local local_pipy_dir_path="$BUILD_TARGET_PATH"  # /tmp/pipy_requirements1

    mkdir -p "$local_pipy_dir_path/simple"
    local index_html_text=""

    [ x"$PYPI_REQUIREMENT_OUTPUT" = x"true" ] && touch "$PYPI_REQUIREMENT_OUTPUT_PATH"
    local pypi_requirement_text=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        local package_name=$(echo "$line" | xargs) # ex: Cython-0.29.37.tar.gz
        [[ -z "$package_name" ]] && continue

        local package_lower_name=$(extract_package_name_from_filename "$package_name")  # ex: cython
        if [[ x"$PYPI_REQUIREMENT_OUTPUT" = x"true" ]]; then
            local package_version=$(extract_package_name_from_filename "$package_name" version)
            pypi_requirement_text=$pypi_requirement_text"$package_lower_name==$package_version"$'\n'
        fi

        [[ -f "$local_pipy_dir_path/$package_name" ]] && echo "exists pypi package: $package_name" && continue

        echo "handle pypi package: $package_name -> $package_lower_name"
        wget "$minio_path/$package_lower_name/$package_name" -O "$local_pipy_dir_path/$package_name"
        mkdir -p "$local_pipy_dir_path/simple/$package_lower_name"
        ln -sf "../../$package_name" "$local_pipy_dir_path/simple/$package_lower_name/$package_name"
        echo "<a href='$package_name'>$package_name</a><br/>" > "$local_pipy_dir_path/simple/$package_lower_name/index.html"

        # build "$local_pipy_dir_path/simple/$package_lower_name/index.html"
        # And index.html.script is a CACHE
        touch "$local_pipy_dir_path/simple/$package_lower_name/index.html.script"
        echo "<a href=\"$package_name\">$package_name</a><br/>" >> "$local_pipy_dir_path/simple/$package_lower_name/index.html.script"
        cat > "$local_pipy_dir_path/simple/$package_lower_name/index.html" <<EOF
<html>
<head><title>Links for $package_lower_name</title></head>
<body>
<h1>Links for $package_lower_name</h1>
$(cat "$local_pipy_dir_path/simple/$package_lower_name/index.html.script")
</body>
</html>
EOF
        index_html_text=$index_html_text"<a href=\"$package_lower_name/\">$package_lower_name</a><br/>"$'\n'

    done < "$requirement_file_path"

    # index.html.script is like a CACHE to restore the storage info before.
    touch "$local_pipy_dir_path/simple/index.html.script"
    echo "$index_html_text" >> "$local_pipy_dir_path/simple/index.html.script"

    # use index.html.script to rewrite index.html
    echo '<html><head><title>Simple Index</title><meta name="api-version" value="2"/></head><body>' > "$local_pipy_dir_path/simple/index.html"
    cat "$local_pipy_dir_path/simple/index.html.script" >> "$local_pipy_dir_path/simple/index.html"
    echo "</body></html>" >> "$local_pipy_dir_path/simple/index.html"
    [ x"$PYPI_REQUIREMENT_OUTPUT" = x"true" ] && echo "$pypi_requirement_text" > $PYPI_REQUIREMENT_OUTPUT_PATH
}

build_pipy_source_directory
exit 0
