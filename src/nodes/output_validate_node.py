"""AgentCore Platform v1.0"""

# TEL-C2-008 — OutputValidateNode
# Final node of the inner workflow: renders the translation report, handles the
# out-of-scope response, and refuses to release anything when the pipeline
# produced no translation.
#
# Domain output hook:
#   _extra_security_gate_output(result) is the framework's injection point for a
#   node-level output check. Its contract takes the result dict and RETURNS it;
#   the framework assigns the return value back over the node's delta, so an
#   implementation that returns None erases the delta and the node fails with an
#   attribute error on every invocation. What it enforces here is the
#   containment inventory: on a non-success delta every answer-bearing key this
#   node knows about must be empty, so a field added later cannot quietly ride
#   out on a refusal.
#
# The @final framework gate (_security_gate_output) must never be overridden —
# the domain hook is the supported extension point.
#
# Input state keys:
#   out_of_scope, oos_reason, translated_text, glossary_hits, accuracy_score,
#   accuracy_flags, review_required, document_ref, detected_language,
#   target_language
#
# Output state keys (partial dict):
#   result, translation_report, status, error_log (only on refusal)

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import TelecomGlossary

logger = logging.getLogger(__name__)

# Keys this node can put answer text into. The inventory guard checks all of
# them on a refusal; adding a key here is the only way to add one to the delta.
_ANSWER_BEARING_KEYS = ("result", "translation_report", "translated_text")

# Closed-set refusal labels. A refusal reason is a label from this set — never
# an upstream error body, which is unbounded third-party text.
_REFUSAL_EMPTY_TRANSLATION = "empty_translation"


class OutputValidateNode(FunctionNode):
    """Render the translation report, or refuse when there is nothing to report."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, glossary: Optional[TelecomGlossary] = None) -> None:
        super().__init__()
        self._glossary = glossary if glossary is not None else TelecomGlossary({})

    # ── Domain output hook (framework-invoked; must return the result) ────────

    def _extra_security_gate_output(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Containment inventory guard for this node's delta.

        Called by the framework after its own credential scan, with the dict
        this node returned, and its return value replaces that dict. On a
        non-success delta every answer-bearing key must be falsy; a violation
        raises, which the node wrapper converts into an error result.
        """
        if result.get("status") == AgentStatus.SUCCESS.value:
            return result
        carried = [key for key in _ANSWER_BEARING_KEYS if result.get(key)]
        if carried:
            raise ValueError(f"output boundary: non-success delta carried answer fields {sorted(carried)}")
        return result

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on a document that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        if state.get("out_of_scope"):
            reason = state.get("oos_reason") or "the request is outside the supported language pair"
            report = self._render_out_of_scope(state, str(reason))
            logger.info("OutputValidateNode: out of scope — returning the explanatory response")
            emit_trace_event(
                "translation_output",
                {
                    "out_of_scope": True,
                    "glossary_hits": state.get("glossary_hits", []),
                    "accuracy_score": state.get("accuracy_score", 0.0),
                    "report_chars": len(report),
                },
                state,
            )
            return {
                "result": report,
                "translation_report": report,
                "status": AgentStatus.SUCCESS.value,
            }

        translated_text = state.get("translated_text") or ""
        if not translated_text.strip():
            logger.warning("OutputValidateNode: refusing — the pipeline produced no translation")
            emit_trace_event(
                "translation_refused",
                {"reason": _REFUSAL_EMPTY_TRANSLATION},
                state,
            )
            return {
                "result": None,
                "translation_report": None,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputValidateNode: refused ({_REFUSAL_EMPTY_TRANSLATION})"],
            }

        report = self._render_report(state, translated_text)
        logger.info(
            "OutputValidateNode: report rendered (%d chars, review_required=%s)",
            len(report),
            bool(state.get("review_required")),
        )
        emit_trace_event(
            "translation_output",
            {
                "out_of_scope": False,
                "glossary_hits": state.get("glossary_hits", []),
                "accuracy_score": state.get("accuracy_score", 0.0),
                "accuracy_flags": state.get("accuracy_flags", []),
                "review_required": bool(state.get("review_required")),
                "report_chars": len(report),
            },
            state,
        )
        return {
            "result": report,
            "translation_report": report,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _header(self, state: AgentState) -> List[str]:
        ref = state.get("document_ref")
        return [
            "Telecom Standard Translation Report",
            f"Document reference: {ref if ref else '(not supplied)'}",
        ]

    def _render_out_of_scope(self, state: AgentState, reason: str) -> str:
        lines = self._header(state)
        lines.append("Outcome: not translated")
        lines.append(f"Reason: {reason}")
        return "\n".join(lines)

    def _render_report(self, state: AgentState, translated_text: str) -> str:
        hits = [str(t) for t in (state.get("glossary_hits") or [])]
        preserved = [self._glossary.target_form(t) for t in hits if t in self._glossary.term_ids]
        flags = [str(f) for f in (state.get("accuracy_flags") or [])]
        score = state.get("accuracy_score")
        score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) and not isinstance(score, bool) else "n/a"

        lines = self._header(state)
        lines.append(
            f"Direction: {state.get('detected_language', 'unknown')} -> {state.get('target_language', 'unknown')}"
        )
        lines.append(f"Terminology preserved: {', '.join(preserved) if preserved else '(none)'}")
        lines.append(f"Terminology not carried over: {', '.join(flags) if flags else '(none)'}")
        lines.append(f"Terminology preservation score: {score_text}")
        lines.append(f"Review required: {'yes' if state.get('review_required') else 'no'}")
        lines.append("")
        lines.append("Translation:")
        lines.append(translated_text)
        return "\n".join(lines)
