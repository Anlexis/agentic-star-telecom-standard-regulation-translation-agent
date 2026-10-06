"""AgentCore Platform v1.0"""

# TEL-C2-008 — PreProcessNode
# Outer backbone pre_process slot: caller trust gate + caller-data contract.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level). This is the only
#     node that carries it, so an unauthenticated caller is refused here rather
#     than deep inside the workflow.
#   - Validate EVERY caller field against the explicit contract in
#     src/services/caller_contract.py: bounded numbers, closed vocabularies,
#     inert identifiers for anything that renders, control-token and directive
#     screening on the document text — fail closed, never echoing a value.
#   - Refuse a document that carries a credential shape. The framework's output
#     gate scans every value a node returns, so such a document makes the first
#     node of the workflow fail with a traceback and no actionable message; the
#     request cannot succeed either way, so a clean refusal that names the field
#     is strictly better than an opaque internal error.
#   - Write validated_input (the screened document) and validated_context (the
#     projected control record, JSON) for the main node's context bridge.
#   - Emit an audit event for every validation decision.
#
# The template owns this contract itself: the screens run in execute(), so the
# guarantee holds wherever the agent runs, with or without any platform-side
# input filtering in front of it.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.services.progress import emit_progress

from src.services.caller_contract import (
    FieldContractError,
    HostileContentError,
    screen_context_credentials,
    validate_translation_request,
)
from src.services.service import TelecomGlossary

logger = logging.getLogger(__name__)


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PreProcessNode(FunctionNode):
    """Caller-data contract enforcement for TEL-C2-008.

    Constructed with the declared `translation` settings, so the accepted
    document size, the default accuracy floor and the known glossary ids all
    come from config/config.yaml rather than from a copy held in this file.

    Input state keys:
        user_input:    str  — the document text to translate
        input_context: dict — caller control fields (document_ref,
                              target_language, min_accuracy, max_terms,
                              glossary_ids); unknown keys are dropped

    Output state keys (partial dict):
        validated_input:   str        — screened document text
        validated_context: str        — JSON control record for the main node
        enriched_context:  dict       — channel metadata
        status:            str
        error_log:         list[str]  — only on ERROR; names fields, never values
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self._settings: Dict[str, Any] = settings or {}
        self._glossary = TelecomGlossary.from_config(self._settings.get("glossary"))

    @staticmethod
    def _refuse(reason: str, code: str = "INVALID_REQUEST") -> Dict[str, Any]:
        """Return a refusal delta carrying a TRUTHY caller-facing notice.

        Two outcomes, chosen by whether the caller can act on the finding, and
        selected by an explicit argument at the call site rather than by the text
        of *reason* — so the distinction survives any later rewording.

        `code` non-empty — a value the caller can correct. The run COMPLETES
        carrying the reason, so the caller can send a corrected request on the
        same conversation instead of receiving only an exception type.

        `code` empty — a refusal the caller cannot reword their way past: a
        credential on the caller channel, or spliced instructions. Terminates.

        Either way the document is NOT translated and nothing is published.

        A refusal here short-circuits the backbone straight to finalize, so the
        output boundary never runs and the framework resolves the caller's
        answer as `formatted_output or result`. A falsy notice would activate
        that fallback; a truthy one both suppresses it and tells the caller
        which part of the contract was not met. *reason* names a FIELD and a
        RULE — the rejected value is never part of it.
        """
        if code:
            emit_progress(INPUT_REJECTED)
            return {
                # `reason` names a field and a rule; that belongs in the audit
                # channel. The caller reads a fixed sentence instead.
                "formatted_output": _DEGRADED_MESSAGES.get(code, INPUT_REJECTED),
                "result": None,
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"PreProcessNode: {reason}"],
            }
        return {
            "formatted_output": f"The request was not accepted: {reason}.",
            "result": None,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {reason}"],
        }

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})
        settings = self._settings
        glossary = self._glossary

        # ── Credential screen on the caller channels ──────────────────────────
        # Delegating to the framework's own detector keeps this refusal set
        # identical to the set the platform output gate blocks on, so nothing
        # can pass here and detonate one node later.
        offenders: List[str] = screen_context_credentials(input_context)
        if isinstance(user_input, str) and detect_credentials(user_input):
            offenders.insert(0, "input")
        if offenders:
            logger.warning("PreProcessNode: credential-shaped content in %s", offenders)
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "credential_shape", "fields": offenders},
                state,
            )
            # Terminal: a credential reaching the caller channel is a
            # containment event, not a formatting mistake to correct.
            return self._refuse(f"credential-shaped content in {', '.join(offenders)}", code="")

        # ── Caller-data contract (fail closed; errors name fields, not values) ─
        try:
            controls = validate_translation_request(
                user_input,
                input_context,
                max_input_chars=int(settings.get("max_input_chars", 10_000)),
                default_min_accuracy=float(settings.get("default_min_accuracy", 0.80)),
                max_glossary_terms=int(settings.get("max_glossary_terms", 16)),
                known_glossary_ids=glossary.term_ids,
            )
        except HostileContentError as exc:
            # Caught BEFORE the base class below, and deliberately so: spliced
            # instructions are not a value the caller can correct, so this one
            # refusal keeps terminating while its siblings now complete.
            logger.warning("PreProcessNode: hostile content — %s", exc)
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "hostile_content", "detail": str(exc)},
                state,
            )
            return self._refuse(str(exc), code="")
        except FieldContractError as exc:
            logger.warning("PreProcessNode: contract violation — %s", exc)
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "field_contract_violation", "detail": str(exc)},
                state,
            )
            return self._refuse(str(exc), code=exc.code)

        document = controls.pop("document_text")
        logger.info(
            "PreProcessNode: accepted document (%d chars), ref=%s, terms=%d",
            len(document),
            controls.get("document_ref") or "-",
            len(controls.get("glossary_ids") or []),
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "document_chars": len(document),
                "document_ref": controls.get("document_ref"),
                "target_language": controls.get("target_language"),
                "min_accuracy": controls.get("min_accuracy"),
                "max_terms": controls.get("max_terms"),
                "glossary_ids": controls.get("glossary_ids"),
            },
            state,
        )

        channel = input_context.get("channel") if isinstance(input_context, dict) else None
        return {
            "validated_input": document,
            "validated_context": json.dumps(controls, ensure_ascii=False),
            "enriched_context": {
                "source": "TelecomStandardTranslationAgent",
                "channel": channel if isinstance(channel, str) and channel.isidentifier() else "unknown",
            },
            "status": AgentStatus.SUCCESS.value,
        }
