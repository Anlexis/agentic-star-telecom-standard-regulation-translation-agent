# Test Specification — TEL-C2-008

## Strategy

Two layers, with a deliberate division of labour:

- **Unit tests** (`tests/unit/`) pin node behaviour, the caller-data contract
  and the configuration chain.
- **Boundary proofs** (`tests/proof_of_boundary/`) drive the REAL ASGI entry
  point in `src/api/server.py`. Anything that has to be true of the deployed
  agent is proven there, because a suite that only calls `execute()` bypasses
  the wrapper the trust gate and the output gate live in — such a suite can be
  entirely green on an agent that cannot serve a single request.

Run: `python -m pytest tests/ -v`.

## Framework compliance

| TC-ID | Test | Expected result | Where |
|---|---|---|---|
| TC-01 | State is a flat TypedDict | no models, no dataclasses | `tests/proof_of_boundary/test_state_safety.py` |
| TC-02 | invalid caller input is refused | every out-of-contract value raises; the error names the field and never echoes the value | `tests/unit/test_caller_contract.py` |
| TC-03 | no credential material in state | credential scan clean | `tests/proof_of_boundary/test_state_safety.py` |
| TC-04 | invocation context is not carried in state | state carries primitives only | `tests/proof_of_boundary/test_state_safety.py` |
| TC-05 | no duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from node bodies | `tests/proof_of_boundary/test_pb_tel_c2_008.py` |
| TC-06 | the framework input gate is not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | the framework output gate is not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` is enforced | an under-privileged caller is denied before `execute()` | `tests/proof_of_boundary/test_pb_invoke_order.py` |
| TC-09 | every node declares its trust level explicitly and reachably | no node requires a level the declared caller cannot hold | `tests/unit/test_nodes.py`, `tests/proof_of_boundary/test_pb_tel_c2_008.py` |
| TC-10 | the domain output hook honours the framework contract | it takes the result dict and RETURNS it | `tests/unit/test_nodes.py`, `tests/proof_of_boundary/test_pb_tel_c2_008.py` |
| TC-11 | at least one domain audit event per `execute()` | emitted as a free function, never as a bound method | `tests/proof_of_boundary/test_pb_tel_c2_008.py` |

## Proof-of-boundary

| PB-ID | Boundary | Test | Expected result |
|---|---|---|---|
| PB-1 | node → audit | a domain event fires on every invocation path | no silent failures |
| PB-2 | state serialization | post-invoke state is primitives only | no models, no dataclasses |
| PB-4 | import isolation | AST scan of `src/` | 0 platform-internal imports |
| PB-5 | checkpoint safety | inspect the state a checkpoint would hold | no credential material |
| PB-6 | invoke execution order | trust gate → `node_start` → input gate → `execute()` → output gate → `node_complete`, per node; plus the 5-node backbone order for a full invoke | order verified |
| PB-7 | human-review interrupt propagation | interrupt handling respects the parent's permission | propagation verified |
| PB-8 | the deployed agent serves a request | real `/invoke`, bearer auth, real config | a real report from the caller's own document |
| PB-9 | output containment | a report that violates the release rule is withheld | error status, truthy notice, nothing released in any channel |

### PB-6 payload contract

`deploy/invoke_payload.json["input"]` must equal the PB-6 `_VALID_PAYLOAD`
string. The deployment smoke invoke posts that file as the request body, so the
two would otherwise drift and the smoke check would stop proving anything the
suite asserts. `test_invoke_payload_matches_pb6` pins the equality.

## Business logic

| TC-ID | Test | Input | Expected result |
|---|---|---|---|
| BL-01 | English source is translated to Japanese with terminology preserved | a standards paragraph carrying configured terms | report with each term in its configured target form, score 1.00 |
| BL-02 | Japanese source is translated the other way | a Japanese standards paragraph | `Direction: ja -> en` |
| BL-03 | the output depends on the input | two different documents | two different reports |
| BL-04 | a caller glossary selection narrows what is preserved | `glossary_ids` naming one term | only that term is carried over |
| BL-05 | the caller's term cap is applied | `max_terms: 1` | one term preserved |
| BL-06 | an unsupported script is out of scope, not an error | Korean text | SUCCESS with an explanation |
| BL-07 | a target equal to the source language is out of scope | `target_language` equal to the detected language | SUCCESS with an explanation |
| BL-08 | the caller's accuracy threshold decides review | the same document at two thresholds | `Review required` flips |
| BL-09 | a declared glossary change alters the translation | two glossaries, one document | different target forms rendered |
| BL-10 | a malformed declaration fails the invocation | out-of-range or wrong-typed declared values | `ConfigError` at compile |

## Caller-contract tests

- a parametrized non-finite matrix per numeric field — `"NaN"`, `"Infinity"`,
  `"-Infinity"`, raw `float("nan")`, raw `float("inf")`, over-magnitude,
  bools, numeric strings — refused in every form, plus the same values driven
  over the wire as raw JSON through `/invoke`;
- identifier fields refused for anything outside `[a-z0-9_]{1,32}`;
- closed vocabularies refused outside their set;
- control tokens detected as a class, in both views, in values AND in mapping
  keys, including an escaped payload;
- credential shapes refused on both caller channels, with the field named and
  the value never echoed, and ordinary domain text on the same field still
  accepted;
- the fail-closed direction probed with real telecom sentences, so a screen
  cannot block legitimate work.

### What a rejection does to the run

The contract raising is only half the behaviour. What the run then does is
proven separately, because the two rejection outcomes differ.

**A value the caller can correct ends the run by COMPLETING.**
`tests/unit/test_nodes.py` pins the node delta for an empty document, a
non-string document, a document over the declared limit and an out-of-contract
control field: a success status carrying a reason code in every case. The
offending field is named in the validation error, and the rejected value appears
neither there nor in the error log.
`tests/proof_of_boundary/test_pb_e2e_invoke.py` pins the same paths end to end
over `/invoke` — the envelope status is success and the body is exactly one
fixed sentence, with no field path, no report and no echo of the rejected value
anywhere in the response. A caller who sent nothing reads a different sentence
from one who sent a value out of contract, because telling the first to check a
format would name the wrong thing to fix.

**A spliced directive, a chat-template control token or a credential shape
TERMINATES.** The same file pins an error status for each. For the control
tokens it additionally asserts that `PostProcessNode` never ran: the output
boundary screens for them too, so a test that only checked the final status
could not tell which of the two layers refused.

## Containment tests

`tests/proof_of_boundary/test_pb_output_containment.py` injects the fault on
the DATA path — a document that is legal at intake and renders a report larger
than the filing channel accepts — and checks three channels: the envelope, the
state left behind in the delta (presence AND emptiness, because an omitted key
keeps its old value), and the error log. A clean-path control asserts the same
request shape still produces a real answer, so a refuse-everything boundary
cannot pass the file.

## Execution summary

- Total: 249 tests — 247 pass, 2 skipped (the skips are environment-gated).
- Framework under test: the published `agenticstar-agentcore` wheel, the same
  version the pipeline installs.
