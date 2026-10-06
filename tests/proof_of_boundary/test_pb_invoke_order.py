# PB-6: Invoke execution order and trust-gate denial.
#
# Verifies BaseNode.__call__() enforces the authorised path — trust gate ->
# node_start audit -> input gate -> execute() -> output gate -> node_complete
# audit — for every concrete node under src/nodes/, and that an
# under-privileged caller is denied before execute() or the normal lifecycle
# events can run.
#
# Also verifies the full backbone invoke order for the outer agent (the Cat 2
# two-layer nested graph):
#   InitializeNode -> PreProcessNode (pre_process)
#   -> TelecomTranslationWorkflowNode (main) -> PostProcessNode (post_process)
#   -> FinalizeNode
#
# The backbone invoke uses VERIFIED_EXTERNAL caller trust — the real external
# path a deployed caller takes. for_internal() is deliberately not used: it
# would represent a caller no external client can be.

import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import ClassVar

import pytest

from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "TelecomTranslationWorkflowNode"

# A SUCCESS-yielding document for the backbone invoke test: English source text
# carrying several configured glossary terms, so the run exercises detection,
# terminology preservation, scoring and the output boundary.
#
# CONTRACT: deploy/invoke_payload.json["input"] MUST equal this exact string —
# the deployment smoke invoke and this test must exercise the identical payload.
# test_invoke_payload_matches_pb6 below asserts that equality so the two can
# never drift.
_VALID_PAYLOAD = (
    "The 3GPP specification defines handover procedures for the 5G NR base station "
    "operating in the licensed frequency band assigned under the regulatory domain."
)

# ─────────────────────────────────────────────────────────────────────────────


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the negative trust boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """__call__ must run trust gate -> node_start -> input gate -> execute()
    -> output gate -> node_complete."""

    def test_trust_denial_refuses_execution_before_execute(self, monkeypatch):
        """An always-present privileged node proves the negative trust path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(_PrivilegedTrustGateFixture.required_trust_level).value,
                "correlation_id": "pb6-trust-denial",
            }
        )

        assert result["status"] == "error"
        assert "trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                # caller trust == the node's required level, so the trust gate always
                # passes here; the denial branch is asserted separately above.
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\nexpected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestTrustGate:
    """The trust gate runs BEFORE execute() and denies a caller below the level
    the node requires."""

    def test_pre_process_denies_an_anonymous_caller(self):
        from framework.schemas.agent_status import AgentStatus

        from src.nodes.pre_process_node import PreProcessNode

        result = PreProcessNode()(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "user_input": _VALID_PAYLOAD,
                "correlation_id": "pb6-trust-denial",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any("trust gate" in e.lower() for e in result.get("error_log", []))

    def test_pre_process_admits_a_verified_external_caller(self):
        from framework.schemas.agent_status import AgentStatus

        from src.graph.graph import TelecomStandardTranslationAgent
        from src.services.runtime_config import translation_settings
        from src.nodes.pre_process_node import PreProcessNode

        settings = translation_settings(TelecomStandardTranslationAgent().effective_config())
        result = PreProcessNode(settings=settings)(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "user_input": _VALID_PAYLOAD,
                "input_context": {},
                "correlation_id": "pb6-trust-admit",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"]


class TestBackboneInvokeOrder:
    """A full Graph().invoke() runs the 5-node backbone in order and returns a
    real answer at the trust level the manifest declares."""

    def _invoke(self):
        from framework.schemas.invocation_context import InvocationContext

        from src.graph.graph import Graph, load_runtime_config

        agent = Graph(config=load_runtime_config())
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_VALID_PAYLOAD, ctx=ctx)

    def test_backbone_invoke_succeeds_and_returns_output(self):
        from framework.schemas.agent_status import AgentStatus

        result = self._invoke()
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"expected status={AgentStatus.SUCCESS.value!r}, got {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output"), "output must be set after a successful invoke"
        assert "Telecom Standard Translation Report" in result["output"]
        assert "基地局" in result["output"]

    def test_backbone_node_history_matches_the_expected_order(self):
        history = self._invoke().get("node_history", [])
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "TelecomTranslationWorkflowNode",
            "PostProcessNode",
            "FinalizeNode",
        ], f"unexpected backbone node_history: {history}"

    def test_main_slot_is_the_workflow_graph_node(self):
        from framework.nodes.graph_node import GraphNode

        from src.graph.graph import TelecomStandardTranslationAgent, TelecomTranslationWorkflowNode

        agent = TelecomStandardTranslationAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert isinstance(main_node, TelecomTranslationWorkflowNode)
        assert isinstance(main_node, GraphNode), "the main slot must subclass GraphNode"
        assert main_node.__class__.__name__ == _MAIN_SLOT_NODE

    def test_invoke_payload_matches_pb6(self):
        """deploy/invoke_payload.json["input"] MUST equal _VALID_PAYLOAD.

        The deployment smoke invoke posts that file as the request body, so it
        has to exercise the same payload this file asserts yields SUCCESS.
        """
        repo_root = Path(__file__).resolve().parents[2]
        payload_file = repo_root / "deploy" / "invoke_payload.json"
        assert payload_file.exists(), "deploy/invoke_payload.json is required for deployment"
        body = json.loads(payload_file.read_text(encoding="utf-8"))
        assert body.get("input") == _VALID_PAYLOAD
