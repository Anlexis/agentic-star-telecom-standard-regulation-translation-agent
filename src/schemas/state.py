"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. LangGraph checkpoints
# use msgpack serialization; model objects cause silent corruption. Extend the
# framework state with agent-specific fields only. Do NOT add credentials or
# secrets.
#
# TEL-C2-008 — Telecom Technical Standard & Regulation Translation Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). The fields below cover both layers.
#
# State-key producer → consumer contract:
#   validated_input          PreProcessNode (outer)       → main slot
#   validated_context        PreProcessNode (outer)       → main slot (bridge)
#   translation_controls     inner graph seed             → ValidateInputNode
#   validated_text           ValidateInputNode            → DetectSourceLanguageNode
#   validation_error         ValidateInputNode            → OutputValidateNode
#   document_ref             ValidateInputNode            → OutputValidateNode
#   target_language_request  ValidateInputNode            → DetectSourceLanguageNode
#   min_accuracy             ValidateInputNode            → VerifyTechnicalAccuracyNode
#   max_terms                ValidateInputNode            → TranslateWithTelecomTermsNode
#   selected_terms           ValidateInputNode            → TranslateWithTelecomTermsNode
#   detected_language        DetectSourceLanguageNode     → TranslateWithTelecomTermsNode
#   target_language          DetectSourceLanguageNode     → TranslateWithTelecomTermsNode
#   out_of_scope             DetectSourceLanguageNode     → OutputValidateNode
#   oos_reason               DetectSourceLanguageNode     → OutputValidateNode
#   translated_text          TranslateWithTelecomTermsNode→ VerifyTechnicalAccuracyNode
#   glossary_hits            TranslateWithTelecomTermsNode→ OutputValidateNode
#   accuracy_score           VerifyTechnicalAccuracyNode  → OutputValidateNode
#   accuracy_flags           VerifyTechnicalAccuracyNode  → OutputValidateNode
#   review_required          VerifyTechnicalAccuracyNode  → OutputValidateNode
#   translation_report       OutputValidateNode           → PostProcessNode (outer)
#   formatted_output         PostProcessNode (outer)      → finalize

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for TEL-C2-008.

    All shared fields (user_input, input_context, status, session_id,
    node_history, error_log, trace_id, correlation_id and the rest) are
    inherited from the framework state. Only template-specific keys are added
    here. Every field is Optional so a LangGraph checkpoint can be restored
    before the producing node has run.
    """

    # ------------------------------------------------------------------
    # PreProcessNode (outer pre_process slot)
    # ------------------------------------------------------------------

    # Screened document text accepted for translation.
    validated_input: Optional[str]

    # Projected caller control record, JSON-encoded, handed to the main slot.
    validated_context: Optional[str]

    # Channel metadata attached at intake.
    enriched_context: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner workflow seed (context bridge)
    # ------------------------------------------------------------------

    # Validated caller controls seeded into the inner graph's initial state.
    translation_controls: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # ValidateInputNode (inner step 1)
    # ------------------------------------------------------------------

    # Screened, non-empty document text.
    validated_text: Optional[str]

    # Field-naming validation error (None when the request is valid).
    validation_error: Optional[str]

    # Inert caller reference rendered into the report header.
    document_ref: Optional[str]

    # Caller's explicit target-language selection ("en" / "ja"), if any.
    target_language_request: Optional[str]

    # Terminology preservation ratio the caller requires, in [0.0, 1.0].
    min_accuracy: Optional[float]

    # Caller cap on how many glossary entries are applied.
    max_terms: Optional[int]

    # Caller's glossary selection (empty means the whole configured glossary).
    selected_terms: Optional[List[str]]

    # ------------------------------------------------------------------
    # DetectSourceLanguageNode (inner step 2)
    # ------------------------------------------------------------------

    # Detected source language: "en", "ja" or "unknown".
    detected_language: Optional[str]

    # Language the translation targets.
    target_language: Optional[str]

    # True when the request is outside the supported language pair. Out-of-scope
    # is not a status: the run succeeds and the report explains the outcome.
    out_of_scope: Optional[bool]

    # Explanation of why the request is out of scope (None when in scope).
    oos_reason: Optional[str]

    # ------------------------------------------------------------------
    # TranslateWithTelecomTermsNode (inner step 3)
    # ------------------------------------------------------------------

    # Translated output text.
    translated_text: Optional[str]

    # Configured glossary ids matched in the source and applied.
    glossary_hits: Optional[List[str]]

    # ------------------------------------------------------------------
    # VerifyTechnicalAccuracyNode (inner step 4)
    # ------------------------------------------------------------------

    # Ratio (0.0-1.0) of configured terms preserved in the translation.
    accuracy_score: Optional[float]

    # Configured term ids found in the source but absent from the output.
    accuracy_flags: Optional[List[str]]

    # True when the score is below the threshold the caller requires.
    review_required: Optional[bool]

    # ------------------------------------------------------------------
    # OutputValidateNode (inner step 5) / PostProcessNode (outer post_process)
    # ------------------------------------------------------------------

    # Rendered report produced by the inner output boundary.
    translation_report: Optional[str]

    # Final caller-facing output written by the outer output boundary.
    formatted_output: Optional[str]
    # Set when a run COMPLETES without carrying out the request, because the
    # caller sent a value they can correct. A closed set of codes, never caller
    # content. Nodes downstream of the one that set it do no work and pass it on.
    error_code: Optional[str]
