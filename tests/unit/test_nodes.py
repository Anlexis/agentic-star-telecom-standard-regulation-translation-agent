"""Unit tests for the TEL-C2-008 nodes and graph wiring.

Node-level behaviour only. Anything that has to be true of the DEPLOYED agent —
the trust gate, the caller-data contract end to end, the output boundary — is
proven through the real ASGI entry point in tests/proof_of_boundary/, because a
suite that only ever calls execute() bypasses the wrapper where those gates live.
"""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.detect_source_language_node import DetectSourceLanguageNode
from src.nodes.output_validate_node import OutputValidateNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.translate_with_telecom_terms_node import TranslateWithTelecomTermsNode
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.verify_technical_accuracy_node import VerifyTechnicalAccuracyNode
from src.services.service import GlossaryError, TelecomGlossary

GLOSSARY = TelecomGlossary.from_config(
    {
        "3gpp": {"source": "3GPP", "target": "3GPP"},
        "base_station": {"source": "base station", "target": "基地局"},
        "handover": {"source": "handover", "target": "ハンドオーバ"},
    }
)


# ── Glossary service ──────────────────────────────────────────────────────────


class TestTelecomGlossary:
    def test_from_config_builds_entries(self):
        assert GLOSSARY.term_ids == ["3gpp", "base_station", "handover"]
        assert GLOSSARY.source_form("base_station") == "base station"
        assert GLOSSARY.target_form("base_station") == "基地局"

    def test_empty_declaration_is_empty_glossary(self):
        assert TelecomGlossary.from_config(None).is_empty()

    @pytest.mark.parametrize(
        "declared",
        [
            "not-a-mapping",
            {"x": "not-a-mapping"},
            {"x": {"source": "", "target": "y"}},
            {"x": {"source": "y"}},
        ],
    )
    def test_malformed_declaration_raises(self, declared):
        with pytest.raises(GlossaryError):
            TelecomGlossary.from_config(declared)

    def test_match_respects_selection_and_limit(self):
        text = "3GPP defines handover for the base station."
        assert GLOSSARY.match(text) == ["3gpp", "base_station", "handover"]
        assert GLOSSARY.match(text, selected_ids=["handover"]) == ["handover"]
        assert GLOSSARY.match(text, limit=2) == ["3gpp", "base_station"]


# ── Trust levels (the contract the manifest declares) ─────────────────────────


class TestDeclaredTrustLevels:
    """Every node the request path traverses must be reachable by the caller.

    The manifest declares VERIFIED_EXTERNAL. A node requiring INTERNAL sits on
    the same path but no external caller can ever hold that level, so the trust
    gate denies before execute() runs and the agent cannot serve a request.
    """

    @pytest.mark.parametrize(
        "node_cls",
        [
            PreProcessNode,
            ValidateInputNode,
            DetectSourceLanguageNode,
            TranslateWithTelecomTermsNode,
            VerifyTechnicalAccuracyNode,
            OutputValidateNode,
            PostProcessNode,
        ],
    )
    def test_declared_explicitly_and_reachable(self, node_cls):
        declared = node_cls.__dict__.get("required_trust_level")
        assert declared is not None, f"{node_cls.__name__} must declare required_trust_level in its own body"
        assert declared in (TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL)

    def test_output_boundary_is_not_trust_gated(self):
        """The output boundary must run on every path, including denials."""
        assert PostProcessNode.required_trust_level is TrustLevel.ANONYMOUS


