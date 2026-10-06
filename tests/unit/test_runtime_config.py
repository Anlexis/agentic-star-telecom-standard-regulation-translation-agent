"""The declared runtime configuration is load-bearing.

A declared value that nothing reads is indistinguishable from a value that was
never declared: the agent runs on hard-coded defaults, the suite stays green,
and the shipped config file is decoration. These tests pin the chain from
config/config.yaml through the graph to the nodes, and prove a declared value
changes behaviour rather than coinciding with a default.
"""

import pytest

from framework.errors import ConfigError

from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import TelecomStandardTranslationAgent, TelecomTranslationWorkflowNode
from src.services.runtime_config import RUNTIME_CONFIG_PATH, load_runtime_config, translation_settings


class TestShippedFile:
    def test_the_file_ships_and_parses(self):
        cfg = load_runtime_config()
        assert isinstance(cfg, dict)
        assert RUNTIME_CONFIG_PATH.endswith("config/config.yaml")

    def test_every_declared_translation_key_is_present(self):
        settings = translation_settings(load_runtime_config())
        for key in (
            "ja_ratio_threshold",
            "max_input_chars",
            "default_min_accuracy",
            "max_glossary_terms",
            "max_report_chars",
            "glossary",
        ):
            assert key in settings, f"config/config.yaml must declare translation.{key}"

    def test_backbone_values_are_declared(self):
        cfg = load_runtime_config()
        assert isinstance(cfg.get("max_retry"), int)
        assert isinstance(cfg.get("timeout_s"), int)

    def test_a_non_mapping_translation_block_fails_loudly(self):
        with pytest.raises(ConfigError):
            translation_settings({"translation": "nope"})


class TestConfigReachesTheNodes:
    def test_outer_graph_injects_the_declared_settings(self):
        agent = TelecomStandardTranslationAgent(config={"translation": {"max_input_chars": 42}})
        agent.register_nodes()
        assert agent._nodes["pre_process"]._settings["max_input_chars"] == 42
        assert agent._nodes["main"]._runtime_config["translation"]["max_input_chars"] == 42

    def test_a_bare_constructor_still_reads_the_shipped_file(self):
        """A bare Graph() must not silently run on hard-coded defaults."""
        agent = TelecomStandardTranslationAgent()
        settings = translation_settings(agent.effective_config())
        assert settings["glossary"], "the shipped glossary must reach a bare-constructed graph"

    def test_main_node_forwards_only_declared_keys(self):
        node = TelecomTranslationWorkflowNode(runtime_config={"translation": {"a": 1}, "max_retry": 2})
        forwarded = node._parent_config()
        assert forwarded["translation"] == {"a": 1}
        assert forwarded["max_retry"] == 2
        assert "timeout_s" not in forwarded

    def test_inner_graph_builds_nodes_from_the_declaration(self):
        graph = DomainWorkflowGraph(
            config={
                "translation": {
                    "ja_ratio_threshold": 0.75,
                    "max_input_chars": 55,
                    "default_min_accuracy": 0.25,
                    "max_glossary_terms": 2,
                    "glossary": {"x": {"source": "alpha", "target": "beta"}},
                }
            }
        )
        graph.register_nodes()
        assert graph._nodes["validate_input"]._max_input_chars == 55
        assert graph._nodes["detect_source_language"]._ja_ratio_threshold == 0.75
        assert graph._nodes["translate_with_telecom_terms"]._max_glossary_terms == 2
        assert graph._nodes["verify_technical_accuracy"]._default_min_accuracy == 0.25
        assert graph._nodes["output_validate"]._glossary.term_ids == ["x"]


class TestDeclaredValuesChangeBehaviour:
    """A declared value that coincides with a default proves nothing."""

    def _run(self, glossary):
        graph = DomainWorkflowGraph(config={"translation": {"glossary": glossary}})
        graph.compile()
        return graph

    def test_a_declared_glossary_changes_the_translation(self):
        text = "The alpha unit reports to the gamma controller."
        first = self._run({"a": {"source": "alpha", "target": "ALPHA-JA"}})
        second = self._run({"g": {"source": "gamma", "target": "GAMMA-JA"}})

        node_a = first._nodes["translate_with_telecom_terms"]
        node_g = second._nodes["translate_with_telecom_terms"]
        out_a = node_a.execute({"validated_text": text, "out_of_scope": False})
        out_g = node_g.execute({"validated_text": text, "out_of_scope": False})

        assert "ALPHA-JA" in out_a["translated_text"]
        assert "ALPHA-JA" not in out_g["translated_text"]
        assert "GAMMA-JA" in out_g["translated_text"]

    @pytest.mark.parametrize(
        "declared",
        [
            {"ja_ratio_threshold": "x"},
            {"ja_ratio_threshold": 2.0},
            {"max_input_chars": 0},
            {"max_input_chars": True},
            {"default_min_accuracy": -1},
            {"max_glossary_terms": 0},
            {"glossary": {"x": {"source": "a"}}},
        ],
    )
    def test_a_malformed_declaration_fails_the_invocation(self, declared):
        graph = DomainWorkflowGraph(config={"translation": declared})
        with pytest.raises(ConfigError):
            graph.compile()
