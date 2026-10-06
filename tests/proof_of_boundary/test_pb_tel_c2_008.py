# TEL-C2-008 — Proof-of-Boundary: the domain boundary scenarios
#
# PB-TEL-C2-008-01  Domain output hook : OutputValidateNode implements
#                   _extra_security_gate_output with the framework's contract
#                   (takes the result dict, RETURNS it) and does not override
#                   the @final framework gate. The framework assigns the hook's
#                   return value back over the node's delta, so a hook that
#                   returns None erases the delta.
#
# PB-TEL-C2-008-02  Terminology preservation : a telecom-term-rich source runs
#                   through the pipeline with the configured terms carried into
#                   the translation in their configured target forms.
#
# PB-TEL-C2-008-03  Out of scope is SUCCESS : an unsupported script returns
#                   SUCCESS with out_of_scope=True and an explanation — the agent
#                   behaved correctly, it simply cannot translate this pair.
#
# PB-TEL-C2-008-04  Audit free function : every boundary node calls
#                   emit_trace_event as a free function, never as a bound method
#                   on self, verified by inspecting the source.
#
# PB-TEL-C2-008-05  Trust reachability : no node on the request path requires a
#                   trust level a declared caller cannot hold.
#
# These are node-level and source-level proofs. The proofs about the DEPLOYED
# agent live in test_pb_e2e_invoke.py and test_pb_output_containment.py, which
# drive the real ASGI entry point.

import ast
import inspect
import textwrap

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
from src.services.service import TelecomGlossary

GLOSSARY = TelecomGlossary.from_config(
    {
        "3gpp": {"source": "3GPP", "target": "3GPP"},
        "base_station": {"source": "base station", "target": "基地局"},
        "frequency_band": {"source": "frequency band", "target": "周波数帯"},
    }
)

_BOUNDARY_NODES = [
    PreProcessNode,
    ValidateInputNode,
    DetectSourceLanguageNode,
    TranslateWithTelecomTermsNode,
    VerifyTechnicalAccuracyNode,
    OutputValidateNode,
    PostProcessNode,
]


class TestPB01DomainOutputHook:
    def test_the_final_framework_gate_is_not_overridden(self):
        assert "_security_gate_output" not in OutputValidateNode.__dict__
        assert "_security_gate_input" not in OutputValidateNode.__dict__

    def test_the_hook_returns_the_result_dict(self):
        node = OutputValidateNode(glossary=GLOSSARY)
        delta = {"status": AgentStatus.SUCCESS.value, "result": "clean report"}
        returned = node._extra_security_gate_output(delta)
        assert returned is not None, "returning None erases the node's delta"
        assert returned == delta

    def test_the_hook_refuses_a_non_success_delta_carrying_answer_text(self):
        node = OutputValidateNode(glossary=GLOSSARY)
        with pytest.raises(ValueError):
            node._extra_security_gate_output({"status": AgentStatus.ERROR.value, "translation_report": "leaked"})


class TestPB02TerminologyPreservation:
    def test_configured_terms_survive_the_translation(self):
        node = TranslateWithTelecomTermsNode(glossary=GLOSSARY)
        result = node.execute(
            {
                "validated_text": "The base station frequency band allocation follows 3GPP standards.",
                "detected_language": "en",
                "target_language": "ja",
                "out_of_scope": False,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["glossary_hits"] == ["3gpp", "base_station", "frequency_band"]
        assert "基地局" in result["translated_text"]
        assert "周波数帯" in result["translated_text"]

    def test_the_verifier_scores_against_the_same_configured_glossary(self):
        translate = TranslateWithTelecomTermsNode(glossary=GLOSSARY)
        verify = VerifyTechnicalAccuracyNode(glossary=GLOSSARY, default_min_accuracy=0.8)
        text = "The base station frequency band allocation follows 3GPP standards."
        t = translate.execute({"validated_text": text, "out_of_scope": False})
        v = verify.execute(
            {
                "out_of_scope": False,
                "validated_text": text,
                "translated_text": t["translated_text"],
                "glossary_hits": t["glossary_hits"],
            }
        )
        assert v["accuracy_score"] == 1.0
        assert v["review_required"] is False


class TestPB03OutOfScopeIsSuccess:
    def test_unsupported_script_returns_success_with_an_explanation(self):
        node = DetectSourceLanguageNode()
        result = node.execute({"validated_text": "이것은 한국어 문서입니다."})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["out_of_scope"] is True
        assert result["oos_reason"]

    def test_a_document_without_configured_terminology_still_completes(self):
        detect = DetectSourceLanguageNode()
        translate = TranslateWithTelecomTermsNode(glossary=GLOSSARY)
        verify = VerifyTechnicalAccuracyNode(glossary=GLOSSARY)
        output = OutputValidateNode(glossary=GLOSSARY)

        text = "The quarterly newsletter covers staff updates and the picnic schedule."
        d = detect.execute({"validated_text": text})
        assert d["out_of_scope"] is False
        t = translate.execute({**d, "validated_text": text})
        assert t["glossary_hits"] == []
        v = verify.execute({**d, "validated_text": text, "translated_text": t["translated_text"], "glossary_hits": []})
        assert v["accuracy_score"] == 1.0
        o = output.execute({**d, **t, **v, "out_of_scope": False})
        assert o["status"] == AgentStatus.SUCCESS.value
        assert "Terminology preserved: (none)" in o["result"]


class TestPB04AuditIsAFreeFunction:
    @pytest.mark.parametrize("node_cls", _BOUNDARY_NODES, ids=lambda c: c.__name__)
    def test_execute_calls_the_free_function(self, node_cls):
        tree = ast.parse(textwrap.dedent(inspect.getsource(node_cls)))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        bound = [
            c
            for c in calls
            if isinstance(c.func, ast.Attribute)
            and c.func.attr == "emit_trace_event"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "self"
        ]
        free = [c for c in calls if isinstance(c.func, ast.Name) and c.func.id == "emit_trace_event"]
        assert not bound, f"{node_cls.__name__} must not call self.emit_trace_event"
        assert free, f"{node_cls.__name__} must call the free function emit_trace_event"


class TestPB05TrustReachability:
    """A node requiring a level no declared caller can hold makes the deployed
    agent unable to serve a request, while every unit test still passes because
    execute() bypasses the wrapper the gate lives in."""

    @pytest.mark.parametrize("node_cls", _BOUNDARY_NODES, ids=lambda c: c.__name__)
    def test_no_node_demands_more_than_the_manifest_declares(self, node_cls):
        assert node_cls.required_trust_level is not TrustLevel.INTERNAL

    @pytest.mark.parametrize("node_cls", _BOUNDARY_NODES, ids=lambda c: c.__name__)
    def test_every_node_declares_its_level_explicitly(self, node_cls):
        assert "required_trust_level" in node_cls.__dict__

    def test_the_manifest_declares_the_level_the_caller_boundary_requires(self):
        import pathlib

        import yaml

        manifest_path = pathlib.Path(__file__).resolve().parents[2] / "config" / "agent.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        assert manifest["required_trust_level"] == PreProcessNode.required_trust_level.value
