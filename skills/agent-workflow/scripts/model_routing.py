#!/usr/bin/env python3
"""Deterministic, provider-neutral model-routing contract for Agent Workflow."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


POLICY_SCHEMA = "agent-workflow.model-routing-policy.v2"
CAPABILITY_SCHEMA = "agent-workflow.runtime-capabilities.v2"
PACKET_SCHEMA = "agent-workflow.routing-packet.v1"
DECISION_SCHEMA = "agent-workflow.routing-decision.v2"

CANONICAL_POLICY_ID = "responsibility-routing-codex-v2"
CANONICAL_POLICY_VERSION = 1
CANONICAL_POLICY_SEMANTICS_SHA256 = (
    "sha256:a95d420469cb4768c71e97c4c186cd7f838346c4c6c8c38ca169d76074c31669"
)
CAPABILITY_RECHECK_MAX_AGE = timedelta(hours=24)
CAPABILITY_CLOCK_SKEW = timedelta(minutes=5)
EVIDENCE_MIN_NON_WHITESPACE_BYTES = 32
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
EVIDENCE_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")

APPROVED_MODELS = ("gpt-5.6-terra", "gpt-5.6-sol")
EXCLUDED_MODELS = {"gpt-5.6-luna"}
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}

PACKET_TAXONOMY: dict[str, tuple[Any, ...] | type] = {
    "ambiguity": ("bounded", "material"),
    "coupling": ("local", "cross_boundary"),
    "blast_radius": ("local", "shared", "external_production"),
    "reversibility": ("easy", "bounded_rollback", "hard"),
    "verifiability": ("deterministic", "partial", "judgment_only"),
    "novelty": ("established", "novel"),
    "role": (
        "discover",
        "plan",
        "roundtable",
        "implement",
        "seam",
        "review",
        "challenge",
        "verify",
        "repair",
        "custom",
    ),
    "claim_class": ("routine", "judgment", "high_consequence"),
    "approval_required": bool,
}

POLICY_KEYS = {
    "schema_version",
    "snapshot_id",
    "policy_id",
    "policy_version",
    "adapter_scope",
    "content_sha256",
    "packet_taxonomy",
    "default_model",
    "model_order",
    "automatic_exclusions",
    "decision_rules",
    "verifier_rules",
    "override_policy",
    "fallback_policy",
    "attempt_policy",
}
CAPABILITY_KEYS = {
    "schema_version",
    "snapshot_id",
    "snapshot_version",
    "adapter",
    "dispatch_surface",
    "observed_at",
    "source",
    "reasoning_effort",
    "content_sha256",
    "models",
}
DECISION_KEYS = {
    "schema_version",
    "packet_id",
    "decision_id",
    "decision_sha256",
    "policy_snapshot",
    "capability_snapshot",
    "facts",
    "request",
    "matched_rule_id",
    "minimum",
    "selected",
    "override",
    "verification_floor",
    "status",
}
REQUEST_SOURCES = {"automatic", "lead", "user"}
DECISION_STATUSES = {"planned", "human_gate", "blocked"}
TRANSITIONS = {"initial", "retry", "fallback", "escalation"}
OUTCOMES = {"completed", "failed", "unavailable", "blocked"}
FAILURE_CLASSES = {
    "route_unavailable",
    "context_failure",
    "insufficient_reasoning",
    "tool_failure",
}
MISSING_EVIDENCE_ACTIONS = {"more_discovery", "human_gate", "blocked"}
CLASSIFICATIONS = {"general", "deep", "excluded", "unclassified"}


class RoutingError(ValueError):
    """Raised when a routed workflow fails the portable contract."""


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingError(f"{label} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise RoutingError(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        raise RoutingError(f"{label} has unknown keys: {', '.join(unknown)}")


def _route(value: Any, label: str = "route") -> dict[str, str]:
    route = _require_object(value, label)
    _require_exact_keys(route, {"model", "effort"}, label)
    model = route.get("model")
    effort = route.get("effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise RoutingError(f"{label}.model and .effort must be strings")
    return {"model": model, "effort": effort}


def route_key(value: Any) -> str:
    route = _route(value)
    return route["model"]


def inherited_effort(capabilities: dict[str, Any]) -> str:
    reasoning = _require_object(
        capabilities.get("reasoning_effort"), "runtime capabilities.reasoning_effort"
    )
    _require_exact_keys(
        reasoning,
        {"source", "value", "locked"},
        "runtime capabilities.reasoning_effort",
    )
    if reasoning.get("source") != "user_session":
        raise RoutingError("runtime reasoning effort must come from user_session")
    effort = reasoning.get("value")
    if effort not in REASONING_EFFORTS:
        raise RoutingError(
            "runtime reasoning effort must be one of " + ", ".join(sorted(REASONING_EFFORTS))
        )
    if reasoning.get("locked") is not True:
        raise RoutingError("runtime reasoning effort must be locked for the workflow")
    return str(effort)


def materialize_route(model: str, capabilities: dict[str, Any]) -> dict[str, str]:
    if model not in APPROVED_MODELS:
        raise RoutingError(f"model {model!r} is not approved for automatic routing")
    return {"model": model, "effort": inherited_effort(capabilities)}


def canonical_sha256(value: Any, omitted_keys: Iterable[str] = ()) -> str:
    """Hash canonical UTF-8 JSON after omitting named root-level digest keys."""

    normalized = copy.deepcopy(value)
    if isinstance(normalized, dict):
        for key in omitted_keys:
            normalized.pop(key, None)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def with_content_digest(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    normalized["content_sha256"] = canonical_sha256(
        normalized, omitted_keys=("content_sha256",)
    )
    return normalized


def parse_rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise RoutingError(f"{label} must be an RFC3339 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RoutingError(f"{label} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoutingError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_capability_availability_evidence(
    value: Any,
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an explicit, fresh recheck bound to the capability snapshot."""

    evidence = _require_object(value, "capability availability evidence")
    expected_keys = {
        "source",
        "summary",
        "verified",
        "checked_at",
        "snapshot_content_sha256",
    }
    _require_exact_keys(evidence, expected_keys, "capability availability evidence")
    if evidence.get("source") != "lead_agent":
        raise RoutingError("capability availability evidence.source must be lead_agent")
    summary = evidence.get("summary")
    if not isinstance(summary, str) or len(summary.strip()) < 16:
        raise RoutingError("capability availability evidence.summary must be substantive")
    if evidence.get("verified") is not True:
        raise RoutingError("explicit fresh capability availability evidence is required")
    if evidence.get("snapshot_content_sha256") != snapshot.get("content_sha256"):
        raise RoutingError("capability availability evidence must bind the capability snapshot")
    checked_at = parse_rfc3339(
        evidence.get("checked_at"), "capability availability evidence.checked_at"
    )
    observed_at = parse_rfc3339(
        snapshot.get("observed_at"), "runtime capabilities.observed_at"
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if checked_at < observed_at:
        raise RoutingError("capability availability recheck cannot precede snapshot observation")
    if checked_at > current + CAPABILITY_CLOCK_SKEW:
        raise RoutingError("capability availability recheck cannot be in the future")
    if current - checked_at > CAPABILITY_RECHECK_MAX_AGE:
        raise RoutingError("capability availability evidence is stale; record a fresh recheck")
    return copy.deepcopy(evidence)


def _parse_evidence_ref(value: Any, label: str) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RoutingError(f"{label} must be a non-empty trimmed string")
    if "\\" in value or "\x00" in value:
        raise RoutingError(f"{label} must use a safe POSIX workspace-relative path")
    path_text, separator, fragment = value.partition("#")
    if "#" in fragment:
        raise RoutingError(f"{label} may contain at most one fragment separator")
    if not path_text or path_text.startswith(("/", "~")):
        raise RoutingError(f"{label} must be workspace-relative")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path_text):
        raise RoutingError(f"{label} cannot be a URI or drive-qualified path")
    pure = PurePosixPath(path_text)
    if pure.as_posix() != path_text or any(part in {"", ".", ".."} for part in pure.parts):
        raise RoutingError(f"{label} cannot contain traversal or normalized path segments")
    if separator:
        if not fragment or not EVIDENCE_FRAGMENT_RE.fullmatch(fragment):
            raise RoutingError(f"{label} has an invalid evidence fragment")
        return path_text, fragment
    return path_text, None


