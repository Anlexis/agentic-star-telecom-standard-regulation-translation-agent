"""AgentCore Platform v1.0"""

# TEL-C2-008 — PostProcessNode
# Outer backbone post_process slot: the output boundary. This is the last node
# that can decide what the caller receives, so it owns the release decision.
#
# Output invariant (see docs/02_design.md — "Output boundary"):
#   A released report carries the translation, the configured target forms of
#   the terminology it preserved, and inert caller identifiers. It must never
#   carry a credential shape, a chat-template control token, or a body larger
#   than the filing channel accepts. No rounding grid applies: this template
#   renders no monetary aggregates, and rounding a standards figure would
#   falsify the document it is translating.
#
# Containment (why a refusal CLEARS rather than raises):
#   The framework's own get_output() resolves the caller-visible answer as
#   `formatted_output or result`, with no status check, so returning an error
#   status while leaving `result` in place ships the un-gated report inside the
#   error envelope. A falsy formatted_output makes it worse — it activates the
#   very fallback it looks like it suppresses. On violation this node therefore
#   returns an error status, clears every answer-bearing field, and puts a
#   TRUTHY notice in formatted_output.
#
#   The notice is composed from constants and the finding CLASS only. It is
#   never rebuilt from the state that was just cleared: a replacement assembled
#   out of the same state is containment in form only.
#
# Trust: declared ANONYMOUS deliberately. The caller trust gate is enforced at
# pre_process; making the output boundary itself trust-gated would let a trust
# denial skip the release decision on exactly the paths that need it most.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.services.caller_contract import find_control_tokens

logger = logging.getLogger(__name__)

_DEFAULT_MAX_REPORT_CHARS = 8_000

# Every state key that can carry answer text or a payload. On a refusal each one
# is written back empty, so nothing survives in the delta for a checkpoint or a
# downstream reader to pick up. Adding an answer-bearing field to the state
# means adding it here; the inventory test pins that.
_ANSWER_BEARING_KEYS = (
    "result",
    "translation_report",
    "translated_text",
    "glossary_hits",
    "accuracy_flags",
    "accuracy_score",
    "oos_reason",
)

# Closed-set violation labels. A caller-facing notice and an error entry name a
# label from this set and nothing else — never the matched text, never an
# upstream error body, never a source path.
_VIOLATION_CREDENTIAL = "credential_shape"
_VIOLATION_CONTROL_TOKEN = "control_token"
_VIOLATION_OVERSIZED = "report_exceeds_channel_limit"
_VIOLATION_UPSTREAM = "upstream_failure"

_NOTICE = {
    _VIOLATION_CREDENTIAL: (
        "The translation was withheld: the rendered report matched a credential pattern. "
        "Remove the credential from the source document and resubmit."
    ),
    _VIOLATION_CONTROL_TOKEN: (
        "The translation was withheld: the rendered report contained chat-template control tokens. "
        "Remove them from the source document and resubmit."
    ),
    _VIOLATION_OVERSIZED: (
        "The translation was withheld: the rendered report exceeds the size the filing channel accepts. "
        "Split the document and resubmit."
    ),
    _VIOLATION_UPSTREAM: (
        "No translation was produced. The request did not complete; nothing was released. "
        "Check the request against the accepted contract and resubmit."
    ),
}


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Apply the output boundary and expose the final report.

    Input state keys:
        translation_report: str | None — report rendered by the inner boundary
        status:             str        — status carried out of the main slot

    Output state keys (partial dict):
        formatted_output: str        — always truthy
        result:           str | None
        status:           str
        plus the cleared answer-bearing fields on a refusal
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        declared = (settings or {}).get("max_report_chars", _DEFAULT_MAX_REPORT_CHARS)
        self._max_report_chars = (
            int(declared) if isinstance(declared, int) and not isinstance(declared, bool) else _DEFAULT_MAX_REPORT_CHARS
        )

    # ── Release decision ──────────────────────────────────────────────────────

    def _violation(self, report: str) -> Optional[str]:
        """Return the violation label for *report*, or None when it may ship.

        The credential scan runs FIRST, on the untouched text: a pattern scan
        must see the text before anything else rewrites it. It delegates to the
        framework's own detector rather than a local pattern list, so this
        refusal set cannot end up narrower than the set the framework blocks on
        — a gap there would make the framework raise after this node returned,
        and the wrapper would discard the clearing along with the rest of the
        delta.
        """
        if detect_credentials(report):
            return _VIOLATION_CREDENTIAL
        if find_control_tokens(report):
            return _VIOLATION_CONTROL_TOKEN
        if len(report) > self._max_report_chars:
            return _VIOLATION_OVERSIZED
        return None

    def _withhold(self, state: AgentState, violation: str) -> Dict[str, Any]:
        """Return a fully cleared delta with a truthy caller notice.

        Nothing here is read back out of state: every value is a constant or the
        violation label. Inert provenance (review_required, out_of_scope) is
        pinned to fixed values rather than carried.
        """
        cleared: Dict[str, Any] = {key: None for key in _ANSWER_BEARING_KEYS}
        cleared["glossary_hits"] = []
        cleared["accuracy_flags"] = []
        return {
            **cleared,
            "formatted_output": _NOTICE[violation],
            "review_required": True,
            "out_of_scope": False,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: output withheld ({violation})"],
        }

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # Checked BEFORE the upstream-withhold branch below. A declined request
        # produced no report, so that branch would fire and turn a completed,
        # actionable refusal back into a bare withheld-output error - undoing the
        # whole point of settling a reason upstream.
        marker = state.get("error_code")
        if marker:
            emit_trace_event("output_not_produced", {"reason": marker}, state)
            return {
                "formatted_output": _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED),
                "result": None,
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        incoming_status = state.get("status")
        report = state.get("translation_report") or ""

        if incoming_status != AgentStatus.SUCCESS.value or not report.strip():
            logger.warning("PostProcessNode: withholding output — no successful report reached the boundary")
            emit_trace_event(
                "output_withheld",
                {"violation": _VIOLATION_UPSTREAM},
                state,
            )
            return self._withhold(state, _VIOLATION_UPSTREAM)

        violation = self._violation(report)
        if violation:
            logger.error("PostProcessNode: output boundary violation — %s", violation)
            emit_trace_event(
                "output_withheld",
                {"violation": violation, "report_chars": len(report), "limit": self._max_report_chars},
                state,
            )
            return self._withhold(state, violation)

        flags: List[str] = [str(f) for f in (state.get("accuracy_flags") or [])]
        logger.info("PostProcessNode: released report (%d chars, flags=%d)", len(report), len(flags))
        emit_trace_event(
            "output_released",
            {
                "report_chars": len(report),
                "review_required": bool(state.get("review_required")),
                "flag_count": len(flags),
            },
            state,
        )
        return {
            "formatted_output": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
