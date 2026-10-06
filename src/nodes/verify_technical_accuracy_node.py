"""AgentCore Platform v1.0"""

# TEL-C2-008 — VerifyTechnicalAccuracyNode
# Inner workflow step 4: post-translation terminology check. Verifies that every
# configured telecom term present in the source survives into the translation
# in its configured target form, scores the preservation ratio, and decides
# whether the result needs human review against the caller's threshold.
#
# This node scores; it does not gate. The output boundary decides what ships.
#
# Input state keys:
#   validated_text:  str        — original source text
#   translated_text: str        — output from the translation node
#   glossary_hits:   list[str]  — term ids applied during translation
#   min_accuracy:    float      — caller threshold (validated upstream)
#   out_of_scope:    bool       — skip when True
#
# Output state keys (partial dict):
#   accuracy_score:  float      — 0.0-1.0 ratio of expected terms preserved
#   accuracy_flags:  list[str]  — term ids expected but absent from the output
#   review_required: bool       — score below the caller's threshold
#   status:          AgentStatus.SUCCESS.value

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import TelecomGlossary

logger = logging.getLogger(__name__)

_DEFAULT_MIN_ACCURACY = 0.80


class VerifyTechnicalAccuracyNode(FunctionNode):
    """Score telecom-term preservation and decide whether review is required.

    Deterministic check against the SAME configured glossary the translation
    used — the two nodes share one declaration, so a terminology change cannot
    move them out of step.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        glossary: Optional[TelecomGlossary] = None,
        default_min_accuracy: float = _DEFAULT_MIN_ACCURACY,
    ) -> None:
        super().__init__()
        self._glossary = glossary if glossary is not None else TelecomGlossary({})
        self._default_min_accuracy = float(default_min_accuracy)

    def _threshold(self, state: AgentState) -> float:
        raw = state.get("min_accuracy")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return self._default_min_accuracy
        return float(raw)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on a document that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        threshold = self._threshold(state)

        if state.get("out_of_scope", False):
            logger.info("VerifyTechnicalAccuracyNode: out of scope — nothing to verify")
            emit_trace_event(
                "accuracy_verified",
                {"score": 1.0, "flags": [], "threshold": threshold, "reason": "out_of_scope"},
                state,
            )
            return {
                "accuracy_score": 1.0,
                "accuracy_flags": [],
                "review_required": False,
                "status": AgentStatus.SUCCESS.value,
            }

        source = state.get("validated_text", "") or ""
        translated = state.get("translated_text", "") or ""
        applied = {str(t) for t in (state.get("glossary_hits") or [])}

        expected = self._glossary.match(source)
        if not expected:
            logger.info("VerifyTechnicalAccuracyNode: source carries no configured terminology")
            emit_trace_event(
                "accuracy_verified",
                {"score": 1.0, "flags": [], "threshold": threshold, "reason": "no_terms_in_source"},
                state,
            )
            return {
                "accuracy_score": 1.0,
                "accuracy_flags": [],
                "review_required": False,
                "status": AgentStatus.SUCCESS.value,
            }

        translated_lower = translated.lower()
        flags: List[str] = []
        matched = 0
        for term_id in expected:
            target_form = self._glossary.target_form(term_id).lower()
            source_form = self._glossary.source_form(term_id).lower()
            if term_id in applied or target_form in translated_lower or source_form in translated_lower:
                matched += 1
            else:
                flags.append(term_id)

        score = max(0.0, min(1.0, matched / len(expected)))
        review_required = score < threshold

        logger.info(
            "VerifyTechnicalAccuracyNode: score=%.2f matched=%d/%d threshold=%.2f review=%s",
            score,
            matched,
            len(expected),
            threshold,
            review_required,
        )
        emit_trace_event(
            "accuracy_verified",
            {
                "score": round(score, 4),
                "flags": flags,
                "threshold": threshold,
                "expected": len(expected),
                "matched": matched,
            },
            state,
        )
        return {
            "accuracy_score": score,
            "accuracy_flags": flags,
            "review_required": review_required,
            "status": AgentStatus.SUCCESS.value,
        }
