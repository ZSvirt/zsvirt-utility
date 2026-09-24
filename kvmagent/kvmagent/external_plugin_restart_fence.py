# -*- coding: utf-8 -*-
from __future__ import absolute_import

import json
import re

from zstacklib.utils.restart_fence import AgentRestartFence


HOST_OPERATIONS_STATUS_PATH = "/kvmagent/operations/status"
RESTART_FENCE_PATH = "/kvmagent/operations/restart-fence"
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LOOPBACK = frozenset(("127.0.0.1", "::1", "::ffff:127.0.0.1"))


def status_handler(unused_request):
    result = AgentRestartFence.snapshot()
    result["success"] = True
    return json.dumps(result, sort_keys=True)


def restart_fence_handler(request):
    import cherrypy
    from zstacklib.utils import http

    remote_ip = getattr(getattr(request, "remote", None), "ip", None)
    if remote_ip not in _LOOPBACK:
        raise cherrypy.HTTPError(403, "restart fence is restricted to loopback")
    if str(getattr(request, "method", "")).upper() != "POST":
        raise cherrypy.HTTPError(405, "restart fence requires POST")
    try:
        body = request.body.fp.read() if request.body else "{}"
        command = json.loads(body or "{}")
        if not isinstance(command, dict):
            raise ValueError("request body must be a JSON object")
        operation_id = command.get("operationId")
        drain_timeout = int(command.get("drainTimeoutSeconds", 30))
        lease_seconds = int(command.get("leaseSeconds", 90))
        if not _OPERATION_ID.match(str(operation_id or "")):
            raise ValueError("operationId is invalid")
        if drain_timeout < 1 or drain_timeout > 600:
            raise ValueError("drainTimeoutSeconds must be between 1 and 600")
        if lease_seconds <= drain_timeout or lease_seconds > 3600:
            raise ValueError("leaseSeconds must exceed drainTimeoutSeconds and be at most 3600")
    except (TypeError, ValueError) as error:
        return json.dumps({
            "success": False,
            "errorCode": "RESTART_FENCE_REQUEST_INVALID",
            "error": str(error),
        }, sort_keys=True)

    if http.AsyncUirHandler.STOP_WORLD:
        return json.dumps({
            "success": False,
            "errorCode": "KVM_AGENT_BASE_UNAVAILABLE",
            "error": "KVM Agent is already stopping",
        }, sort_keys=True)

    acquired, snapshot = AgentRestartFence.acquire(
        drain_timeout, lease_seconds)
    result = dict(snapshot)
    result.update({
        "success": acquired,
        "operationId": operation_id,
    })
    if not acquired:
        result.update({
            "errorCode": "HOST_OPERATION_IN_PROGRESS",
            "error": "asynchronous KVM Agent operations did not reach an idle restart point",
        })
    return json.dumps(result, sort_keys=True)
