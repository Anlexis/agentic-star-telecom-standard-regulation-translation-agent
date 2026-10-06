"""AgentCore Platform v1.0"""

# TEL-C2-008 — caller-field validation contract (single source of truth).
#
# Every caller-supplied field is hostile until proven bounded. This module owns
# the rules; the outer pre-process node applies them at the caller boundary and
# the inner input-validation node re-applies them inside the workflow, so the
# pipeline keeps its guarantee even when it is driven directly.
#
# Principles:
#   - Strict typing, not coercion. A bool is not a ratio, "0.8" is not a ratio,
#     and NaN/+-Infinity parse fine through float() while every comparison
#     against them is False — a silent fail-OPEN on exactly the accuracy
#     decision this agent exists to make. Numbers are therefore accepted only
#     as real int/float values inside explicit bounds.
#   - Fail closed with an error that names the FIELD. The rejected value is
#     never echoed into an error, a log line, or an audit event.
#   - Caller strings that render into the report are locked to inert
#     identifiers; the document text itself is length-capped, control-stripped
#     and screened.
#   - Chat-template control tokens are screened as a CLASS, over the raw text
#     and over a markup-stripped view of it, because a markup strip can
#     re-assemble a directive that neither view alone reveals.

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials_in_value
from framework.security.injection_detector import detect_injection

# ── Bounds (explicit) ─────────────────────────────────────────────────────────

# Hard ceiling on the configured single-call document size, independent of the
# declared value: a misconfigured file cannot widen the caller-facing limit.
MAX_INPUT_CHARS_CEILING = 200_000

MAX_GLOSSARY_IDS = 32
MAX_GLOSSARY_TERMS_CEILING = 256
MAX_DOCUMENT_REF_LEN = 32

# Inert identifier alphabet for every caller string that renders into output.
_INERT_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Closed language vocabulary. Auto-detection applies when the caller omits it.
_LANGUAGE_VOCAB = frozenset({"en", "ja"})

# C0/C1 control characters except newline and tab (documents legitimately use
# both). Stripping the rest removes a channel for hiding directives.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# ── Control-token screen (the class, not a phrase list) ───────────────────────
#
# The framework's own detector classifies `[INST]`/`[/INST]`/`[SYS]`/`[/SYS]`
# and the literal `<|im_start|>` as high-confidence, and nothing else in this
# family. It does NOT flag `<<SYS>>`, nor `<|...|>` markers other than that one
# literal. Those are the shapes a chat template consumes as structure, so they
# are screened here as a class rather than as individual strings.
_CONTROL_TOKEN_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("pipe_marker", re.compile(r"<\|[^|>]{0,64}\|>")),
    ("bracket_marker", re.compile(r"\[/?(?:INST|SYS)\]", re.IGNORECASE)),
    ("angle_sys_marker", re.compile(r"<</?SYS>>", re.IGNORECASE)),
]

# Markup stripped before the second pass. A directive split by inline tags
# ("ig<b>nore</b> all previous instructions") is only visible once the tags are
# removed; a control token is only visible BEFORE they are, because the strip
# can delete it. Both views are therefore screened.
_MARKUP_RE = re.compile(r"<[^<>]{0,128}>")


