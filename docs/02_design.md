# Template Design Specification — TEL-C2-008

Telecom Technical Standard & Regulation Translation Agent.

Translates telecom standards and regulatory text between English and Japanese
while preserving the domain terminology a standards document depends on.

## Position in the framework architecture

| Layer | Value |
|---|---|
| Agent class | `TelecomStandardTranslationAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Inner graph | `DomainWorkflowGraph` (`src/graph/domain_workflow_graph.py`), inherits BaseGraph |
| Category | Cat 2 — a multi-step domain workflow behind the fixed outer backbone |
| Declared caller trust | `VERIFIED_EXTERNAL` |

Three-layer separation:

- **State** — a flat `TypedDict` (`src/schemas/state.py`). No models, no
  dataclasses, no arbitrary objects: checkpoints are msgpack-serialised and
  anything else corrupts silently.
- **Node** — every node subclasses `FunctionNode` and overrides only
  `execute(self, state) -> dict`, returning the keys it changes.
- **Graph** — composition through `register_nodes()`; the outer backbone wiring
  is never overridden.

## Architecture overview

### Outer backbone

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry)
                                         pre_process
```

| Slot | Class | Responsibility | Trust |
|---|---|---|---|
| initialize | `InitializeNode` (framework default) | seeds the invocation state | ANONYMOUS |
| pre_process | `PreProcessNode` | caller trust gate + caller-data contract | VERIFIED_EXTERNAL |
| main | `TelecomTranslationWorkflowNode` (GraphNode) | runs the inner workflow | VERIFIED_EXTERNAL |
| post_process | `PostProcessNode` | output boundary and release decision | ANONYMOUS |
| finalize | `FinalizeNode` (framework default) | response metadata | ANONYMOUS |

`PostProcessNode` is deliberately ANONYMOUS. The trust decision is made once,
at `pre_process`; gating the output boundary as well would let a trust denial
skip the release decision on exactly the paths that need it.

No node requires `INTERNAL`. A node on the request path that required it could
never be reached by the caller the manifest declares, and the trust gate would
deny the request before `execute()` ran — while every node-level unit test
still passed, because those bypass the wrapper the gate lives in.

### Inner workflow

```
START → validate_input → detect_source_language → translate_with_telecom_terms
      → verify_technical_accuracy → output_validate → END
```

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| `ValidateInputNode` | re-applies the caller contract inside the workflow | `user_input`, `translation_controls` | `validated_text`, `document_ref`, `target_language_request`, `min_accuracy`, `max_terms`, `selected_terms` |
| `DetectSourceLanguageNode` | EN/JA classification by character-set share | `validated_text`, `target_language_request` | `detected_language`, `target_language`, `out_of_scope`, `oos_reason` |
| `TranslateWithTelecomTermsNode` | renders the target-language text, preserving configured terminology | `validated_text`, `selected_terms`, `max_terms` | `translated_text`, `glossary_hits` |
| `VerifyTechnicalAccuracyNode` | scores terminology preservation against the caller threshold | `validated_text`, `translated_text`, `glossary_hits`, `min_accuracy` | `accuracy_score`, `accuracy_flags`, `review_required` |
| `OutputValidateNode` | renders the report, or refuses when there is nothing to report | the above | `result`, `translation_report` |

The topology is linear; `add_conditional_edges()` is not used. `route()` is
implemented to satisfy the abstract base and is annotated with this graph's own
`State`: LangGraph reads a path callable's annotation as its input schema and
projects away every field the annotation does not carry, so annotating with the
framework base state would silently blank any routing flag.

### Crossing the subgraph boundary

The framework forwards only the input **string** into a subgraph — the outer
state's `input_context` does not cross. `src/graph/context_bridge.py` carries
the validated control record across out of band: the main node stashes it and
the inner graph's `_extra_initial_state()` pops it into `translation_controls`.
The value is scoped to the current logical context and cleared on read, so
concurrent invocations cannot observe each other's records and a stale record
cannot reach a later run.

Without the bridge every caller control silently reverts to a default and the
agent still answers — which is why this is proven end to end rather than at
node level.

## Configuration contract

Two files, with no overlap:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the **flat** manifest: identity, category, entry point, declared trust level, `requires.secrets` / `requires.extras` | the agent registry |
| `config/config.yaml` | every runtime parameter: `max_retry`, `timeout_s`, and the whole `translation` block | `src/services/runtime_config.py` |

