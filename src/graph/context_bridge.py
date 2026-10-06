"""AgentCore Platform v1.0"""

# TEL-C2-008 — validated-control bridge between the outer and inner graphs.
#
# Why this exists:
#   The framework's GraphNode.execute() calls subgraph.invoke(user_input, ...)
#   and forwards ONLY the extracted input string — the outer state's structured
#   input_context does not cross the subgraph boundary. Without a bridge the
#   inner workflow sees an empty context and every caller control field
#   (document reference, language selection, accuracy threshold, glossary
#   selection) silently reverts to a default.
#
# What crosses:
#   The outer main node stashes the ALREADY-VALIDATED control record here (the
#   projection produced by the caller-data contract, never raw caller input);
#   the inner graph's _extra_initial_state() pops it into the state key
#   `translation_controls`.
#
# ContextVar semantics: the value is scoped to the current logical context, so
# concurrent invocations do not observe each other's records; pop() clears the
# slot on read, so a record can never leak into a later invocation.

from contextvars import ContextVar
from typing import Any, Dict, Optional

_TRANSLATION_CONTROLS: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "tel_c2_008_translation_controls", default=None
)


def stash_translation_controls(controls: Optional[Dict[str, Any]]) -> None:
    """Store the validated control record for the inner graph to pick up."""
    _TRANSLATION_CONTROLS.set(controls)


def pop_translation_controls() -> Optional[Dict[str, Any]]:
    """Return and clear the stashed record (single-use — never leaks across runs)."""
    value = _TRANSLATION_CONTROLS.get()
    _TRANSLATION_CONTROLS.set(None)
    return value
