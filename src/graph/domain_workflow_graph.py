"""AgentCore Platform v1.0"""

# TEL-C2-008 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph of the Cat 2 two-layer nested architecture. It
# encapsulates the telecom standard / regulation translation workflow:
#
#   START → validate_input → detect_source_language
#         → translate_with_telecom_terms → verify_technical_accuracy
#         → output_validate → END
#
# Called by TelecomTranslationWorkflowNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Configuration:
#   The graph is constructed with the declared `translation` block from
#   config/config.yaml. _validate_config() type-checks every declared value and
#   raises ConfigError on a malformed declaration, so a bad file fails the
#   invocation instead of degrading silently to defaults. register_nodes()
#   injects the validated values into the domain nodes through their
#   constructors — the node execute() contract takes no configuration argument.
#
# Caller controls:
#   The framework forwards only the input string across a subgraph boundary, so
#   the validated control record crosses via the context bridge and is seeded
#   into the inner state by _extra_initial_state().
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph abstract methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with TelecomTranslationWorkflowNode.merge_output()
#   ❌ No platform-internal SDK imports

from typing import Any, Dict, List

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import pop_translation_controls
from src.nodes.detect_source_language_node import DetectSourceLanguageNode
from src.nodes.output_validate_node import OutputValidateNode
from src.nodes.translate_with_telecom_terms_node import TranslateWithTelecomTermsNode
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.verify_technical_accuracy_node import VerifyTechnicalAccuracyNode
from src.schemas.state import State
from src.services.service import GlossaryError, TelecomGlossary

# Declared-value bounds. A configuration file is trusted less than code but more
# than a caller: it may choose a value, not an impossible one.
_MIN_JA_RATIO = 0.0
_MAX_JA_RATIO = 1.0
_MAX_INPUT_CHARS_CEILING = 200_000
_MAX_GLOSSARY_TERMS_CEILING = 256


