"""Output containment: a withheld report leaves nothing behind.

Why this is not obvious: the framework resolves the caller-visible answer as
`formatted_output or result`, with NO status check. An output boundary that
returns an error status while leaving `result` in place therefore ships the
un-gated report inside the error envelope, and a FALSY formatted_output makes it
worse — it activates the very fallback it looks like it suppresses.

Three channels are checked here, not one:
  1. the envelope the caller receives;
  2. the state left behind in the delta (LangGraph merges partial deltas, so a
     key that is merely OMITTED keeps its old value — the assertions below
     require PRESENCE AND emptiness);
  3. error_log, which must carry closed-set labels only.

The fault is injected on the DATA path: a document that is legal at intake but
renders a report larger than the filing channel accepts. Nothing patches the
gate — patching the gate would test the patch, not the repository.
"""

import json
import warnings
from uuid import uuid4

import pytest

from framework.nodes.defaults.initialize_node import CURRENT_SCHEMA_VERSION
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.nodes.post_process_node import _ANSWER_BEARING_KEYS, PostProcessNode
from src.services.runtime_config import load_runtime_config, translation_settings

pytest.importorskip("fastapi")

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", message=".*httpx.*", category=UserWarning)
    from fastapi.testclient import TestClient  # noqa: E402

_TOKEN = "pb-contain-token-0001"

# Legal at intake (under translation.max_input_chars) and larger than
# translation.max_report_chars once rendered.
OVERSIZED_DOC = "3GPP handover " * 700

CLEAN_DOC = "The 3GPP handover procedure for the base station."


@pytest.fixture(scope="module")
def client():
    import os

    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    from src.api import server

    with TestClient(server.app) as tc:
        yield tc
    os.environ.pop("INVOKE_AUTH_TOKEN", None)


def _auth():
    return {"Authorization": f"Bearer {_TOKEN}"}


