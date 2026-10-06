# Telecom Standard & Regulation Translation Agent

AI agent for translating telecom technical standards and regulations between English and Japanese, built with Agentic Star.

> **Category**: Cat 2 (a multi-step domain workflow behind the fixed backbone)
> **Industry**: Telecommunications
> **Template ID**: TEL-C2-008

## Overview

Translates telecom technical standards and regulatory text between English and
Japanese under an explicit terminology constraint. Every term declared in the
deployment's glossary is carried across in its declared target form, the result
is scored on how much of that terminology survived, and the caller is told when
the output needs human review before it is used.

The glossary, the accuracy floor, the accepted document size and the report size
limit are all configuration — a deployment retunes them without touching code.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform's runtime services (secret provisioning, audit backend) are unavailable or
the SDK version does not match, start-up fails during graph compile / secret provisioning rather
than starting in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Using it

`POST /invoke` takes the document as `input` and the bounded controls as
`input_context`:

```json
{
  "input": "The 3GPP specification defines handover procedures for the 5G NR base station.",
  "input_context": {
    "document_ref": "spec_2026_001",
    "target_language": "ja",
    "min_accuracy": 0.9,
    "glossary_ids": ["handover", "base_station"]
  }
}
```

Every control field is optional and bounded; unknown keys are dropped. The
response carries the report — the document reference, the direction, the
terminology preserved and not carried over, the preservation score, the review
verdict and the translation itself.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent.yaml (manifest) and config.yaml (runtime parameters)
docs/         design and operational documentation
```

`docs/02_design.md` (design) and `docs/03_test_spec.md` (test specification)
ship with this repository.

## Customising

1. Edit `config/config.yaml` — the terminology glossary is the main entry point,
   alongside the detection threshold, the accuracy floor and the size limits.
2. Point the translation node at your own translation model; the node interface
   and the glossary constraint stay as they are.
3. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
