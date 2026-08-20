#!/bin/bash

mkdir -p ./requirements
mkdir -p ./pypi

bash pypi_source_builder.sh build \
    --package-list ./package_list_base.txt \
    --target-path ./pypi \
    --requirement-output-path ./requirements/requirements1.txt
bash pypi_source_builder.sh build \
    --package-list ./package_list_virtualenv.txt \
    --target-path ./pypi \
    --requirement-output-path ./requirements/requirements2.txt

pip install -r requirements/requirements1.txt -i "file://$(pwd)/pypi/simple"
pip install -r requirements/requirements2.txt -i "file://$(pwd)/pypi/simple"