def _final_state(text):
    """Drive the compiled graph and return the FULL final state.

    The envelope hides everything the graph left in state; a containment claim
    that only inspects the envelope cannot see channel 2.
    """
    agent = Graph(config=load_runtime_config())
    agent.compile()
    ctx = InvocationContext(session_id="pb-contain", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    initial = {
        "user_input": text,
        "input_context": {},
        "session_id": ctx.session_id,
        "correlation_id": ctx.correlation_id,
        "trace_id": str(uuid4()),
        "thread_id": ctx.thread_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "caller_trust_level": ctx.caller_trust_level.value,
        "caller_id": "",
        "hitl_allowed": ctx.hitl_allowed,
        "message_id": ctx.message_id,
        "request_source": ctx.request_source,
        "status": AgentStatus.PENDING.value,
        "retry_count": 0,
        "hitl_count": 0,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    return agent._compiled.invoke(initial)


class TestTheFaultIsReachable:
    def test_the_oversized_document_is_accepted_at_intake(self):
        settings = translation_settings(load_runtime_config())
        assert len(OVERSIZED_DOC) <= settings["max_input_chars"]
        assert len(OVERSIZED_DOC) > settings["max_report_chars"]


class TestChannelOneTheEnvelope:
    def test_the_report_is_withheld_with_a_truthy_notice(self, client):
        body = client.post("/invoke", json={"input": OVERSIZED_DOC}, headers=_auth()).json()
        assert body["status"] == "error"
        assert body["output"], "a falsy notice would activate the `or result` fallback"
        assert "withheld" in body["output"]

    def test_no_released_text_reaches_the_caller(self, client):
        body = client.post("/invoke", json={"input": OVERSIZED_DOC}, headers=_auth()).json()
        rendered = json.dumps(body, ensure_ascii=False)
        assert "Telecom Standard Translation Report" not in rendered
        assert "ハンドオーバ" not in rendered
        assert "Translation:" not in rendered

    def test_no_traceback_or_source_path_reaches_the_caller(self, client):
        body = client.post("/invoke", json={"input": OVERSIZED_DOC}, headers=_auth()).json()
        rendered = json.dumps(body)
        for marker in ("Traceback", "/src/", ".py", 'File \\"'):
            assert marker not in rendered

    def test_the_block_happened_at_the_output_boundary(self, client):
        body = client.post("/invoke", json={"input": OVERSIZED_DOC}, headers=_auth()).json()
        assert "PostProcessNode" in body["node_history"]


class TestChannelTwoTheDelta:
    """LangGraph merges partial deltas: an OMITTED key keeps its old value, so
    `not state.get(k)` passes on a gate that clears nothing. Presence AND
    emptiness are asserted."""

    def test_every_answer_bearing_field_is_present_and_empty(self):
        final = _final_state(OVERSIZED_DOC)
        for key in _ANSWER_BEARING_KEYS:
            assert key in final, f"{key} must be written back, not merely omitted"
            assert not final[key], f"{key} still carries content after the refusal"

    def test_inert_provenance_is_pinned_not_carried(self):
        final = _final_state(OVERSIZED_DOC)
        assert final["review_required"] is True
        assert final["out_of_scope"] is False

    def test_the_inventory_covers_every_answer_bearing_state_key(self):
        """A field added to the state later must join the cleared set."""
        from src.schemas.state import State

        answer_like = {
            "result",
            "translation_report",
            "translated_text",
            "glossary_hits",
            "accuracy_flags",
            "accuracy_score",
            "oos_reason",
        }
        assert set(_ANSWER_BEARING_KEYS) == answer_like
        assert answer_like <= set(State.__annotations__) | {"result"}


class TestChannelThreeTheErrorLog:
    def test_entries_carry_closed_set_labels_only(self):
        final = _final_state(OVERSIZED_DOC)
        entries = final.get("error_log", [])
        assert entries
        for entry in entries:
            assert entry.startswith("PostProcessNode: output withheld (")
            assert "Traceback" not in entry
            assert "3GPP handover" not in entry

    def test_error_log_does_not_reach_the_caller_envelope(self, client):
        body = client.post("/invoke", json={"input": OVERSIZED_DOC}, headers=_auth()).json()
        assert "error_log" not in body


class TestCleanPathControl:
    """A refuse-everything boundary must not be able to pass this file."""

    def test_the_same_request_shape_still_produces_a_real_answer(self, client):
        body = client.post("/invoke", json={"input": CLEAN_DOC}, headers=_auth()).json()
        assert body["status"] == "success"
        assert "Translation:" in body["output"]
        assert "ハンドオーバ" in body["output"]
        assert "PostProcessNode" in body["node_history"]


class TestBoundaryUnitLevel:
    """Layers the data path cannot reach on its own.

    The framework's own output gate scans every value a node returns, so a
    credential-bearing document fails inside the first node that returns it —
    which is why the caller boundary refuses such a document outright and why
    this layer is falsifiable only against a hand-built state.
    """

    def _node(self):
        return PostProcessNode(settings=translation_settings(load_runtime_config()))

    def _state(self, report):
        return {"status": AgentStatus.SUCCESS.value, "translation_report": report, "result": report}

    def test_a_credential_in_the_report_is_withheld(self):
        delta = self._node().execute(self._state("Report body sk_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX end"))
        assert delta["status"] == AgentStatus.ERROR.value
        assert delta["formatted_output"]
        assert "sk_live_" not in json.dumps(delta)
        assert delta["error_log"] == ["PostProcessNode: output withheld (credential_shape)"]

    def test_a_control_token_in_the_report_is_withheld(self):
        delta = self._node().execute(self._state("Report body <|im_start|> end"))
        assert delta["status"] == AgentStatus.ERROR.value
        assert delta["error_log"] == ["PostProcessNode: output withheld (control_token)"]

    def test_the_notice_is_not_rebuilt_from_the_cleared_state(self):
        """A replacement assembled from the state just cleared is containment in
        form only — it satisfies the truthiness guard while still disclosing."""
        marker = "SECRET-REFERENCE-9911"
        delta = self._node().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "translation_report": f"Report {marker} sk_live_" + f"ABCDEFGHIJKLMNOPQRSTUVWX",
                "result": marker,
                "document_ref": marker,
                "glossary_hits": [marker],
                "accuracy_flags": [marker],
                "oos_reason": marker,
            }
        )
        assert marker not in json.dumps(delta)

    def test_a_clean_report_is_released_unchanged(self):
        report = "Telecom Standard Translation Report\nTranslation:\nok"
        delta = self._node().execute(self._state(report))
        assert delta["status"] == AgentStatus.SUCCESS.value
        assert delta["formatted_output"] == report
        assert delta["result"] == report

    def test_the_gate_uses_the_frameworks_own_detector(self):
        """A local pattern set narrower than the framework's is a bypass: the
        framework would raise after this node returned, and the wrapper would
        discard the clearing along with the rest of the delta."""
        import inspect

        from framework.security import credential_detector

        source = inspect.getsource(PostProcessNode._violation)
        assert "detect_credentials(" in source
        assert hasattr(credential_detector, "detect_credentials")
