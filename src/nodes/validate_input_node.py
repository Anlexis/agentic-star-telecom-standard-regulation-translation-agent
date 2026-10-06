"""AgentCore Platform v1.0"""

# TEL-C2-008 — ValidateInputNode
# First node of the inner workflow. Re-applies the caller-data contract inside
# the pipeline so the workflow keeps its guarantee even when it is driven
# directly, and normalises the caller control record seeded by the context
# bridge.
#
# Defence in depth, not duplication: the outer pre-process node owns the caller
# boundary, but the inner graph is independently invocable, and a contract that
# only exists one layer up is a contract the inner pipeline does not have.
#
# Input state keys:
#   user_input:            str  — document text handed across the boundary
#   translation_controls:  dict — validated control record from the bridge
#
# Output state keys (partial dict):
#   validated_text:   str        — screened document text
#   validation_error: str | None — field-naming error when invalid
#   target_language_request: str | None
#   document_ref:     str | None
#   min_accuracy:     float
#   max_terms:        int
#   selected_terms:   list[str]
#   status:           AgentStatus.SUCCESS.value | AgentStatus.ERROR.value

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.services.caller_contract import (
    FieldContractError,
    parse_bounded_count,
    parse_bounded_ratio,
    HostileContentError,
    screen_untrusted_text,
    validate_glossary_ids,
    validate_inert_id,
    validate_language,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_INPUT_CHARS = 10_000
_DEFAULT_MIN_ACCURACY = 0.80
_DEFAULT_MAX_TERMS = 16
_MAX_TERMS_CEILING = 256


class ValidateInputNode(FunctionNode):
    """Validate the document and the caller controls before detection.

    Pure deterministic validation — no model client. Constructed with the
    declared document-size limit so the bound is the configured one.

    Trust: the caller trust gate is enforced once, at the outer pre-process
    slot. Requiring a higher level here would make the workflow unreachable for
    the very callers the manifest declares it serves.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS) -> None:
        super().__init__()
        self._max_input_chars = int(max_input_chars)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        controls = state.get("translation_controls") or {}
        if not isinstance(controls, dict):
            controls = {}

        try:
            text = screen_untrusted_text(state.get("user_input", ""), "input", self._max_input_chars)
            if not text:
                raise FieldContractError("input is required (the document text to translate)", code="EMPTY_INPUT")

            raw_ref = controls.get("document_ref")
            document_ref: Optional[str] = (
                validate_inert_id(raw_ref, "input_context.document_ref") if raw_ref not in (None, "") else None
            )
            target_request = validate_language(controls.get("target_language"), "input_context.target_language")

            raw_min = controls.get("min_accuracy")
            min_accuracy = (
                _DEFAULT_MIN_ACCURACY
                if raw_min is None
                else parse_bounded_ratio(raw_min, "input_context.min_accuracy", 0.0, 1.0)
            )

            raw_max_terms = controls.get("max_terms")
            max_terms = (
                _DEFAULT_MAX_TERMS
                if raw_max_terms is None
                else parse_bounded_count(raw_max_terms, "input_context.max_terms", 1, _MAX_TERMS_CEILING)
            )

            selected_terms: List[str] = validate_glossary_ids(controls.get("glossary_ids"))
        except HostileContentError as exc:
            # Caught BEFORE the base class below, and deliberately so: spliced
            # instructions are not a value the caller can correct, so this one
            # refusal keeps terminating while its siblings now complete.
            logger.warning("ValidateInputNode: hostile content — %s", exc)
            emit_trace_event("input_validation_failed", {"reason": "hostile_content"}, state)
            return {
                "validation_error": f"ValidateInputNode: {exc}",
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ValidateInputNode: {exc}"],
            }
        except FieldContractError as exc:
            logger.info("ValidateInputNode: contract violation — %s", exc)
            emit_trace_event("input_validation_failed", {"detail": str(exc)}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "validation_error": f"ValidateInputNode: {exc}",
                "status": AgentStatus.SUCCESS.value,
                "error_code": exc.code,
                "error_log": [f"ValidateInputNode: {exc}"],
            }

        logger.info("ValidateInputNode: accepted document (%d chars)", len(text))
        emit_trace_event(
            "input_validated",
            {
                "document_chars": len(text),
                "document_ref": document_ref,
                "target_language_request": target_request,
                "min_accuracy": min_accuracy,
                "max_terms": max_terms,
                "selected_terms": selected_terms,
            },
            state,
        )
        return {
            "validated_text": text,
            "validation_error": None,
            "document_ref": document_ref,
            "target_language_request": target_request,
            "min_accuracy": min_accuracy,
            "max_terms": max_terms,
            "selected_terms": selected_terms,
            "status": AgentStatus.SUCCESS.value,
        }
