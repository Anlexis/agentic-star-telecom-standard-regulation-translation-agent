"""AgentCore Platform v1.0"""

# TEL-C2-008 — telecom glossary service.
#
# Owns the declared terminology: the mapping between the term as it appears in
# the source document and the form that must be preserved in the translation.
# The whole glossary is declared in config/config.yaml under
# `translation.glossary` and reaches this class through the graph's runtime
# config, so a deployment can change the terminology without touching code.
#
# Service layer: domain lookups only — no routing, no business decisions, no
# credentials.

from __future__ import annotations

from typing import Any, Dict, List, Optional


class GlossaryError(ValueError):
    """The declared glossary is malformed."""


class TelecomGlossary:
    """Resolved telecom terminology for one invocation.

    Entries are keyed by an inert identifier (the id a caller may name in
    ``input_context.glossary_ids``) and carry the source form to match and the
    target form to preserve.
    """

    def __init__(self, entries: Dict[str, Dict[str, str]]) -> None:
        self._entries = entries

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, declared: Any) -> "TelecomGlossary":
        """Build a glossary from the declared ``translation.glossary`` mapping.

        Raises GlossaryError on a malformed declaration rather than degrading to
        an empty glossary: a silently empty glossary produces a translation that
        preserves nothing and still reports success.
        """
        if declared is None:
            return cls({})
        if not isinstance(declared, dict):
            raise GlossaryError("translation.glossary must be a mapping of term id to {source, target}")
        entries: Dict[str, Dict[str, str]] = {}
        for term_id, entry in declared.items():
            if not isinstance(term_id, str) or not term_id:
                raise GlossaryError("translation.glossary keys must be non-empty strings")
            if not isinstance(entry, dict):
                raise GlossaryError(f"translation.glossary['{term_id}'] must be a mapping")
            source = entry.get("source")
            target = entry.get("target")
            if not isinstance(source, str) or not source:
                raise GlossaryError(f"translation.glossary['{term_id}'].source must be a non-empty string")
            if not isinstance(target, str) or not target:
                raise GlossaryError(f"translation.glossary['{term_id}'].target must be a non-empty string")
            entries[term_id] = {"source": source, "target": target}
        return cls(entries)

    # ── Lookup ───────────────────────────────────────────────────────────────

    @property
    def term_ids(self) -> List[str]:
        """Every configured term id, in declaration order."""
        return list(self._entries)

    def is_empty(self) -> bool:
        """True when no terminology is configured."""
        return not self._entries

    def source_form(self, term_id: str) -> str:
        """Return the source-text form of *term_id*."""
        return self._entries[term_id]["source"]

    def target_form(self, term_id: str) -> str:
        """Return the preserved form of *term_id*."""
        return self._entries[term_id]["target"]

    def match(self, text: str, selected_ids: Optional[List[str]] = None, limit: Optional[int] = None) -> List[str]:
        """Return the ids of configured terms whose source form occurs in *text*.

        ``selected_ids`` narrows the scan to the caller's selection; ``limit``
        caps how many ids are returned (declaration order decides which).
        """
        lowered = text.lower()
        candidates = selected_ids if selected_ids else self.term_ids
        hits = [tid for tid in candidates if tid in self._entries and self._entries[tid]["source"].lower() in lowered]
        if limit is not None:
            hits = hits[:limit]
        return hits
