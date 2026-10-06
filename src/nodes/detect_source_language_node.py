"""AgentCore Platform v1.0"""

# TEL-C2-008 — DetectSourceLanguageNode
# Inner workflow step 2: detect the source language of validated_text and pick
# the target language (EN<->JA). Flags out-of-scope when the language pair is
# not one this agent serves.
#
# The classification threshold is a DECLARED value: the node is constructed with
# translation.ja_ratio_threshold from config/config.yaml, so a deployment can
# retune detection for its own corpus without a code change.
#
# Input state keys:
#   validated_text:          str        — non-empty text from ValidateInputNode
#   target_language_request: str | None — caller's explicit target selection
#
# Output state keys (partial dict):
#   detected_language: str        — "en" or "ja"
#   target_language:   str        — the language the translation targets
#   out_of_scope:      bool       — True when the pair is not one we serve
#   oos_reason:        str | None — populated when out_of_scope is True
#   status:            AgentStatus.SUCCESS.value on all normal paths
#
# Status rule: out-of-scope is NOT an error. The agent behaved correctly, it
# simply cannot translate this pair. ERROR is reserved for unexpected failures.

import logging
from typing import Any, ClassVar, Dict, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

_DEFAULT_JA_RATIO_THRESHOLD = 0.20

# Korean Hangul ranges — used to detect an explicitly unsupported language so
# the request can be flagged out-of-scope rather than mis-classified as English.
_HANGUL_SYLLABLES = (0xAC00, 0xD7A3)
_HANGUL_JAMO = (0x1100, 0x11FF)
_HANGUL_COMPAT_JAMO = (0x3130, 0x318F)

_OOS_UNSUPPORTED_SCRIPT = "the source text is in a script outside the English-Japanese pair this agent serves"
_OOS_SAME_LANGUAGE = "the requested target language is the language the source is already written in"


def _in_range(code_point: int, span: Tuple[int, int]) -> bool:
    """Return True when *code_point* falls within the inclusive *span*."""
    return span[0] <= code_point <= span[1]


def _is_japanese_char(ch: str) -> bool:
    """Return True for Hiragana, Katakana, or CJK unified ideographs."""
    cp = ord(ch)
    return (
        _in_range(cp, (0x3040, 0x309F))  # Hiragana
        or _in_range(cp, (0x30A0, 0x30FF))  # Katakana
        or _in_range(cp, (0x4E00, 0x9FFF))  # CJK unified ideographs
    )


def _is_hangul_char(ch: str) -> bool:
    """Return True for Korean Hangul syllables or jamo (an unsupported script)."""
    cp = ord(ch)
    return _in_range(cp, _HANGUL_SYLLABLES) or _in_range(cp, _HANGUL_JAMO) or _in_range(cp, _HANGUL_COMPAT_JAMO)


class DetectSourceLanguageNode(FunctionNode):
    """Detect the EN/JA source language by deterministic character-set analysis.

    Classification is a character-frequency heuristic, not a model call:
    - Japanese-script share at or above the declared threshold → "ja"
    - any Hangul present with no Japanese markers            → out of scope
    - otherwise                                              → "en"

    The caller may name the target language explicitly; a target equal to the
    detected source is out of scope, because it names no translation.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, ja_ratio_threshold: float = _DEFAULT_JA_RATIO_THRESHOLD) -> None:
        super().__init__()
        self._ja_ratio_threshold = float(ja_ratio_threshold)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on a document that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        text = state.get("validated_text", "") or ""

        # Count script categories over alphabetic / ideographic characters only;
        # whitespace, digits and punctuation are language-neutral.
        letters = [ch for ch in text if ch.isalpha()]
        total = len(letters)

        ja_count = sum(1 for ch in letters if _is_japanese_char(ch))
        hangul_count = sum(1 for ch in letters if _is_hangul_char(ch))
        ja_ratio = (ja_count / total) if total else 0.0

        if hangul_count > 0 and ja_count == 0:
            logger.info("DetectSourceLanguageNode: out of scope — unsupported script")
            emit_trace_event(
                "language_detected",
                {"detected": "unknown", "target": "unknown", "out_of_scope": True, "reason": "unsupported_script"},
                state,
            )
            return {
                "detected_language": "unknown",
                "target_language": "unknown",
                "out_of_scope": True,
                "oos_reason": _OOS_UNSUPPORTED_SCRIPT,
                "status": AgentStatus.SUCCESS.value,
            }

        detected = "ja" if ja_ratio >= self._ja_ratio_threshold else "en"
        default_target = "en" if detected == "ja" else "ja"
        requested = state.get("target_language_request")
        target = requested if isinstance(requested, str) and requested else default_target

        if target == detected:
            logger.info("DetectSourceLanguageNode: out of scope — target equals source language")
            emit_trace_event(
                "language_detected",
                {"detected": detected, "target": target, "out_of_scope": True, "reason": "same_language"},
                state,
            )
            return {
                "detected_language": detected,
                "target_language": target,
                "out_of_scope": True,
                "oos_reason": _OOS_SAME_LANGUAGE,
                "status": AgentStatus.SUCCESS.value,
            }

        logger.info(
            "DetectSourceLanguageNode: detected=%s target=%s (ja_ratio=%.2f, threshold=%.2f)",
            detected,
            target,
            ja_ratio,
            self._ja_ratio_threshold,
        )
        emit_trace_event(
            "language_detected",
            {
                "detected": detected,
                "target": target,
                "out_of_scope": False,
                "ja_ratio": round(ja_ratio, 4),
                "threshold": self._ja_ratio_threshold,
            },
            state,
        )
        return {
            "detected_language": detected,
            "target_language": target,
            "out_of_scope": False,
            "oos_reason": None,
            "status": AgentStatus.SUCCESS.value,
        }