# ── ValidateInputNode ─────────────────────────────────────────────────────────


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode(max_input_chars=100)

    def test_valid_input_normalises_controls(self):
        result = self.node.execute(
            {
                "user_input": "  3GPP standard text  ",
                "translation_controls": {
                    "document_ref": "doc_1",
                    "target_language": "ja",
                    "min_accuracy": 0.5,
                    "max_terms": 3,
                    "glossary_ids": ["3gpp"],
                },
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_text"] == "3GPP standard text"
        assert result["document_ref"] == "doc_1"
        assert result["target_language_request"] == "ja"
        assert result["min_accuracy"] == 0.5
        assert result["max_terms"] == 3
        assert result["selected_terms"] == ["3gpp"]

    def test_empty_input_refused(self):
        result = self.node.execute({"user_input": "   "})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert "input" in result["validation_error"]

    def test_non_string_input_refused(self):
        result = self.node.execute({"user_input": 12345})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")

    def test_oversized_input_uses_the_declared_limit(self):
        result = self.node.execute({"user_input": "a" * 101})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert "100-character limit" in result["validation_error"]

    def test_error_never_echoes_the_rejected_value(self):
        secret = "ZZ-Secret-Value-9911"
        result = self.node.execute({"user_input": "ok", "translation_controls": {"document_ref": secret}})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert secret not in result["validation_error"]
        assert secret not in " ".join(result["error_log"])


# ── DetectSourceLanguageNode ──────────────────────────────────────────────────


class TestDetectSourceLanguageNode:
    def test_english_input(self):
        node = DetectSourceLanguageNode(ja_ratio_threshold=0.20)
        result = node.execute({"validated_text": "The base station supports handover."})
        assert result["detected_language"] == "en"
        assert result["target_language"] == "ja"
        assert result["out_of_scope"] is False

    def test_japanese_input(self):
        node = DetectSourceLanguageNode(ja_ratio_threshold=0.20)
        result = node.execute({"validated_text": "基地局のハンドオーバ手順を定義する。"})
        assert result["detected_language"] == "ja"
        assert result["target_language"] == "en"

    def test_declared_threshold_changes_the_classification(self):
        mixed = "The 基地局 supports handover procedures across the network."
        low = DetectSourceLanguageNode(ja_ratio_threshold=0.01).execute({"validated_text": mixed})
        high = DetectSourceLanguageNode(ja_ratio_threshold=0.99).execute({"validated_text": mixed})
        assert low["detected_language"] == "ja"
        assert high["detected_language"] == "en"

    def test_unsupported_script_is_out_of_scope_not_error(self):
        node = DetectSourceLanguageNode()
        result = node.execute({"validated_text": "이것은 한국어 문서입니다."})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["out_of_scope"] is True
        assert result["oos_reason"]

    def test_requested_target_equal_to_source_is_out_of_scope(self):
        node = DetectSourceLanguageNode()
        result = node.execute({"validated_text": "The base station.", "target_language_request": "en"})
        assert result["out_of_scope"] is True
        assert result["status"] == AgentStatus.SUCCESS.value


# ── TranslateWithTelecomTermsNode ─────────────────────────────────────────────


class TestTranslateWithTelecomTermsNode:
    def setup_method(self):
        self.node = TranslateWithTelecomTermsNode(glossary=GLOSSARY, max_glossary_terms=16)

    def test_out_of_scope_skips_translation(self):
        result = self.node.execute({"out_of_scope": True, "validated_text": "x"})
        assert result["translated_text"] == ""
        assert result["glossary_hits"] == []
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_configured_terms_are_matched_and_carried_over(self):
        result = self.node.execute(
            {
                "validated_text": "The base station follows 3GPP handover rules.",
                "detected_language": "en",
                "target_language": "ja",
                "out_of_scope": False,
            }
        )
        assert result["glossary_hits"] == ["3gpp", "base_station", "handover"]
        assert "基地局" in result["translated_text"]
        assert "ハンドオーバ" in result["translated_text"]

    def test_matching_is_case_insensitive(self):
        result = self.node.execute(
            {"validated_text": "BASE STATION handover", "out_of_scope": False},
        )
        assert "base_station" in result["glossary_hits"]

    def test_caller_selection_narrows_the_glossary(self):
        result = self.node.execute(
            {
                "validated_text": "The base station follows 3GPP handover rules.",
                "selected_terms": ["handover"],
                "out_of_scope": False,
            }
        )
        assert result["glossary_hits"] == ["handover"]
        assert "基地局" not in result["translated_text"]

    def test_caller_cap_limits_the_terms_applied(self):
        result = self.node.execute(
            {
                "validated_text": "The base station follows 3GPP handover rules.",
                "max_terms": 1,
                "out_of_scope": False,
            }
        )
        assert result["glossary_hits"] == ["3gpp"]

    def test_no_configured_terms_means_no_hits(self):
        node = TranslateWithTelecomTermsNode(glossary=TelecomGlossary({}))
        result = node.execute({"validated_text": "The base station.", "out_of_scope": False})
        assert result["glossary_hits"] == []
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_output_depends_on_the_input(self):
        first = self.node.execute({"validated_text": "handover A", "out_of_scope": False})
        second = self.node.execute({"validated_text": "handover B", "out_of_scope": False})
        assert first["translated_text"] != second["translated_text"]
        assert "A" in first["translated_text"] and "B" in second["translated_text"]


# ── VerifyTechnicalAccuracyNode ───────────────────────────────────────────────


class TestVerifyTechnicalAccuracyNode:
    def setup_method(self):
        self.node = VerifyTechnicalAccuracyNode(glossary=GLOSSARY, default_min_accuracy=0.80)

    def test_out_of_scope_skips_the_check(self):
        result = self.node.execute({"out_of_scope": True})
        assert result["accuracy_score"] == 1.0
        assert result["review_required"] is False

    def test_all_terms_preserved(self):
        result = self.node.execute(
            {
                "out_of_scope": False,
                "validated_text": "base station handover",
                "translated_text": "基地局 ハンドオーバ",
                "glossary_hits": ["base_station", "handover"],
            }
        )
        assert result["accuracy_score"] == 1.0
        assert result["accuracy_flags"] == []
        assert result["review_required"] is False

    def test_dropped_term_flags_and_scores_partially(self):
        result = self.node.execute(
            {
                "out_of_scope": False,
                "validated_text": "base station handover",
                "translated_text": "基地局 only",
                "glossary_hits": ["base_station"],
            }
        )
        assert result["accuracy_flags"] == ["handover"]
        assert result["accuracy_score"] == pytest.approx(0.5)
        assert result["review_required"] is True

    def test_source_without_terms_scores_one(self):
        result = self.node.execute(
            {"out_of_scope": False, "validated_text": "Staff picnic schedule.", "translated_text": "x"}
        )
        assert result["accuracy_score"] == 1.0
        assert result["review_required"] is False

    def test_caller_threshold_decides_review(self):
        state = {
            "out_of_scope": False,
            "validated_text": "base station handover",
            "translated_text": "基地局 only",
            "glossary_hits": ["base_station"],
        }
        lenient = self.node.execute({**state, "min_accuracy": 0.4})
        strict = self.node.execute({**state, "min_accuracy": 0.9})
        assert lenient["review_required"] is False
        assert strict["review_required"] is True

    def test_declared_default_threshold_is_used_when_the_caller_omits_one(self):
        state = {
            "out_of_scope": False,
            "validated_text": "base station handover",
            "translated_text": "基地局 only",
            "glossary_hits": ["base_station"],
        }
        lenient = VerifyTechnicalAccuracyNode(glossary=GLOSSARY, default_min_accuracy=0.1).execute(state)
        strict = VerifyTechnicalAccuracyNode(glossary=GLOSSARY, default_min_accuracy=0.9).execute(state)
        assert lenient["review_required"] is False
        assert strict["review_required"] is True


# ── OutputValidateNode (inner boundary) ───────────────────────────────────────


class TestOutputValidateNode:
    def setup_method(self):
        self.node = OutputValidateNode(glossary=GLOSSARY)

    def test_out_of_scope_response(self):
        result = self.node.execute({"out_of_scope": True, "oos_reason": "unsupported script"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Outcome: not translated" in result["result"]
        assert "unsupported script" in result["result"]

    def test_report_renders_the_configured_target_forms(self):
        result = self.node.execute(
            {
                "out_of_scope": False,
                "translated_text": "[en->ja] 基地局",
                "glossary_hits": ["base_station"],
                "accuracy_score": 1.0,
                "accuracy_flags": [],
                "review_required": False,
                "document_ref": "doc_9",
                "detected_language": "en",
                "target_language": "ja",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Document reference: doc_9" in result["result"]
        assert "Terminology preserved: 基地局" in result["result"]
        assert "[en->ja] 基地局" in result["result"]
        assert result["translation_report"] == result["result"]

    def test_empty_translation_is_refused_with_nothing_carried(self):
        result = self.node.execute({"out_of_scope": False, "translated_text": "  "})
        assert result["status"] == AgentStatus.ERROR.value
        assert "result" in result and result["result"] is None
        assert "translation_report" in result and result["translation_report"] is None
        assert result["error_log"] == ["OutputValidateNode: refused (empty_translation)"]

    def test_domain_output_hook_returns_the_result(self):
        """The framework assigns the hook's return value back over the delta.

        A hook that returns None erases the node's whole delta and the node
        then fails on every invocation, so returning the dict is the contract —
        not an implementation detail.
        """
        delta = {"status": AgentStatus.SUCCESS.value, "result": "ok"}
        assert self.node._extra_security_gate_output(delta) == delta

    def test_domain_output_hook_rejects_a_non_success_delta_carrying_answers(self):
        with pytest.raises(ValueError):
            self.node._extra_security_gate_output({"status": AgentStatus.ERROR.value, "result": "leaked"})

    def test_final_gate_is_not_overridden(self):
        assert "_security_gate_output" not in OutputValidateNode.__dict__


# ── Graph wiring ──────────────────────────────────────────────────────────────


class TestGraphWiring:
    def test_outer_agent_registers_the_backbone_slots(self):
        from src.graph.graph import TelecomStandardTranslationAgent, TelecomTranslationWorkflowNode

        agent = TelecomStandardTranslationAgent()
        agent.register_nodes()
        assert isinstance(agent._nodes["main"], TelecomTranslationWorkflowNode)
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)
        assert agent._nodes["initialize"] is not None
        assert agent._nodes["finalize"] is not None

    def test_outer_graph_does_not_override_add_edges(self):
        from framework.graph.agent_base_graph import AgentBaseGraph

        from src.graph.graph import TelecomStandardTranslationAgent

        assert "add_edges" not in TelecomStandardTranslationAgent.__dict__
        assert TelecomStandardTranslationAgent.add_edges is AgentBaseGraph.add_edges

    def test_back_compat_alias_resolves(self):
        from src.graph.graph import Graph, TelecomRegTranslationAgent, TelecomStandardTranslationAgent

        assert TelecomRegTranslationAgent is TelecomStandardTranslationAgent
        assert Graph is TelecomStandardTranslationAgent

    def test_merge_output_returns_only_changed_keys(self):
        from src.graph.graph import TelecomTranslationWorkflowNode

        node = TelecomTranslationWorkflowNode()
        delta = node.merge_output(
            {},
            {
                "status": AgentStatus.SUCCESS.value,
                "result": "report",
                "translated_text": "t",
                "translation_report": "report",
                "glossary_hits": ["3gpp"],
                "accuracy_score": 1.0,
                "accuracy_flags": [],
                "review_required": False,
                "out_of_scope": False,
                "oos_reason": None,
                "node_history": ["should not travel"],
                "trace_id": "should not travel",
            },
        )
        assert "node_history" not in delta
        assert "trace_id" not in delta
        assert delta["result"] == "report"

    def test_merge_output_carries_nothing_on_a_non_success_inner_result(self):
        from src.graph.graph import TelecomTranslationWorkflowNode

        node = TelecomTranslationWorkflowNode()
        delta = node.merge_output(
            {},
            {
                "status": AgentStatus.ERROR.value,
                "result": "un-gated report",
                "translated_text": "un-gated translation",
                "translation_report": "un-gated report",
                "glossary_hits": ["3gpp"],
                "accuracy_flags": ["handover"],
                "accuracy_score": 0.5,
                "oos_reason": "leaky",
            },
        )
        for key in ("result", "translated_text", "translation_report", "accuracy_score", "oos_reason"):
            assert key in delta, f"{key} must be present in the delta, not merely omitted"
            assert delta[key] is None
        assert delta["glossary_hits"] == []
        assert delta["accuracy_flags"] == []

    def test_inner_graph_registers_the_pipeline_in_order(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        graph = DomainWorkflowGraph(config={"translation": {}})
        graph.register_nodes()
        assert list(graph._nodes) == [
            "validate_input",
            "detect_source_language",
            "translate_with_telecom_terms",
            "verify_technical_accuracy",
            "output_validate",
        ]
        assert "initialize" not in graph._nodes
        assert "finalize" not in graph._nodes

    def test_route_is_annotated_with_this_graphs_own_state(self):
        """LangGraph reads a path callable's annotation as its input schema.

        Annotating with the framework base state projects away every
        template-specific field, so a conditional branch keyed on one would
        never be taken while the unit suite stayed green.
        """
        import typing

        from src.graph.domain_workflow_graph import DomainWorkflowGraph
        from src.schemas.state import State

        hints = typing.get_type_hints(DomainWorkflowGraph.route)
        assert hints["state"] is State
