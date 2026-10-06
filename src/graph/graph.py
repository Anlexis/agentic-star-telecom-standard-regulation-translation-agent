"""AgentCore Platform v1.0"""

# TEL-C2-008 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#
#   The `main` slot is a GraphNode subclass (TelecomTranslationWorkflowNode) that
#   delegates the full translation workflow to DomainWorkflowGraph (inner BaseGraph).
#   Domain complexity is fully encapsulated inside the inner graph; the outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (translation pipeline)
#   src/graph/context_bridge.py        ← validated-control bridge (outer → inner)
#
# Configuration:
#   Runtime values come from config/config.yaml. They are taken from the
#   constructor argument when the caller supplies one and loaded from the
#   shipped file otherwise, then injected into every node at registration time.
#   No node carries a hard-coded copy of a declared value.
#
# Rules enforced:
#   ✅ TelecomStandardTranslationAgent inherits AgentBaseGraph (framework base class)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ TelecomTranslationWorkflowNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys, and carries no answer-bearing
#      field forward from a non-success inner result
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-internal SDK imports

import json
from typing import Any, ClassVar, Dict, Optional, TYPE_CHECKING, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.context_bridge import stash_translation_controls
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.runtime_config import load_runtime_config, translation_settings

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

__all__ = [
    "Graph",
    "TelecomRegTranslationAgent",
    "TelecomStandardTranslationAgent",
    "TelecomTranslationWorkflowNode",
    "load_runtime_config",
]


class TelecomTranslationWorkflowNode(GraphNode):
    """GraphNode assigned to the `main` slot of TelecomStandardTranslationAgent.

    Wraps DomainWorkflowGraph (the inner Cat 2 BaseGraph). Invoked by the
    backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate DomainWorkflowGraph with the declared
                        runtime configuration
      extract_input() — stash the validated control record on the context
                        bridge and hand the inner graph the document text
      merge_output()  — map sub_result fields into the outer state delta
                        (changed keys only; no answer-bearing field on a
                        non-success inner result)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail fast).
    error_strategy: ClassVar[str] = "propagate"

    # False: human-review interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    # The caller contract is enforced one slot earlier, at the same trust level.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, runtime_config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self._runtime_config: Dict[str, Any] = runtime_config or {}

    def execute(self, state: AgentState) -> Dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A declined request has no screened document to translate, so running the
        workflow would only reach the first domain node, fail its own
        precondition, and replace the specific, actionable reason already
        settled with a vaguer one.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        return cast(Dict[str, Any], super().execute(state))

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner translation workflow graph.

        Imported lazily (inside the method) to avoid circular-import risk at
        module load time.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Bridge the validated controls to the inner graph; return the document.

        PreProcessNode validates the caller request and writes the screened
        document to validated_input and the projected control record to
        validated_context. The controls cross out-of-band via the context
        bridge because the framework forwards only the input string across a
        subgraph boundary.

        The bridge is written even when the record is absent (upstream error
        paths): pop-on-read semantics keep a stale record from a prior run from
        ever being observed.
        """
        stash_translation_controls(self._controls_from_state(state))
        return state.get("validated_input", "") or ""

    @staticmethod
    def _controls_from_state(state: AgentState) -> Optional[Dict[str, Any]]:
        """Decode the validated control record written by pre_process."""
        raw = state.get("validated_context")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner get_output() keys into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        On a non-success inner result no answer-bearing field is carried
        forward. The outer state must not acquire text the inner boundary
        refused to release: the framework's own get_output() reads
        formatted_output or result with no status check, and anything left in
        the delta is also checkpointed for later readers.
        """
        status = sub_result.get("status")
        # A reason settled OUTSIDE the subgraph (PreProcessNode) is the real
        # one: reading sub_result alone would overwrite it with the inner blank,
        # since the inner graph never ran.
        marker = state.get("error_code") or sub_result.get("error_code")
        if status != AgentStatus.SUCCESS.value:
            return {
                "result": None,
                "translated_text": None,
                "translation_report": None,
                "glossary_hits": [],
                "accuracy_score": None,
                "accuracy_flags": [],
                "review_required": True,
                "out_of_scope": bool(sub_result.get("out_of_scope", False)),
                "oos_reason": None,
                "status": status,
                "error_code": marker,
            }
        return {
            "result": sub_result.get("result"),
            "translated_text": sub_result.get("translated_text"),
            "translation_report": sub_result.get("translation_report"),
            "glossary_hits": sub_result.get("glossary_hits", []),
            "accuracy_score": sub_result.get("accuracy_score", 0.0),
            "accuracy_flags": sub_result.get("accuracy_flags", []),
            "review_required": bool(sub_result.get("review_required", False)),
            "out_of_scope": bool(sub_result.get("out_of_scope", False)),
            "oos_reason": sub_result.get("oos_reason"),
            "status": status,
            "error_code": marker,
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the declared runtime configuration to the inner graph.

        Only keys the configuration actually declares are forwarded; the inner
        graph's _validate_config() then type-checks every declared value, so a
        malformed declaration fails the invocation instead of degrading
        silently to defaults.
        """
        cfg = self._runtime_config
        forwarded: Dict[str, Any] = {"translation": translation_settings(cfg)}
        for key in ("timeout_s", "max_retry"):
            if cfg.get(key) is not None:
                forwarded[key] = cfg[key]
        return forwarded


class TelecomStandardTranslationAgent(AgentBaseGraph):
    """Outer graph for TEL-C2-008 (Cat 2).

    Inherits AgentBaseGraph (the framework base class) directly. Domain logic
    is fully encapsulated in TelecomTranslationWorkflowNode (main slot), which
    delegates to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode  (VERIFIED_EXTERNAL — caller trust gate)
      - main:         TelecomTranslationWorkflowNode (delegates to the inner graph)
      - post_process: PostProcessNode (output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "TelecomStandardTranslationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def effective_config(self) -> Dict[str, Any]:
        """Return the runtime configuration governing this instance.

        The constructor argument wins when it declares the runtime block;
        otherwise the shipped config/config.yaml is loaded. There is no
        hard-coded fallback: a missing or malformed file raises rather than
        letting the agent run with every declared value ignored.
        """
        cfg = self.config or {}
        if isinstance(cfg, dict) and "translation" in cfg:
            return cfg
        loaded = load_runtime_config()
        loaded.update({k: v for k, v in cfg.items() if k != "translation"} if isinstance(cfg, dict) else {})
        return loaded

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots, injecting the declared configuration.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize and finalize nodes.
        """
        super().register_nodes()  # fills: initialize, finalize

        runtime = self.effective_config()
        settings = translation_settings(runtime)

        self._nodes["pre_process"] = PreProcessNode(settings=settings)
        self._nodes["main"] = TelecomTranslationWorkflowNode(runtime_config=runtime)
        self._nodes["post_process"] = PostProcessNode(settings=settings)

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat aliases — callers (and config/agent.yaml `class:`) may reference
# either name. config/agent.yaml points at "TelecomRegTranslationAgent".
TelecomRegTranslationAgent = TelecomStandardTranslationAgent
Graph = TelecomStandardTranslationAgent