The manifest has no runtime section. A reader that looks for one there gets an
empty mapping and the agent runs on hard-coded defaults with every declared
value ignored, which is indistinguishable from a value that was never declared.

`translation` values and where each one lands:

| Key | Effect |
|---|---|
| `ja_ratio_threshold` | Japanese-script share at or above which the source is classified `ja` |
| `max_input_chars` | largest single-call document accepted at intake |
| `default_min_accuracy` | terminology-preservation floor applied when the caller names none |
| `max_glossary_terms` | ceiling on the number of glossary entries applied to one document |
| `max_report_chars` | largest report body the filing channel accepts |
| `glossary` | the terminology itself: term id → `{source, target}` |

The loader fails loudly (`ConfigError`) when the file is missing, unparseable or
malformed, and the inner graph type-checks every declared value before it
compiles. The values reach the nodes by constructor injection at registration
time, because a node's `execute()` contract takes no configuration argument.

`requires.secrets` is empty. Nothing in this template calls
`ctx.secrets.require()`, and declaring a secret that the deployment does not
provision makes the agent fail at compile time.

## Caller-data contract

`/invoke` takes the document as `input` and the structured controls as
`input_context`. The contract lives in `src/services/caller_contract.py`; the
outer boundary applies it and the inner pipeline re-applies it, so the workflow
keeps its guarantee even when driven directly.

| Field | Rule |
|---|---|
| `input` | string, control characters stripped, at most `max_input_chars`, screened |
| `input_context.document_ref` | inert identifier `[a-z0-9_]{1,32}`; renders into the report header |
| `input_context.target_language` | closed vocabulary `en` / `ja`; absent means auto-detect |
| `input_context.min_accuracy` | finite number in `[0.0, 1.0]` |
| `input_context.max_terms` | integer in `[1, 256]` |
| `input_context.glossary_ids` | at most 32 inert identifiers, each a configured term id |

Rules that hold across every field:

- **Numbers are parsed strictly, never coerced.** A bool is not a ratio and
  `"0.8"` is not a ratio. `NaN` and the infinities arrive over the wire as raw
  JSON and pass `float()` unharmed, and every comparison against `NaN` is
  `False` — a silent fail-OPEN on exactly the decision the field drives.
- **Unknown keys are dropped** by whitelist projection.
- **Errors name the field and the rule, never the rejected value.**
- **Caller strings that render into output are locked to inert identifiers.**

### Screens

- **Chat-template control tokens are screened as a class** — `<|…|>`,
  `[INST]` / `[/INST]`, `<<SYS>>` / `<</SYS>>` — over the raw text and over a
  markup-stripped view of it. Both views matter: a token is only visible before
  a strip could delete it, and a directive spliced with inline tags
  (`ig<b>nore</b> all previous instructions`) is only visible after the strip
  re-assembles it. The structured channel is walked depth-first including
  mapping KEYS, after parsing, so an escaped payload cannot evade the scan.
  A finding here is not a value the caller can correct, and it terminates the
  run rather than completing it.
- **Credential shapes are refused at the boundary.** The platform output gate
  scans every value a node returns, and the first node returns the caller's
  context verbatim — so a credential-shaped string kills the run at node one
  with an internal error the caller cannot act on. The request cannot succeed
  either way, so it is refused up front with a message naming the field. The
  check delegates to the framework's own detector, so the refusal set and the
  platform block set cannot drift apart. This refusal also terminates: a
  credential arriving on the caller channel is a containment event, not a
  formatting mistake to correct.

### Two outcomes for a rejected request

A rejection is not one behaviour. Which of the two applies is decided at the
raise site by an explicit reason code, never by the text of the error, so
rewording a message cannot move a request from one outcome to the other.

**A value the caller can correct completes the run.** The status is success and
the run carries a closed-set reason code — `EMPTY_INPUT`, `QUESTION_TOO_LONG` or
`INVALID_REQUEST` — in `error_code`. Every node after the one that set it does
no work and passes the code on, and the output boundary renders it as one fixed
sentence from `src/services/failure_message.py`. Nothing is translated and
nothing is released; the run ends by naming what to change. Terminating instead
would end the calling turn and surface only an exception type, leaving the
reason reachable from the audit trail alone — and a caller who sent a
correctable value is exactly the caller who can fix it and send the request
again on the same conversation.

