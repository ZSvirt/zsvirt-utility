#!/bin/bash
ZSTACK_SOURCE=$1
PYTHON_APIS_DIR=$2
cd ${ZSTACK_SOURCE}

if [ -d zsvirt-ee/tests-ee/sdk-tools-ee ]; then
  cd zsvirt-ee/tests-ee/sdk-tools-ee
  mvn test -Dtest=TestGeneratePython2ApiBindingEe -Djacoco.skip=true -DpythonApisDir=${PYTHON_APIS_DIR}
else
  cd tests/sdk-tools
  mvn test -Dtest=TestGeneratePython2ApiBinding -Djacoco.skip=true -DpythonApisDir=${PYTHON_APIS_DIR}
fi
