"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# When the platform routes the request instead, the gateway calls agent.invoke()
# directly and this module is not in the path.

import json
import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import Graph, load_runtime_config
from src.services.caller_contract import screen_context_credentials

app = FastAPI(title="Agent")

# The declared runtime values only govern the agent when it is constructed WITH
# them — a bare Graph() would run on hard-coded defaults with config/config.yaml
# ignored.
agent = Graph(config=load_runtime_config())
agent.compile()
# namespace / agent_name match the manifest (config/agent.yaml: namespace, class).
agent.provision_secrets(secrets_factory(namespace="tel", agent_name="TelecomStandardTranslationAgent"))

# Adapter-level size caps. The document rides the `input` field; `input_context`
# carries only bounded control fields, so its cap is deliberately small.
_INPUT_MAX_BYTES = 256 * 1024
_INPUT_CONTEXT_MAX_BYTES = 16 * 1024


class InvokeRequest(BaseModel):
    """Caller request.

    `input` is the document text to translate. `input_context` carries the
    structured control fields documented in docs/02_design.md — a document
    reference, a target-language selection, a terminology-preservation
    threshold, a terminology cap and a glossary selection. Unknown keys are
    dropped by the caller-data contract.
    """

    input: str
    session_id: str = ""
    input_context: Dict[str, Any] = {}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a bearer token and run at
    # VERIFIED_EXTERNAL, the level this agent's manifest declares. Trust
    # established by middleware is never demoted. This is a deployment-level
    # caller credential, not an agent secret — no invocation context exists
    # before authentication, so ctx.secrets does not apply.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — never disclose whether the token was
            # absent, malformed or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    # Size caps run before any processing; the error names the field, never the value.
    if len(req.input.encode("utf-8", errors="replace")) > _INPUT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="input exceeds the size limit.")
    try:
        context_bytes = len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="input_context is not JSON-serializable.")
    if context_bytes > _INPUT_CONTEXT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the size limit.")

    # A credential-shaped value anywhere in input_context makes the framework's
    # first node fail with an internal error the caller cannot act on: the
    # context is returned verbatim into that node's result, and the platform
    # output gate scans every value of every result. Undeclared keys are not
    # stripped before that happens, so declaring an inert contract is not
    # immunity. The request cannot succeed either way, so refuse it here with a
    # message that names the field. The check delegates to the framework's own
    # detector, so this refusal set and the platform block set cannot drift.
    # 400, not 422: pydantic owns 422 and answers with a list of error objects
    # there, which would make client handling ambiguous.
    offenders = screen_context_credentials(req.input_context)
    if offenders:
        raise HTTPException(
            status_code=400,
            detail=f"credential-shaped content in {', '.join(offenders)}; remove it and resubmit.",
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=req.input_context)
        return result


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "TelecomStandardTranslationAgent"}