**A refusal the caller cannot reword past terminates with an error status.**
Three findings reach this path at intake: a chat-template control token, a
high-confidence spliced directive, and a credential shape on either caller
channel. The first two raise a distinct error class, caught ahead of the
correctable one, so the distinction survives any rewording of a message; the
credential branch selects the terminating outcome explicitly. A terminating
refusal at intake short-circuits the backbone to finalize, so the output
boundary never runs — which is what makes it provable that such a refusal came
from intake rather than from a later layer. The output boundary and the subgraph
boundary terminate on their own violations for the same reason; see **Output
boundary** below.

`error_code` is deliberately internal. The response envelope carries `output`,
`status`, `trace_id`, `correlation_id` and `node_history` — no reason code — so
the code is not part of the caller contract and the reason reaches the caller
through the message text alone. The field path and the rule that was broken stay
in `error_log`, the internal audit channel.

## Output boundary

A released report carries the translation, the configured target forms of the
terminology it preserved, and inert caller identifiers. Before release,
`PostProcessNode` checks, in this order:

1. **credential shapes**, on the untouched text — a pattern scan must see the
   text before anything rewrites it;
2. **chat-template control tokens** in the rendered report;
3. **the report size** against `max_report_chars`.

Those checks are reached only when there is a report to check. A request
declined earlier in the run arrives here carrying its reason code, and the
boundary completes with the fixed sentence for that code and releases nothing.
That branch is evaluated before anything else: a declined request produced no
report, so the upstream-failure branch below would otherwise fire and replace a
specific, actionable reason with a bare withheld-output error.

**No rounding grid applies to this template.** It renders no monetary
aggregates; the only numeric it emits is a terminology-preservation ratio, and
rounding a figure inside a standards translation would falsify the document
being translated. The invariant enforced instead is the release rule above.

A violation of the release rule terminates the run — there is no correctable
value behind it, and a run that shipped nothing must not report as completed.
The node returns an error status, **clears every answer-bearing field**, and
puts a **truthy** notice in `formatted_output`. All three parts matter:

- the framework resolves the caller's answer as `formatted_output or result`,
  with no status check, so an error status alone still ships the un-gated
  report;
- a falsy notice (`""`, `{}`, an absent key) ACTIVATES that fallback rather
  than suppressing it;
- LangGraph merges partial deltas, so a key that is merely omitted keeps its
  old value — the cleared keys are written back explicitly.

The notice is composed from constants and the violation label only. It is never
rebuilt from the state that was just cleared: a replacement assembled out of the
same state is containment in form only.

The same rule governs the subgraph boundary. On a non-success inner result,
`merge_output()` carries no answer-bearing field into the outer state — neither
into the envelope nor into the delta a checkpoint would keep.

Violation labels are a closed set (`credential_shape`, `control_token`,
`report_exceeds_channel_limit`, `upstream_failure`). An error entry names a
label and nothing else: an upstream error body is unbounded third-party text.

## Import isolation

- The template imports `framework.*` and `shared.*` only.
- No platform-internal SDK import anywhere in `src/`; `tests/proof_of_boundary/
  test_import_isolation.py` asserts it by AST scan.

## Design decisions

| Decision | Chosen | Rationale |
|---|---|---|
| Base class | AgentBaseGraph | fixed backbone + one domain slot; the workflow is a pipeline, not an autonomous loop |
| Composition | GraphNode subgraph | keeps the multi-step workflow out of the outer backbone |
| Error propagation | propagate | an inner failure is a request failure; the boundary decides what the caller sees |
| Terminology source | declared configuration | a deployment retunes its glossary without a code change; an empty glossary preserves nothing visibly rather than falling back to a copy in code |
| Out-of-scope handling | SUCCESS with an explanation | the agent behaved correctly; an error status would tell the caller something failed |
| Correctable caller value | complete, carrying a reason code | the caller can fix the value and resend on the same conversation; terminating would end the turn and surface only an exception type |
| Spliced instruction or credential shape | terminate | not a value any rewording reaches; reporting it as a completed run would relax the refusal without changing a screening rule |
| Report size violation | withhold, never truncate | a truncated standards translation is a wrong translation, not a shorter one |
