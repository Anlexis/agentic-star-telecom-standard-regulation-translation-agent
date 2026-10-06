"""AgentCore Platform v1.0"""

# TEL-C2-008 — TranslateWithTelecomTermsNode
# Inner workflow step 3: EN<->JA translation with telecom terminology
# preservation.
#
# The translation body is deterministic: it composes the target-language
# rendering from the source text and the configured terminology, and marks the
# integration point where a model call replaces the composition. The glossary
# itself is NOT hard-coded — it is the declared translation.glossary from
# config/config.yaml, injected at construction. A caller may narrow it to a
# named subset and cap how many entries are applied.
#
# Input state keys:
#   validated_text:    str        — text to translate
#   detected_language: str        — "en" or "ja"
#   target_language:   str        — the language the translation targets
#   selected_terms:    list[str]  — caller's glossary selection (empty = all)
#   max_terms:         int        — cap on entries applied
#   out_of_scope:      bool       — skip translation when True
#
# Output state keys (partial dict):
#   translated_text: str        — translated output
#   glossary_hits:   list[str]  — configured term ids matched in the source
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

_DEFAULT_MAX_TERMS = 16


class TranslateWithTelecomTermsNode(FunctionNode):
    """EN<->JA telecom translation with glossary-driven term preservation.

    The node holds no terminology of its own: everything it preserves comes
    from the glossary it was constructed with, which is the declared
    translation.glossary. An empty declaration therefore preserves nothing —
    visibly, in glossary_hits — rather than silently falling back to a copy
    embedded in this file.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        glossary: Optional[TelecomGlossary] = None,
        max_glossary_terms: int = _DEFAULT_MAX_TERMS,
    ) -> None:
        super().__init__()
        self._glossary = glossary if glossary is not None else TelecomGlossary({})
        self._max_glossary_terms = int(max_glossary_terms)

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on a document that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        if state.get("out_of_scope", False):
            logger.info("TranslateWithTelecomTermsNode: out of scope — skipping translation")
            emit_trace_event("translation_skipped", {"reason": "out_of_scope"}, state)
            return {
                "translated_text": "",
                "glossary_hits": [],
                "status": AgentStatus.SUCCESS.value,
            }

        source_text = state.get("validated_text", "") or ""
        source_lang = state.get("detected_language", "en")
        target_lang = state.get("target_language", "ja")

        selected = state.get("selected_terms") or []
        cap = state.get("max_terms")
        limit = cap if isinstance(cap, int) and not isinstance(cap, bool) else self._max_glossary_terms
        limit = min(limit, self._max_glossary_terms)

        hits: List[str] = self._glossary.match(source_text, selected_ids=list(selected), limit=limit)

        # The target-language rendering. Each matched term is carried over in
        # its configured target form so terminology survives the conversion;
        # this is the seam a model-backed translation replaces.
        rendered = source_text
        for term_id in hits:
            source_form = self._glossary.source_form(term_id)
            target_form = self._glossary.target_form(term_id)
            if source_form != target_form:
                rendered = self._replace_case_insensitive(rendered, source_form, target_form)

        translated_text = f"[{source_lang}->{target_lang}] {rendered}"

        logger.info(
            "TranslateWithTelecomTermsNode: %s->%s, terms applied=%d (limit=%d)",
            source_lang,
            target_lang,
            len(hits),
            limit,
        )
        emit_trace_event(
            "translation_produced",
            {
                "source_language": source_lang,
                "target_language": target_lang,
                "glossary_hits": hits,
                "term_limit": limit,
                "output_chars": len(translated_text),
            },
            state,
        )
        return {
            "translated_text": translated_text,
            "glossary_hits": hits,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _replace_case_insensitive(text: str, needle: str, replacement: str) -> str:
        """Replace every case-insensitive occurrence of *needle* in *text*.

        Implemented as a scan rather than a regular expression so a configured
        term containing regex metacharacters cannot change the match semantics.
        """
        if not needle:
            return text
        lowered_text = text.lower()
        lowered_needle = needle.lower()
        out: List[str] = []
        index = 0
        while True:
            found = lowered_text.find(lowered_needle, index)
            if found == -1:
                out.append(text[index:])
                return "".join(out)
            out.append(text[index:found])
            out.append(replacement)
            index = found + len(needle)
