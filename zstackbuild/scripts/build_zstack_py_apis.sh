#!/bin/bash
ZSTACK_SOURCE=$1
PYTHON_APIS_DIR=$2
cd ${ZSTACK_SOURCE}/tests/checker

mvn test -Dtest=TestGenerateApiPython2ClassAndJsonTemplate -Djacoco.skip=true -DpythonApisDir=${PYTHON_APIS_DIR}