def validate_evidence_refs(
    value: Any,
    *,
    evidence_root: Path | None,
    label: str,
    bound_attempt_id: str | None = None,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RoutingError(f"{label} must be a non-empty evidence reference list")
    if len(set(value)) != len(value):
        raise RoutingError(f"{label} contains duplicate evidence references")
    parsed = [_parse_evidence_ref(item, f"{label}[{index}]") for index, item in enumerate(value, 1)]
    if evidence_root is None:
        raise RoutingError(f"{label} requires a workspace evidence root")
    root = evidence_root.resolve()
    normalized: list[str] = []
    for index, ((relative, fragment), original) in enumerate(zip(parsed, value), start=1):
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RoutingError(f"{label}[{index}] resolves outside the workflow workspace") from exc
        if not candidate.is_file():
            raise RoutingError(f"{label}[{index}] must reference an existing workflow file")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise RoutingError(f"{label}[{index}] could not be read") from exc
        if len(b"".join(content.split())) < EVIDENCE_MIN_NON_WHITESPACE_BYTES:
            raise RoutingError(f"{label}[{index}] must reference a substantive artifact")
        if bound_attempt_id is not None:
            if fragment != bound_attempt_id or bound_attempt_id.encode("utf-8") not in content:
                raise RoutingError(
                    f"{label}[{index}] must bind the preceding failed attempt "
                    f"{bound_attempt_id} in its fragment and content"
                )
        normalized.append(original)
    return normalized


def _expected_taxonomy_json() -> dict[str, Any]:
    result: dict[str, Any] = {"schema_version": PACKET_SCHEMA, "fields": {}}
    for field, allowed in PACKET_TAXONOMY.items():
        if allowed is bool:
            result["fields"][field] = {"type": "boolean"}
        else:
            result["fields"][field] = {"enum": list(allowed)}
    return result


def validate_packet_facts(value: Any) -> dict[str, Any]:
    facts = _require_object(value, "packet facts")
    _require_exact_keys(facts, set(PACKET_TAXONOMY), "packet facts")
    for field, allowed in PACKET_TAXONOMY.items():
        item = facts[field]
        if allowed is bool:
            if not isinstance(item, bool):
                raise RoutingError(f"packet facts.{field} must be boolean")
        elif item not in allowed:
            raise RoutingError(
                f"packet facts.{field} must be one of {sorted(allowed)}"
            )
    return copy.deepcopy(facts)


def _validate_match(value: Any, label: str) -> dict[str, list[Any]]:
    match = _require_object(value, label)
    unknown = sorted(set(match) - set(PACKET_TAXONOMY))
    if unknown:
        raise RoutingError(f"{label} has unknown packet fields: {', '.join(unknown)}")
    normalized: dict[str, list[Any]] = {}
    for field, accepted in match.items():
        if not isinstance(accepted, list) or not accepted:
            raise RoutingError(f"{label}.{field} must be a non-empty list")
        allowed = PACKET_TAXONOMY[field]
        for item in accepted:
            if allowed is bool:
                if not isinstance(item, bool):
                    raise RoutingError(f"{label}.{field} values must be boolean")
            elif item not in allowed:
                raise RoutingError(f"{label}.{field} contains unknown value {item!r}")
        if len({json.dumps(item, sort_keys=True) for item in accepted}) != len(accepted):
            raise RoutingError(f"{label}.{field} contains duplicate values")
        normalized[field] = copy.deepcopy(accepted)
    return normalized


def _validate_rules(value: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RoutingError(f"{kind}_rules must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    priorities: list[int] = []
    defaults = 0
    for index, raw in enumerate(value, start=1):
        label = f"{kind}_rules[{index}]"
        rule = _require_object(raw, label)
        _require_exact_keys(rule, {"id", "priority", "match", "effect"}, label)
        rule_id = rule.get("id")
        priority = rule.get("priority")
        if not isinstance(rule_id, str) or not rule_id:
            raise RoutingError(f"{label}.id must be a non-empty string")
        if rule_id in seen_ids:
            raise RoutingError(f"{kind}_rules duplicates id {rule_id}")
        seen_ids.add(rule_id)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise RoutingError(f"{label}.priority must be an integer")
        priorities.append(priority)
        match = _validate_match(rule.get("match"), f"{label}.match")
        defaults += int(not match)
        effect = _require_object(rule.get("effect"), f"{label}.effect")
        if kind == "decision":
            if set(effect) == {"gate"}:
                if effect.get("gate") != "human_gate":
                    raise RoutingError(f"{label}.effect.gate must be human_gate")
            else:
                _require_exact_keys(effect, {"model"}, f"{label}.effect")
                if effect.get("model") not in APPROVED_MODELS:
                    raise RoutingError(f"{label}.effect.model is not approved")
        else:
            _require_exact_keys(
                effect,
                {"minimum_model", "required", "missing_evidence_action"},
                f"{label}.effect",
            )
            if effect.get("minimum_model") not in APPROVED_MODELS:
                raise RoutingError(f"{label}.effect.minimum_model is not approved")
            if not isinstance(effect.get("required"), bool):
                raise RoutingError(f"{label}.effect.required must be boolean")
            if effect.get("missing_evidence_action") not in MISSING_EVIDENCE_ACTIONS:
                raise RoutingError(
                    f"{label}.effect.missing_evidence_action is invalid"
                )
        normalized.append(copy.deepcopy(rule))
    if len(set(priorities)) != len(priorities):
        raise RoutingError(f"{kind}_rules priorities must be unique")
    if priorities != sorted(priorities):
        raise RoutingError(f"{kind}_rules must be stored in ascending priority order")
    if defaults != 1 or normalized[-1]["match"] != {}:
        raise RoutingError(f"{kind}_rules must end with exactly one empty default match")
    return normalized


def validate_policy_snapshot(value: Any) -> dict[str, Any]:
    policy = _require_object(value, "routing policy")
    _require_exact_keys(policy, POLICY_KEYS, "routing policy")
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise RoutingError(f"routing policy.schema_version must be {POLICY_SCHEMA}")
    for key in ("snapshot_id", "policy_id", "adapter_scope"):
        if not isinstance(policy.get(key), str) or not policy.get(key):
            raise RoutingError(f"routing policy.{key} must be a non-empty string")
    if policy.get("policy_id") != CANONICAL_POLICY_ID:
        raise RoutingError(f"routing policy.policy_id must be {CANONICAL_POLICY_ID}")
    if policy.get("policy_version") != CANONICAL_POLICY_VERSION:
        raise RoutingError(
            f"routing policy.policy_version must be {CANONICAL_POLICY_VERSION}"
        )
    if policy.get("adapter_scope") != "codex_builtin_subagents":
        raise RoutingError("routing policy.adapter_scope must be codex_builtin_subagents")
    if policy.get("packet_taxonomy") != _expected_taxonomy_json():
        raise RoutingError("routing policy.packet_taxonomy must match routing-packet.v1")
    if policy.get("default_model") != APPROVED_MODELS[0]:
        raise RoutingError("routing policy.default_model must be gpt-5.6-terra")
    if policy.get("model_order") != list(APPROVED_MODELS):
        raise RoutingError("routing policy.model_order must be Terra then Sol")
    exclusions = _require_object(
        policy.get("automatic_exclusions"), "routing policy.automatic_exclusions"
    )
    _require_exact_keys(
        exclusions,
        {"models", "unclassified_models"},
        "routing policy.automatic_exclusions",
    )
    if set(exclusions.get("models", [])) != EXCLUDED_MODELS:
        raise RoutingError("routing policy must exclude exactly gpt-5.6-luna by model")
    if exclusions.get("unclassified_models") is not True:
        raise RoutingError("routing policy must exclude unclassified models")
    _validate_rules(policy.get("decision_rules"), "decision")
    _validate_rules(policy.get("verifier_rules"), "verifier")
    override = _require_object(policy.get("override_policy"), "routing policy.override_policy")
    _require_exact_keys(
        override,
        {
            "lead_may_raise_model",
            "user_may_raise_model",
            "effort_override_allowed",
            "lower_model_request_action",
        },
        "routing policy.override_policy",
    )
    if override != {
        "lead_may_raise_model": True,
        "user_may_raise_model": True,
        "effort_override_allowed": False,
        "lower_model_request_action": "human_gate",
    }:
        raise RoutingError("routing policy.override_policy violates responsibility routing")
    fallback = _require_object(policy.get("fallback_policy"), "routing policy.fallback_policy")
    _require_exact_keys(
        fallback,
        {"terra_unavailable", "sol_unavailable", "silent_fallback"},
        "routing policy.fallback_policy",
    )
    if fallback.get("terra_unavailable") != "upgrade_to_sol_same_effort":
        raise RoutingError("Terra unavailability must upgrade to Sol at the inherited effort")
    if fallback.get("sol_unavailable") != "human_gate":
        raise RoutingError("Sol unavailability must human_gate")
    if fallback.get("silent_fallback") is not False:
        raise RoutingError("silent fallback must be false")
    attempts = _require_object(policy.get("attempt_policy"), "routing policy.attempt_policy")
    _require_exact_keys(
        attempts,
        {"same_model_retries", "model_changes", "allowed_model_change", "exhausted_action"},
        "routing policy.attempt_policy",
    )
    if attempts.get("same_model_retries") != 1 or attempts.get("model_changes") != 1:
        raise RoutingError("routing attempt caps must be one retry and one model change")
    if attempts.get("allowed_model_change") != "terra_to_sol_same_effort":
        raise RoutingError("routing model changes must be Terra to Sol at the inherited effort")
    if attempts.get("exhausted_action") != "repair_round_or_human_gate":
        raise RoutingError("routing attempt exhausted_action is invalid")
    semantic_digest = canonical_sha256(
        policy, omitted_keys=("snapshot_id", "content_sha256")
    )
    if semantic_digest != CANONICAL_POLICY_SEMANTICS_SHA256:
        raise RoutingError(
            "routing policy semantics do not match the registered responsibility-routing-codex-v2 contract"
        )
    expected_digest = canonical_sha256(policy, omitted_keys=("content_sha256",))
    if policy.get("content_sha256") != expected_digest:
        raise RoutingError("routing policy.content_sha256 does not match canonical content")
    return copy.deepcopy(policy)


def prepare_capability_snapshot(
    value: Any,
    *,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Normalize lead-provided capability input into a canonical immutable snapshot."""

    raw = _require_object(value, "runtime capabilities")
    allowed_input = CAPABILITY_KEYS | {"schema_version"}
    unknown = sorted(set(raw) - allowed_input)
    if unknown:
        raise RoutingError(f"runtime capabilities has unknown keys: {', '.join(unknown)}")
    normalized = copy.deepcopy(raw)
    normalized["schema_version"] = CAPABILITY_SCHEMA
    for model in normalized.get("models", []):
        if isinstance(model, dict):
            # v1 inventories used this field for router-selected effort. In v2
            # it is ignored because the user-session effort is authoritative.
            model.pop("automatic_efforts", None)
    if reasoning_effort is not None:
        normalized["reasoning_effort"] = {
            "source": "user_session",
            "value": reasoning_effort,
            "locked": True,
        }
    normalized.setdefault("snapshot_version", 1)
    normalized.setdefault("snapshot_id", "pending")
    normalized["content_sha256"] = "pending"
    provisional = canonical_sha256(normalized, omitted_keys=("content_sha256",))
    if normalized["snapshot_id"] == "pending":
        normalized["snapshot_id"] = "runtime-capabilities-" + provisional.split(":", 1)[1][:16]
    normalized["content_sha256"] = canonical_sha256(
        normalized, omitted_keys=("content_sha256",)
    )
    return validate_capability_snapshot(normalized)


def validate_capability_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _require_object(value, "runtime capabilities")
    _require_exact_keys(snapshot, CAPABILITY_KEYS, "runtime capabilities")
    if snapshot.get("schema_version") != CAPABILITY_SCHEMA:
        raise RoutingError(f"runtime capabilities.schema_version must be {CAPABILITY_SCHEMA}")
    if snapshot.get("adapter") != "codex_builtin_subagents":
        raise RoutingError("runtime capabilities.adapter must be codex_builtin_subagents")
    if snapshot.get("dispatch_surface") != "multi_agent_v1":
        raise RoutingError("runtime capabilities.dispatch_surface must be multi_agent_v1")
    for key in ("snapshot_id", "observed_at", "source"):
        if not isinstance(snapshot.get(key), str) or not snapshot.get(key):
            raise RoutingError(f"runtime capabilities.{key} must be a non-empty string")
    parse_rfc3339(snapshot.get("observed_at"), "runtime capabilities.observed_at")
    inherited_effort(snapshot)
    if not isinstance(snapshot.get("snapshot_version"), int) or snapshot["snapshot_version"] < 1:
        raise RoutingError("runtime capabilities.snapshot_version must be an integer >= 1")
    models = snapshot.get("models")
    if not isinstance(models, list) or not models:
        raise RoutingError("runtime capabilities.models must be a non-empty list")
    seen: set[str] = set()
    for index, raw_model in enumerate(models, start=1):
        label = f"runtime capabilities.models[{index}]"
        model = _require_object(raw_model, label)
        _require_exact_keys(
            model,
            {"id", "available", "classification", "supported_efforts"},
            label,
        )
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise RoutingError(f"{label}.id must be a non-empty string")
        if model_id in seen:
            raise RoutingError(f"runtime capabilities duplicates model {model_id}")
        seen.add(model_id)
        if not isinstance(model.get("available"), bool):
            raise RoutingError(f"{label}.available must be boolean")
        if model.get("classification") not in CLASSIFICATIONS:
            raise RoutingError(f"{label}.classification is invalid")
        efforts = model.get("supported_efforts")
        if not isinstance(efforts, list) or not all(isinstance(item, str) for item in efforts):
            raise RoutingError(f"{label}.supported_efforts must be a string list")
        if len(set(efforts)) != len(efforts):
            raise RoutingError(f"{label}.supported_efforts contains duplicates")
        if not set(efforts).issubset(REASONING_EFFORTS):
            raise RoutingError(f"{label}.supported_efforts contains an unknown effort")
    expected_digest = canonical_sha256(snapshot, omitted_keys=("content_sha256",))
    if snapshot.get("content_sha256") != expected_digest:
        raise RoutingError("runtime capabilities.content_sha256 does not match canonical content")
    return copy.deepcopy(snapshot)


def _model_record(capabilities: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    for model in capabilities["models"]:
        if model["id"] == model_id:
            return model
    return None


def route_is_eligible(capabilities: dict[str, Any], route: dict[str, str]) -> bool:
    if route_key(route) not in APPROVED_MODELS:
        return False
    if route["effort"] != inherited_effort(capabilities):
        return False
    model = _model_record(capabilities, route["model"])
    if not model or model["available"] is not True:
        return False
    if model["classification"] not in {"general", "deep"}:
        return False
    return route["effort"] in model["supported_efforts"]


def route_rank(policy: dict[str, Any], route: dict[str, str]) -> int:
    key = route_key(route)
    for index, item in enumerate(policy["model_order"]):
        if item == key:
            return index
    raise RoutingError(f"model {key} is not in policy.model_order")


def _matches(match: dict[str, list[Any]], facts: dict[str, Any]) -> bool:
    return all(facts[field] in accepted for field, accepted in match.items())


def _first_match(rules: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    for rule in rules:
        if _matches(rule["match"], facts):
            return rule
    raise RoutingError("ordered rules have no matching default")


def high_guard_triggered(facts: dict[str, Any]) -> bool:
    return bool(
        facts["claim_class"] == "high_consequence"
        or facts["blast_radius"] == "external_production"
        or facts["reversibility"] == "hard"
        or facts["verifiability"] == "judgment_only"
        or facts["approval_required"] is True
    )


def normalize_request(
    value: Any,
    *,
    required_effort: str | None = None,
) -> dict[str, Any]:
    request = _require_object(value, "routing request")
    _require_exact_keys(
        request,
        {"source", "requested_route", "reason", "evidence_refs"},
        "routing request",
    )
    source = request.get("source")
    if source not in REQUEST_SOURCES:
        raise RoutingError(f"routing request.source must be one of {sorted(REQUEST_SOURCES)}")
    reason = request.get("reason")
    refs = request.get("evidence_refs")
    if not isinstance(reason, str):
        raise RoutingError("routing request.reason must be a string")
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        raise RoutingError("routing request.evidence_refs must be a string list")
    if len(set(refs)) != len(refs):
        raise RoutingError("routing request.evidence_refs contains duplicates")
    for index, ref in enumerate(refs, start=1):
        _parse_evidence_ref(ref, f"routing request.evidence_refs[{index}]")
    requested = request.get("requested_route")
    if source == "automatic":
        if requested is not None or reason or refs:
            raise RoutingError("automatic routing request cannot include override fields")
    else:
        requested = _route(requested, "routing request.requested_route")
        if route_key(requested) not in APPROVED_MODELS:
            raise RoutingError("routing request.requested_route model is not approved")
        if required_effort is not None and requested["effort"] != required_effort:
            raise RoutingError(
                "routing request cannot override the workflow-inherited reasoning effort"
            )
        if not reason.strip() or not refs:
            raise RoutingError("lead/user route requests require reason and evidence_refs")
    return {
        "source": source,
        "requested_route": copy.deepcopy(requested),
        "reason": reason,
        "evidence_refs": copy.deepcopy(refs),
    }


def evaluate_route(
    policy: dict[str, Any],
    capabilities: dict[str, Any],
    facts: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    policy = validate_policy_snapshot(policy)
    capabilities = validate_capability_snapshot(capabilities)
    facts = validate_packet_facts(facts)
    effort = inherited_effort(capabilities)
    request = normalize_request(request, required_effort=effort)
    rule = _first_match(policy["decision_rules"], facts)
    effect = rule["effect"]
    floor = materialize_route(str(policy["default_model"]), capabilities)
    if effect.get("gate") == "human_gate":
        return {
            "matched_rule_id": rule["id"],
            "minimum": floor,
            "selected": None,
            "override": {"applied": False, "direction": "none"},
            "status": "human_gate",
        }
    minimum = materialize_route(str(effect["model"]), capabilities)
    candidate = copy.deepcopy(minimum)
    override = {"applied": False, "direction": "none"}
    if request["source"] in {"lead", "user"}:
        requested = request["requested_route"]
        assert isinstance(requested, dict)
        if route_rank(policy, requested) < route_rank(policy, minimum):
            return {
                "matched_rule_id": rule["id"],
                "minimum": minimum,
                "selected": None,
                "override": {"applied": False, "direction": "lower_rejected"},
                "status": "human_gate",
            }
        candidate = copy.deepcopy(requested)
        override = {"applied": route_key(candidate) != route_key(minimum), "direction": "raise"}
    if route_is_eligible(capabilities, candidate):
        return {
            "matched_rule_id": rule["id"],
            "minimum": minimum,
            "selected": candidate,
            "override": override,
            "status": "planned",
        }
    if high_guard_triggered(facts):
        return {
            "matched_rule_id": rule["id"],
            "minimum": minimum,
            "selected": None,
            "override": override,
            "status": "human_gate",
        }
    start = route_rank(policy, candidate)
    for model in policy["model_order"][start + 1 :]:
        route = materialize_route(str(model), capabilities)
        if route_is_eligible(capabilities, route):
            fallback_override = copy.deepcopy(override)
            fallback_override["capability_fallback_from"] = candidate
            return {
                "matched_rule_id": rule["id"],
                "minimum": minimum,
                "selected": copy.deepcopy(route),
                "override": fallback_override,
                "status": "planned",
            }
    return {
        "matched_rule_id": rule["id"],
        "minimum": minimum,
        "selected": None,
        "override": override,
        "status": "human_gate",
    }


def evaluate_verifier_floor(
    policy: dict[str, Any],
    capabilities: dict[str, Any],
    facts: dict[str, Any],
) -> dict[str, Any]:
    policy = validate_policy_snapshot(policy)
    capabilities = validate_capability_snapshot(capabilities)
    facts = validate_packet_facts(facts)
    rule = _first_match(policy["verifier_rules"], facts)
    effect = rule["effect"]
    return {
        "rule_id": rule["id"],
        "required": effect["required"],
        "minimum_route": materialize_route(str(effect["minimum_model"]), capabilities),
        "verifier_lane_ids": [],
        "independent_of_lane_ids": [],
        "required_evidence": [],
        "missing_evidence_action": effect["missing_evidence_action"],
    }


def build_planned_decision(
    policy: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    packet_id: str,
    decision_id: str,
    facts: dict[str, Any],
    request: dict[str, Any] | None = None,
    verifier_lane_ids: list[str] | None = None,
    independent_of_lane_ids: list[str] | None = None,
    required_evidence: list[str] | None = None,
) -> dict[str, Any]:
    request = request or {
        "source": "automatic",
        "requested_route": None,
        "reason": "",
        "evidence_refs": [],
    }
    result = evaluate_route(policy, capabilities, facts, request)
    floor = evaluate_verifier_floor(policy, capabilities, facts)
    floor["verifier_lane_ids"] = list(verifier_lane_ids or [])
    floor["independent_of_lane_ids"] = list(independent_of_lane_ids or [])
    floor["required_evidence"] = list(required_evidence or [])
    decision = {
        "schema_version": DECISION_SCHEMA,
        "packet_id": packet_id,
        "decision_id": decision_id,
        "decision_sha256": "pending",
        "policy_snapshot": {
            "snapshot_id": policy["snapshot_id"],
            "content_sha256": policy["content_sha256"],
        },
        "capability_snapshot": {
            "snapshot_id": capabilities["snapshot_id"],
            "content_sha256": capabilities["content_sha256"],
        },
        "facts": copy.deepcopy(facts),
        "request": normalize_request(
            request,
            required_effort=inherited_effort(capabilities),
        ),
        "matched_rule_id": result["matched_rule_id"],
        "minimum": result["minimum"],
        "selected": result["selected"],
        "override": result["override"],
        "verification_floor": floor,
        "status": result["status"],
    }
    decision["decision_sha256"] = canonical_sha256(
        decision, omitted_keys=("decision_sha256",)
    )
    return decision


def draft_decision(
    policy: dict[str, Any], capabilities: dict[str, Any], lane_id: str, role: str
) -> dict[str, Any]:
    return {
        "schema_version": DECISION_SCHEMA,
        "packet_id": f"packet-{lane_id}",
        "decision_id": f"decision-{lane_id}",
        "decision_sha256": None,
        "policy_snapshot": {
            "snapshot_id": policy["snapshot_id"],
            "content_sha256": policy["content_sha256"],
        },
        "capability_snapshot": {
            "snapshot_id": capabilities["snapshot_id"],
            "content_sha256": capabilities["content_sha256"],
        },
        "facts": {
            "ambiguity": None,
            "coupling": None,
            "blast_radius": None,
            "reversibility": None,
            "verifiability": None,
            "novelty": None,
            "role": role,
            "claim_class": None,
            "approval_required": None,
        },
        "request": {
            "source": "automatic",
            "requested_route": None,
            "reason": "",
            "evidence_refs": [],
        },
        "matched_rule_id": None,
        "minimum": None,
        "selected": None,
        "override": None,
        "verification_floor": None,
        "status": "draft",
    }


def validate_draft_decision(
    value: Any, policy: dict[str, Any], capabilities: dict[str, Any]
) -> dict[str, Any]:
    decision = _require_object(value, "routing decision")
    _require_exact_keys(decision, DECISION_KEYS, "routing decision")
    if decision.get("schema_version") != DECISION_SCHEMA:
        raise RoutingError(f"routing decision.schema_version must be {DECISION_SCHEMA}")
    if decision.get("status") != "draft":
        raise RoutingError("scaffold routing decision must be draft or a complete decision")
    for key in ("packet_id", "decision_id"):
        if not isinstance(decision.get(key), str) or not decision.get(key):
            raise RoutingError(f"routing decision.{key} must be a non-empty string")
    for key, snapshot in (
        ("policy_snapshot", policy),
        ("capability_snapshot", capabilities),
    ):
        ref = _require_object(decision.get(key), f"routing decision.{key}")
        _require_exact_keys(ref, {"snapshot_id", "content_sha256"}, f"routing decision.{key}")
        if ref != {
            "snapshot_id": snapshot["snapshot_id"],
            "content_sha256": snapshot["content_sha256"],
        }:
            raise RoutingError(f"routing decision.{key} does not match its snapshot")
    facts = _require_object(decision.get("facts"), "routing decision.facts")
    _require_exact_keys(facts, set(PACKET_TAXONOMY), "routing decision.facts")
    if facts.get("role") not in PACKET_TAXONOMY["role"]:
        raise RoutingError("routing decision.facts.role is invalid")
    for field in set(PACKET_TAXONOMY) - {"role"}:
        if facts.get(field) is not None:
            raise RoutingError(f"draft routing decision.facts.{field} must be null")
    if any(
        decision.get(key) is not None
        for key in (
            "decision_sha256",
            "matched_rule_id",
            "minimum",
            "selected",
            "override",
            "verification_floor",
        )
    ):
        raise RoutingError("draft routing decision cannot contain computed route fields")
    normalize_request(decision.get("request"))
    return copy.deepcopy(decision)


def _validate_verification_floor(
    value: Any, expected: dict[str, Any]
) -> dict[str, Any]:
    floor = _require_object(value, "routing decision.verification_floor")
    keys = {
        "rule_id",
        "required",
        "minimum_route",
        "verifier_lane_ids",
        "independent_of_lane_ids",
        "required_evidence",
        "missing_evidence_action",
    }
    _require_exact_keys(floor, keys, "routing decision.verification_floor")
    for key in ("verifier_lane_ids", "independent_of_lane_ids", "required_evidence"):
        items = floor.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item for item in items
        ):
            raise RoutingError(f"routing decision.verification_floor.{key} must be a string list")
        if len(set(items)) != len(items):
            raise RoutingError(f"routing decision.verification_floor.{key} has duplicates")
        if key == "required_evidence" and any(len(item.strip()) < 4 for item in items):
            raise RoutingError(
                "routing decision.verification_floor.required_evidence names must be substantive"
            )
    for key in ("rule_id", "required", "minimum_route", "missing_evidence_action"):
        if floor.get(key) != expected.get(key):
            raise RoutingError(f"routing decision.verification_floor.{key} was not recomputed")
    if expected.get("required") is True:
        for key in ("verifier_lane_ids", "independent_of_lane_ids", "required_evidence"):
            if not floor[key]:
                raise RoutingError(
                    f"routing decision.verification_floor.{key} is required before dispatch"
                )
    return copy.deepcopy(floor)


def validate_planned_decision(
    value: Any,
    policy: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    allow_draft: bool = False,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    decision = _require_object(value, "routing decision")
    if allow_draft and decision.get("status") == "draft":
        return validate_draft_decision(decision, policy, capabilities)
    _require_exact_keys(decision, DECISION_KEYS, "routing decision")
    if decision.get("schema_version") != DECISION_SCHEMA:
        raise RoutingError(f"routing decision.schema_version must be {DECISION_SCHEMA}")
    if decision.get("status") not in DECISION_STATUSES:
        raise RoutingError(f"routing decision.status must be one of {sorted(DECISION_STATUSES)}")
    for key in ("packet_id", "decision_id"):
        if not isinstance(decision.get(key), str) or not decision.get(key):
            raise RoutingError(f"routing decision.{key} must be a non-empty string")
    refs = (
        ("policy_snapshot", policy),
        ("capability_snapshot", capabilities),
    )
    for key, snapshot in refs:
        ref = _require_object(decision.get(key), f"routing decision.{key}")
        _require_exact_keys(ref, {"snapshot_id", "content_sha256"}, f"routing decision.{key}")
        expected_ref = {
            "snapshot_id": snapshot["snapshot_id"],
            "content_sha256": snapshot["content_sha256"],
        }
        if ref != expected_ref:
            raise RoutingError(f"routing decision.{key} does not match its snapshot")
    facts = validate_packet_facts(decision.get("facts"))
    request = normalize_request(
        decision.get("request"),
        required_effort=inherited_effort(capabilities),
    )
    if request["source"] in {"lead", "user"}:
        validate_evidence_refs(
            request["evidence_refs"],
            evidence_root=evidence_root,
            label="routing request.evidence_refs",
        )
    recomputed = evaluate_route(policy, capabilities, facts, request)
    for key in ("matched_rule_id", "minimum", "selected", "override", "status"):
        if decision.get(key) != recomputed.get(key):
            raise RoutingError(f"routing decision.{key} does not match first-match evaluation")
    expected_floor = evaluate_verifier_floor(policy, capabilities, facts)
    _validate_verification_floor(decision.get("verification_floor"), expected_floor)
    expected_digest = canonical_sha256(decision, omitted_keys=("decision_sha256",))
    if decision.get("decision_sha256") != expected_digest:
        raise RoutingError("routing decision.decision_sha256 does not match canonical content")
    return copy.deepcopy(decision)


def _next_eligible_route(
    policy: dict[str, Any], capabilities: dict[str, Any], route: dict[str, str]
) -> dict[str, str] | None:
    start = route_rank(policy, route)
    for model in policy["model_order"][start + 1 :]:
        candidate = materialize_route(str(model), capabilities)
        if route_is_eligible(capabilities, candidate):
            return copy.deepcopy(candidate)
    return None


def validate_attempts(
    record: Any,
    decision: dict[str, Any],
    policy: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    evidence_root: Path | None = None,
    require_completed_terminal: bool = False,
) -> dict[str, Any]:
    record = _require_object(record, "runner lane record")
    decision = validate_planned_decision(
        decision,
        policy,
        capabilities,
        evidence_root=evidence_root,
    )
    if decision["status"] != "planned" or decision["selected"] is None:
        raise RoutingError("human-gated or blocked decisions cannot have dispatch attempts")
    for key in ("decision_id", "planned_decision_sha256", "terminal_attempt_id", "attempts"):
        if key not in record:
            raise RoutingError(f"runner lane record missing key: {key}")
    if record["decision_id"] != decision["decision_id"]:
        raise RoutingError("runner lane record.decision_id does not match planned decision")
    if record["planned_decision_sha256"] != decision["decision_sha256"]:
        raise RoutingError("runner lane record.planned_decision_sha256 does not match decision")
    attempts = record.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RoutingError("runner lane record.attempts must be a non-empty append-only list")
    seen_ids: set[str] = set()
    retries = 0
    model_changes = 0
    previous: dict[str, Any] | None = None
    required_attempt_keys = {
        "attempt_id",
        "ordinal",
        "transition",
        "parent_attempt_id",
        "decision_id",
        "planned_decision_sha256",
        "route",
        "actual_route",
        "outcome",
        "failure_class",
        "evidence_refs",
        "lifecycle",
    }
    lifecycle_keys = {
        "execution_kind",
        "evidence_level",
        "agent_id",
        "native_handle",
        "spawn_tool",
        "wait_status",
        "close_status",
        "output_path",
    }
    for index, raw in enumerate(attempts, start=1):
        label = f"runner lane record.attempts[{index}]"
        attempt = _require_object(raw, label)
        _require_exact_keys(attempt, required_attempt_keys, label)
        attempt_id = attempt.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise RoutingError(f"{label}.attempt_id must be a non-empty string")
        if attempt_id in seen_ids:
            raise RoutingError(f"runner lane record duplicates attempt_id {attempt_id}")
        seen_ids.add(attempt_id)
        if attempt.get("ordinal") != index:
            raise RoutingError(f"{label}.ordinal must be contiguous from 1")
        transition = attempt.get("transition")
        if transition not in TRANSITIONS:
            raise RoutingError(f"{label}.transition must be one of {sorted(TRANSITIONS)}")
        if attempt.get("decision_id") != decision["decision_id"]:
            raise RoutingError(f"{label}.decision_id does not match planned decision")
        if attempt.get("planned_decision_sha256") != decision["decision_sha256"]:
            raise RoutingError(f"{label}.planned_decision_sha256 does not match decision")
        route = _route(attempt.get("route"), f"{label}.route")
        if route_key(route) not in APPROVED_MODELS or not route_is_eligible(capabilities, route):
            raise RoutingError(f"{label}.route is not eligible in the capability snapshot")
        if route["effort"] != inherited_effort(capabilities):
            raise RoutingError(f"{label}.route must preserve the workflow-inherited effort")
        outcome = attempt.get("outcome")
        failure_class = attempt.get("failure_class")
        if outcome not in OUTCOMES:
            raise RoutingError(f"{label}.outcome must be one of {sorted(OUTCOMES)}")
        if outcome == "completed":
            if failure_class is not None:
                raise RoutingError(f"{label}.failure_class must be null when completed")
            if attempt.get("actual_route") != route:
                raise RoutingError(f"{label}.actual_route must equal dispatched route when completed")
        else:
            if failure_class not in FAILURE_CLASSES:
                raise RoutingError(f"{label}.failure_class must classify a non-completed attempt")
            actual = attempt.get("actual_route")
            if actual is not None and _route(actual, f"{label}.actual_route") != route:
                raise RoutingError(f"{label}.actual_route silently substituted the dispatched route")
        if outcome == "unavailable" and failure_class != "route_unavailable":
            raise RoutingError(f"{label} unavailable outcome requires route_unavailable")
        if failure_class == "route_unavailable" and outcome != "unavailable":
            raise RoutingError(f"{label} route_unavailable requires outcome=unavailable")
        refs = attempt.get("evidence_refs")
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise RoutingError(f"{label}.evidence_refs must be a string list")
        if refs:
            validate_evidence_refs(
                refs,
                evidence_root=evidence_root,
                label=f"{label}.evidence_refs",
            )
        lifecycle = _require_object(attempt.get("lifecycle"), f"{label}.lifecycle")
        _require_exact_keys(lifecycle, lifecycle_keys, f"{label}.lifecycle")
        if not (lifecycle.get("agent_id") or lifecycle.get("native_handle")):
            raise RoutingError(f"{label}.lifecycle needs agent_id or native_handle")
        if lifecycle.get("evidence_level") != "lead_recorded":
            raise RoutingError(f"{label}.lifecycle.evidence_level must be lead_recorded")
        if lifecycle.get("execution_kind") != "lead_recorded_native":
            raise RoutingError(f"{label}.lifecycle.execution_kind must be lead_recorded_native")
        if lifecycle.get("spawn_tool") != "multi_agent_v1":
            raise RoutingError(f"{label}.lifecycle.spawn_tool must be multi_agent_v1")
        if index == 1:
            if transition != "initial" or attempt.get("parent_attempt_id") is not None:
                raise RoutingError("first routing attempt must be initial with no parent")
            if route != decision["selected"]:
                raise RoutingError("initial attempt route must equal the immutable planned selection")
        else:
            assert previous is not None
            previous_outcome = previous.get("outcome")
            if previous_outcome in {"completed", "blocked"}:
                raise RoutingError(
                    f"{previous_outcome} attempt must be terminal and cannot transition"
                )
            if attempt.get("parent_attempt_id") != previous["attempt_id"]:
                raise RoutingError(f"{label}.parent_attempt_id must point to the preceding attempt")
            previous_route = _route(previous["route"])
            previous_failure = previous.get("failure_class")
            if transition == "retry":
                retries += 1
                if route != previous_route:
                    raise RoutingError("retry must keep the same route")
                if previous_outcome != "failed":
                    raise RoutingError("retry must follow outcome=failed")
                if previous_failure not in {"context_failure", "tool_failure"}:
                    raise RoutingError("retry must follow context_failure or tool_failure")
            elif transition == "fallback":
                model_changes += 1
                if previous_outcome != "unavailable":
                    raise RoutingError("fallback must follow outcome=unavailable")
                if previous_failure != "route_unavailable":
                    raise RoutingError("fallback must follow route_unavailable")
                expected_fallback = _next_eligible_route(policy, capabilities, previous_route)
                if expected_fallback is None or route != expected_fallback:
                    raise RoutingError("fallback must upgrade Terra to Sol at the inherited effort")
            elif transition == "escalation":
                model_changes += 1
                if previous_outcome != "failed":
                    raise RoutingError("escalation must follow outcome=failed")
                if previous_failure != "insufficient_reasoning":
                    raise RoutingError("escalation must follow insufficient_reasoning")
                if (
                    previous_route["model"] != "gpt-5.6-terra"
                    or route["model"] != "gpt-5.6-sol"
                    or route["effort"] != previous_route["effort"]
                ):
                    raise RoutingError(
                        "escalation must change Terra to Sol without changing effort"
                    )
                validate_evidence_refs(
                    previous.get("evidence_refs"),
                    evidence_root=evidence_root,
                    label="escalation evidence_refs",
                    bound_attempt_id=str(previous["attempt_id"]),
                )
            else:
                raise RoutingError("only the first attempt may use transition=initial")
        previous = attempt
    if retries > policy["attempt_policy"]["same_model_retries"]:
        raise RoutingError("same-route retry cap exceeded")
    if model_changes > policy["attempt_policy"]["model_changes"]:
        raise RoutingError("model-change cap exceeded")
    terminal = attempts[-1]
    if record.get("terminal_attempt_id") != terminal["attempt_id"]:
        raise RoutingError("terminal_attempt_id must point to the final append-only attempt")
    if require_completed_terminal and terminal.get("outcome") != "completed":
        raise RoutingError("terminal outcome must be completed in final mode")
    for key, value in terminal["lifecycle"].items():
        if record.get(key) != value:
            raise RoutingError(f"runner lane record.{key} must project terminal attempt lifecycle")
    return copy.deepcopy(record)


def expected_swarm_projection(
    decision: dict[str, Any], record: dict[str, Any] | None
) -> dict[str, Any]:
    attempts = record.get("attempts", []) if isinstance(record, dict) else []
    terminal = attempts[-1] if attempts else None
    actual = terminal.get("actual_route") if isinstance(terminal, dict) else None
    if isinstance(terminal, dict) and terminal.get("outcome") != "completed":
        actual = None
    return {
        "packet_id": decision["packet_id"],
        "decision_id": decision["decision_id"],
        "planned_route": copy.deepcopy(decision["selected"]),
        "terminal_actual_route": copy.deepcopy(actual),
        "effort_source": "user_session",
        "route_status": (
            terminal.get("outcome") if isinstance(terminal, dict) else decision["status"]
        ),
        "attempt_count": len(attempts),
    }


def load_policy_template(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"cannot load routing policy template {path}: {exc}") from exc
    return validate_policy_snapshot(value)