class FieldContractError(ValueError):
    """A caller field failed the validation contract.

    The message names the FIELD and the rule — never the rejected value.

    `code` is the closed-set reason a caller-facing sentence is chosen from. It
    is set at the RAISE site, never derived from the message, so rewording an
    error can never silently change which sentence the caller reads.
    """

    def __init__(self, message: str, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code


class HostileContentError(FieldContractError):
    """A caller field carried spliced instructions, not a correctable value.

    A SUBCLASS rather than a flag or a message prefix. Every existing
    ``except FieldContractError`` keeps catching it, so nothing that relies on
    the base class changes behaviour — but a handler that must treat a refusal
    differently from a value the caller can fix catches THIS first, and the
    distinction survives any rewording of the message.

    This is the class that must NEVER be reported as a completed run: a caller
    cannot correct their way past it, and reporting it like an ordinary declined
    value would relax the refusal without changing a single screening rule.
    """


def strip_markup(text: str) -> str:
    """Return *text* with simple inline markup removed (second screening view)."""
    return _MARKUP_RE.sub("", text)


def find_control_tokens(text: str) -> List[str]:
    """Return the sorted control-token classes present in *text* or its stripped view.

    Screens BOTH views: a token is caught before a markup strip can delete it,
    and a directive spliced with inline tags is caught after the strip
    re-assembles it.
    """
    hits: set[str] = set()
    for view in (text, strip_markup(text)):
        for label, pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(view):
                hits.add(label)
    return sorted(hits)


def find_directive_injection(text: str) -> List[str]:
    """Return high-confidence injection types in *text* or its stripped view."""
    types: set[str] = set()
    for view in (text, strip_markup(text)):
        types.update(f["type"] for f in detect_injection(view) if f.get("confidence") == "high")
    return sorted(types)


def screen_untrusted_text(value: Any, field_name: str, max_len: int) -> str:
    """Validate, sanitise and screen a caller free-text field.

    Fails closed on: wrong type, over-length, chat-template control tokens, and
    high-confidence directive injection. The error names the field and the
    finding CLASS — never the matched text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FieldContractError(f"{field_name} must be a string")
    if len(value) > max_len:
        # "input" is the whole request; any other field is one part of a
        # structured record, and telling the caller the REQUEST is too long
        # would name the wrong thing to shorten.
        raise FieldContractError(
            f"{field_name} exceeds the {max_len}-character limit",
            code="QUESTION_TOO_LONG" if field_name == "input" else "INVALID_REQUEST",
        )
    text = _CONTROL_CHARS_RE.sub("", value).strip()
    if not text:
        return ""

    tokens = find_control_tokens(text)
    if tokens:
        raise HostileContentError(f"{field_name} contains chat-template control tokens ({', '.join(tokens)})")

    directives = find_directive_injection(text)
    if directives:
        raise HostileContentError(f"{field_name} contains directive content ({', '.join(directives)})")

    return text


# ── Structured-context screening (depth-first, KEYS included) ─────────────────


def _walk_context(node: Any, path: str, findings: List[Tuple[str, str]], depth: int) -> None:
    """Collect (path, class) control-token findings depth-first, keys included."""
    if depth > 8:
        findings.append((path, "depth_limit"))
        return
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = key if isinstance(key, str) else repr(key)
            for label in find_control_tokens(key_str):
                findings.append((f"{path}.<key>", label))
            _walk_context(value, f"{path}.{_safe_component(key_str)}", findings, depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_context(value, f"{path}[{index}]", findings, depth + 1)
    elif isinstance(node, str):
        for label in find_control_tokens(node):
            findings.append((path, label))


def _safe_component(name: str) -> str:
    """Return *name* if it is an inert identifier, else a masked placeholder.

    Field names are caller data too: an unrecognised name is reported by shape,
    never echoed.
    """
    return name if _INERT_ID_RE.match(name) else "<masked>"


def screen_context_tokens(input_context: Any) -> List[str]:
    """Return "path:class" strings for control tokens anywhere in *input_context*.

    Scans the PARSED structure depth-first including mapping keys, so a
    ``\\u``-escaped payload cannot evade the screen: escapes are already decoded
    by the time the object exists.
    """
    findings: List[Tuple[str, str]] = []
    _walk_context(input_context, "input_context", findings, 0)
    return [f"{path}:{label}" for path, label in findings]


def screen_context_credentials(input_context: Any) -> List[str]:
    """Return the names of top-level context fields carrying credential shapes.

    Delegates to the framework's own detector — the same function the platform
    output gate calls — so this refusal set matches the framework block set
    exactly. ``detect_credentials_in_value`` on a mapping is defined as the
    union over its values, so scanning per field is equivalent to scanning the
    whole mapping while still naming the field.

    Field names are caller data: a name that is not an inert identifier (or
    that trips a credential pattern itself) is reported positionally.
    """
    if not isinstance(input_context, dict):
        return []
    offenders: List[str] = []
    for index, (key, value) in enumerate(input_context.items()):
        if not detect_credentials_in_value(value):
            continue
        key_str = key if isinstance(key, str) else repr(key)
        if _INERT_ID_RE.match(key_str) and not detect_credentials_in_value(key_str):
            offenders.append(f"input_context.{key_str}")
        else:
            offenders.append(f"input_context field #{index + 1}")
    return offenders


# ── Numeric contract ──────────────────────────────────────────────────────────


def parse_bounded_ratio(value: Any, field_name: str, low: float, high: float) -> float:
    """Strictly parse a caller-controlled ratio into [low, high].

    Accepts a real int or float only (bool is rejected — it subclasses int) and
    requires the value to be finite. NaN and +-Infinity arrive over the wire as
    raw JSON and pass float() unharmed; every comparison against NaN is False,
    so an unchecked value fails OPEN.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FieldContractError(f"{field_name} must be a number between {low} and {high}; the value was rejected")
    number = float(value)
    if not math.isfinite(number):
        raise FieldContractError(f"{field_name} must be a finite number; the value was rejected")
    if number < low or number > high:
        raise FieldContractError(f"{field_name} must be between {low} and {high}; the value was rejected")
    return number


def parse_bounded_count(value: Any, field_name: str, low: int, high: int) -> int:
    """Strictly parse a caller-controlled count into [low, high] (ints only)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldContractError(f"{field_name} must be an integer between {low} and {high}; the value was rejected")
    if value < low or value > high:
        raise FieldContractError(f"{field_name} must be between {low} and {high}; the value was rejected")
    return int(value)


# ── Field contracts ───────────────────────────────────────────────────────────


def validate_inert_id(value: Any, field_name: str) -> str:
    """Validate a caller string that renders into the report."""
    if not isinstance(value, str) or not _INERT_ID_RE.match(value):
        raise FieldContractError(
            f"{field_name} must be 1-{MAX_DOCUMENT_REF_LEN} characters of [a-z0-9_]; the value was rejected"
        )
    return value


def validate_language(value: Any, field_name: str) -> Optional[str]:
    """Validate an explicit language selection against the closed vocabulary."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in _LANGUAGE_VOCAB:
        raise FieldContractError(f"{field_name} must be one of: en, ja")
    return value


def validate_glossary_ids(value: Any, known_ids: Optional[List[str]] = None) -> List[str]:
    """Validate the caller's glossary selection (inert identifiers, bounded list)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise FieldContractError("glossary_ids must be a list of identifiers")
    if len(value) > MAX_GLOSSARY_IDS:
        raise FieldContractError(f"glossary_ids accepts at most {MAX_GLOSSARY_IDS} entries")
    selected: List[str] = []
    for index, entry in enumerate(value):
        term_id = validate_inert_id(entry, f"glossary_ids[{index}]")
        if known_ids is not None and term_id not in known_ids:
            raise FieldContractError(f"glossary_ids[{index}] names a term that is not configured")
        if term_id not in selected:
            selected.append(term_id)
    return selected


# ── Whole-request contract ────────────────────────────────────────────────────

#: Keys the request contract recognises. Anything else is dropped by the
#: projection below, so an undeclared key never travels further into the graph.
CONTEXT_FIELDS = ("document_ref", "target_language", "min_accuracy", "max_terms", "glossary_ids")


def validate_translation_request(
    document_text: Any,
    input_context: Any,
    *,
    max_input_chars: int,
    default_min_accuracy: float,
    max_glossary_terms: int,
    known_glossary_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Validate the whole caller request and return the projected control record.

    Unknown context keys are DROPPED (whitelist projection). Raises
    FieldContractError on the first violation; the message names the field.
    """
    limit = min(int(max_input_chars), MAX_INPUT_CHARS_CEILING)
    text = screen_untrusted_text(document_text, "input", limit)
    if not text:
        raise FieldContractError("input is required (the document text to translate)", code="EMPTY_INPUT")

    context: Dict[str, Any] = input_context if isinstance(input_context, dict) else {}

    token_findings = screen_context_tokens(context)
    if token_findings:
        raise HostileContentError(f"input_context contains chat-template control tokens ({', '.join(token_findings)})")

    document_ref = context.get("document_ref")
    ref = validate_inert_id(document_ref, "input_context.document_ref") if document_ref not in (None, "") else None

    target_language = validate_language(context.get("target_language"), "input_context.target_language")

    raw_min_accuracy = context.get("min_accuracy")
    min_accuracy = (
        default_min_accuracy
        if raw_min_accuracy is None
        else parse_bounded_ratio(raw_min_accuracy, "input_context.min_accuracy", 0.0, 1.0)
    )

    raw_max_terms = context.get("max_terms")
    max_terms = (
        min(int(max_glossary_terms), MAX_GLOSSARY_TERMS_CEILING)
        if raw_max_terms is None
        else parse_bounded_count(raw_max_terms, "input_context.max_terms", 1, MAX_GLOSSARY_TERMS_CEILING)
    )

    glossary_ids = validate_glossary_ids(context.get("glossary_ids"), known_glossary_ids)

    return {
        "document_text": text,
        "document_ref": ref,
        "target_language": target_language,
        "min_accuracy": min_accuracy,
        "max_terms": max_terms,
        "glossary_ids": glossary_ids,
    }