class DomainWorkflowGraph(BaseGraph):
    """Inner translation workflow graph for TEL-C2-008.

    Inherits BaseGraph directly for a fully custom node topology. Called by
    TelecomTranslationWorkflowNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → validate_input               (ValidateInputNode)
          → detect_source_language       (DetectSourceLanguageNode)
          → translate_with_telecom_terms (TranslateWithTelecomTermsNode)
          → verify_technical_accuracy    (VerifyTechnicalAccuracyNode)
          → output_validate              (OutputValidateNode — inner boundary)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "TelecomTranslationWorkflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across the inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Type-check every declared runtime value before compilation.

        Raises ConfigError rather than falling back to a default: a declared
        value that is ignored is indistinguishable from a value that was never
        declared, which is how runtime configuration goes quietly dead.
        """
        translation = self.config.get("translation", {}) or {}
        if not isinstance(translation, dict):
            raise ConfigError("translation must be a mapping")

        ratio = translation.get("ja_ratio_threshold", 0.20)
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ConfigError("translation.ja_ratio_threshold must be a number")
        if not _MIN_JA_RATIO <= float(ratio) <= _MAX_JA_RATIO:
            raise ConfigError("translation.ja_ratio_threshold must be between 0.0 and 1.0")

        max_chars = translation.get("max_input_chars", 10_000)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ConfigError("translation.max_input_chars must be an integer")
        if not 1 <= max_chars <= _MAX_INPUT_CHARS_CEILING:
            raise ConfigError(f"translation.max_input_chars must be between 1 and {_MAX_INPUT_CHARS_CEILING}")

        default_min = translation.get("default_min_accuracy", 0.80)
        if isinstance(default_min, bool) or not isinstance(default_min, (int, float)):
            raise ConfigError("translation.default_min_accuracy must be a number")
        if not 0.0 <= float(default_min) <= 1.0:
            raise ConfigError("translation.default_min_accuracy must be between 0.0 and 1.0")

        max_terms = translation.get("max_glossary_terms", 16)
        if isinstance(max_terms, bool) or not isinstance(max_terms, int):
            raise ConfigError("translation.max_glossary_terms must be an integer")
        if not 1 <= max_terms <= _MAX_GLOSSARY_TERMS_CEILING:
            raise ConfigError(f"translation.max_glossary_terms must be between 1 and {_MAX_GLOSSARY_TERMS_CEILING}")

        try:
            TelecomGlossary.from_config(translation.get("glossary"))
        except GlossaryError as exc:
            raise ConfigError(str(exc)) from exc

    # ── Declared values ───────────────────────────────────────────────────────

    @property
    def _translation_config(self) -> Dict[str, Any]:
        block = self.config.get("translation", {}) or {}
        return block if isinstance(block, dict) else {}

    @property
    def glossary(self) -> TelecomGlossary:
        """The configured telecom terminology for this invocation."""
        return TelecomGlossary.from_config(self._translation_config.get("glossary"))

    # ── Caller controls across the subgraph boundary ──────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated control record stashed by the outer main node.

        The framework forwards only the input string into a subgraph, so
        without this the inner pipeline would never see the caller's language
        selection, accuracy threshold or glossary selection.
        """
        controls = pop_translation_controls()
        return {"translation_controls": controls or {}}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register the 5 domain nodes with the declared configuration.

        No super() call — BaseGraph.register_nodes() is abstract. Do NOT
        register initialize or finalize; those are outer backbone concerns
        handled by the outer graph. Every key registered here is referenced in
        add_edges().

        Each node receives the validated declared values through its
        constructor, which is what makes config/config.yaml load-bearing: a
        node that reads nothing at construction time runs on its own hard-coded
        defaults no matter what the file says.
        """
        translation = self._translation_config
        glossary = self.glossary

        self._nodes["validate_input"] = ValidateInputNode(
            max_input_chars=int(translation.get("max_input_chars", 10_000)),
        )
        self._nodes["detect_source_language"] = DetectSourceLanguageNode(
            ja_ratio_threshold=float(translation.get("ja_ratio_threshold", 0.20)),
        )
        self._nodes["translate_with_telecom_terms"] = TranslateWithTelecomTermsNode(
            glossary=glossary,
            max_glossary_terms=int(translation.get("max_glossary_terms", 16)),
        )
        self._nodes["verify_technical_accuracy"] = VerifyTechnicalAccuracyNode(
            glossary=glossary,
            default_min_accuracy=float(translation.get("default_min_accuracy", 0.80)),
        )
        self._nodes["output_validate"] = OutputValidateNode(glossary=glossary)

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear telecom translation topology.

        Each step passes its partial-dict output into the shared State. The
        topology is intentionally linear — no conditional branching between
        domain nodes. route() is implemented as required by the abstract base
        but add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "detect_source_language")
        self._sg.add_edge("detect_source_language", "translate_with_telecom_terms")
        self._sg.add_edge("translate_with_telecom_terms", "verify_technical_accuracy")
        self._sg.add_edge("verify_technical_accuracy", "output_validate")
        self._sg.add_edge("output_validate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by the abstract base.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. It is annotated with this graph's own
        State: LangGraph reads a path callable's annotation as its input schema
        and projects away every field the annotation does not carry, so an
        annotation of the framework base state would make the routing flags
        always-absent if the topology ever gained a conditional edge.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_validate"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by TelecomTranslationWorkflowNode.merge_output()
        in graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency. This method reports the
        inner pipeline faithfully, including on a refusal — deciding what may
        cross into the outer state is merge_output()'s single responsibility.
        """
        flags: List[str] = state.get("accuracy_flags") or []
        return {
            # The reason must leave the subgraph or the outer graph has no way
            # to tell a declined request from a produced-nothing one.
            "error_code": state.get("error_code"),
            "result": state.get("result"),
            "translated_text": state.get("translated_text", ""),
            "translation_report": state.get("translation_report"),
            "glossary_hits": state.get("glossary_hits", []),
            "accuracy_score": state.get("accuracy_score", 0.0),
            "accuracy_flags": flags,
            "review_required": bool(state.get("review_required", False)),
            "out_of_scope": bool(state.get("out_of_scope", False)),
            "oos_reason": state.get("oos_reason"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
