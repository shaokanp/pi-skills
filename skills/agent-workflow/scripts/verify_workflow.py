#!/usr/bin/env python3
"""Check that an Agent Workflow run workspace is structurally auditable."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from clean_orchestrator import (
    CLEAN_RUNTIME_SCHEMA,
    CleanOrchestratorError,
    validate_clean_runtime_contract,
    validate_completion_density,
)
from execution_efficiency import (
    ExecutionEfficiencyError,
    RECEIPT_PAYLOAD_KEYS,
    build_integration_index,
    receipt_relative_path,
    validate_agent_execution_evidence,
    validate_execution_policy,
    validate_lane_receipt,
    validate_orchestration_efficiency,
    validate_wait_telemetry,
)
from model_routing import (
    CAPABILITY_SCHEMA,
    RoutingError,
    expected_swarm_projection,
    route_rank,
    validate_attempts,
    validate_capability_availability_evidence,
    validate_capability_snapshot,
    validate_planned_decision,
    validate_policy_snapshot,
)
from runtime_harness import RuntimeHarnessError, validate_artifact as validate_runtime_observations
from render_swarm_card import (
    CARD_SCHEMA,
    LEGACY_CARD_SCHEMA,
    SwarmCardError,
    validate_card,
)
from token_accounting import TOKEN_USAGE_SCHEMA, validate_v2


TERMINAL_PROJECTIONS = (
    "token-usage.json",
    "token-evidence.json",
    "runtime-observations.json",
    "runner-evidence.json",
    "final-report.md",
)


def validate_terminal_commit_manifest(
    workflow_dir: Path,
    failures: list[str],
    mode: str,
    *,
    required: bool = True,
) -> None:
    if mode != "final":
        return
    path = workflow_dir / "terminal-commit.json"
    if not path.is_file():
        if required:
            failures.append("Missing terminal-commit.json manifest-last revision")
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"Invalid terminal-commit.json: {exc}")
        return
    if not isinstance(value, dict):
        failures.append("terminal-commit.json must be an object")
        return
    if value.get("schema_version") != "agent-workflow.terminal-commit.v1":
        failures.append("terminal-commit.json schema_version is invalid")
    if value.get("status") != "committed":
        failures.append("terminal-commit.json status must be committed")
    projections = value.get("projections")
    if not isinstance(projections, dict) or set(projections) != set(TERMINAL_PROJECTIONS):
        failures.append("terminal-commit.json must bind exactly the five terminal projections")
        return
    actual: dict[str, str] = {}
    for name in TERMINAL_PROJECTIONS:
        projection = workflow_dir / name
        if not projection.is_file():
            failures.append(f"terminal-commit projection is missing: {name}")
            continue
        actual[name] = "sha256:" + hashlib.sha256(projection.read_bytes()).hexdigest()
        if projections.get(name) != actual[name]:
            failures.append(f"terminal-commit mixed revision detected: {name}")
    if len(actual) == len(TERMINAL_PROJECTIONS):
        revision = "sha256:" + hashlib.sha256(
            json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if value.get("revision") != revision:
            failures.append("terminal-commit revision digest mismatch")


REQUIRED_V1_FILES = (
    "plan.md",
    "state.json",
    "token-usage.json",
    "orchestration.md",
    "orchestration.json",
    "final-report.md",
)

LEGACY_REQUIRED_FILES = ("plan.md", "state.json", "orchestration.md", "final-report.md")
LEGACY_REQUIRED_DIRS = ("packets", "results")

ALLOWED_LANES = {
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
}

LEGACY_LANES = {"integrate"}

STRICT_MODES = {"planned", "executed", "final"}
EXECUTION_MODES = {"executed", "final"}

LANE_STATUSES = {"pending", "running", "complete", "skipped", "blocked", "invalid_output"}
SEVERITIES = {"P0", "P1", "P2", "P3"}
GATE_DECISIONS = {
    "pending",
    "pass",
    "revise",
    "more_discovery",
    "challenge",
    "second_opinion",
    "human_gate",
    "blocked",
}
WORKFLOW_STATUSES = {
    "planned",
    "orchestrated",
    "running",
    "integrating",
    "verifying",
    "passed",
    "revising",
    "blocked",
    "complete",
    "abandoned",
}
INTEGRATION_STATUSES = {"pending", "running", "complete", "revise", "blocked", "invalid_output"}
PASS_STOP_REASONS = {"verify_pass", "pass"}
TERMINAL_STOP_REASONS = PASS_STOP_REASONS | {"human_gate", "blocked", "round_budget_exhausted"}
REVISION_STOP_REASONS = {"revise"}
NON_CURRENT_TERMINAL_ROUND_STATUSES = {"passed", "revising", "complete", "blocked"}
NON_CURRENT_TERMINAL_INTEGRATION_STATUSES = {"complete", "revise", "blocked"}
LEAD_OWNED_LANES = {"implement", "repair"}
RUNNER_MODES = {
    "codex_builtin_subagents",
    "claude_code_builtin_subagents",
    "manual_simulation",
}
DISPATCH_SURFACES = {
    "multi_agent_v1",
    "claude_code_agent_tool",
    "none",
}
RUNNER_DISPATCH_SURFACE = {
    "codex_builtin_subagents": "multi_agent_v1",
    "claude_code_builtin_subagents": "claude_code_agent_tool",
    "manual_simulation": "none",
}
RUNNER_LANE_DISPATCH_METHOD = {
    "codex_builtin_subagents": "spawn_agent",
    "claude_code_builtin_subagents": "Agent",
    "manual_simulation": "simulate_in_main_thread",
}
RUNNER_EVIDENCE_LEVELS = {"lead_recorded", "tool_event_verified"}
TRUSTED_RUNNER_EVENT_CAPTURE_SOURCES = {
    "codex_runtime_tool_events",
    "claude_code_runtime_tool_events",
    "multi_agent_v1_runtime_events",
    "claude_code_agent_tool_runtime_events",
}

PAYLOAD_REQUIRED_KEYS = RECEIPT_PAYLOAD_KEYS

PASS_DECISIONS = {"pass"}
HIGH_SEVERITIES = {"P0", "P1", "P2"}
BLOCKING_SEVERITIES = {"P0", "P1"}
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
FINAL_REPORT_MIN_WORDS = 80
FINAL_REPORT_MIN_SECTION_WORDS = 5
FINAL_REPORT_REQUIRED_SECTIONS = (
    ("outcome", "result", "summary", "完成", "結果", "摘要"),
    ("verification", "verify", "驗證"),
    ("risk", "remaining risk", "風險"),
    ("token", "tokens", "token usage", "消耗"),
    ("stop", "gate", "終止", "停止"),
    ("runner", "workflow shape", "agent execution", "native", "manual", "執行"),
)
TOKEN_USAGE_SOURCES = {
    "runtime_reported",
    "runner_reported",
    "lead_estimated",
    "manual_estimate",
}
TOKEN_USAGE_CONFIDENCE = {"exact", "estimated"}
TOKEN_USAGE_STATUSES = {"pending", "complete"}
FINAL_REPORT_EVIDENCE_ANCHORS = (
    "round-",
    "lane-runs/",
    "runner-evidence.json",
    "verify_workflow.py",
    "--mode final",
    "multi_agent_v1",
)
FINAL_REPORT_RESULT_TERMS = (
    "pass",
    "passed",
    "exited 0",
    "exit=0",
    "failed",
    "exited 1",
    "exit=1",
    "rejected",
    "records",
    "recorded",
    "completed",
    "closed",
    "通過",
    "失敗",
    "拒絕",
    "記錄",
)
FINAL_REPORT_COMMAND_TERMS = (
    "python",
    "verify_workflow.py",
    "scripts/",
    "validate-skill",
    "git diff",
    "--mode",
)
LOW_INFORMATION_EVIDENCE = {
    "ok",
    "okay",
    "pass",
    "passed",
    "true",
    "done",
    "yes",
    "works",
    "fine",
    "n/a",
    "na",
    "none",
    "success",
}
EVIDENCE_CONCRETE_TERMS = (
    "exited",
    "exit=",
    "exit code",
    "rounds/",
    "lane-runs/",
    "fixtures/",
    "runner-evidence.json",
    "final-report.md",
    "state.json",
    "orchestration.json",
    "verify_workflow.py",
    "--mode",
    "fixture",
    "python3 ",
    "scripts/",
    "git diff --check",
    "py_compile",
    "rejected",
    "failed",
)
AUDITABLE_EVIDENCE_PATH_RE = re.compile(
    r"("
    r"rounds/|lane-runs/|fixtures/|"
    r"runner-evidence\.json|state\.json|orchestration\.json|"
    r"final-report\.md|integration\.json|"
    r"\.claude/skills/|references/[A-Za-z0-9_.-]+\.md|"
    r"scripts/[A-Za-z0-9_.-]+\.py|agents/[A-Za-z0-9_.-]+\.ya?ml|"
    r"changelog/[A-Za-z0-9_.-]+\.md"
    r")"
)
WORKFLOW_AUDITABLE_REF_RE = re.compile(
    r"(runner-evidence\.json|state\.json|orchestration\.json|final-report\.md|"
    r"rounds/round-[0-9]{3}/lane-runs/[A-Za-z0-9_.-]+\.json|"
    r"rounds/round-[0-9]{3}/integration\.json|"
    r"rounds/round-[0-9]{3}/fixtures/[A-Za-z0-9_./-]+)"
)
SOURCE_AUDITABLE_REF_RE = re.compile(
    r"(\.claude/skills/[A-Za-z0-9_./-]+|"
    r"references/[A-Za-z0-9_.-]+\.md|"
    r"scripts/[A-Za-z0-9_.-]+\.(?:py|sh|mjs|js)|"
    r"agents/[A-Za-z0-9_.-]+\.ya?ml|"
    r"changelog/[A-Za-z0-9_.-]+\.md)"
)
VACUOUS_EVIDENCE_PHRASES = (
    "all checks passed",
    "checks passed",
    "passed successfully",
    "without issue",
    "without issues",
    "with no issues",
    "no issues",
    "looks good",
    "worked fine",
    "meaningful evidence",
)
FINAL_REPORT_REF_RE = re.compile(
    r"(runner-evidence\.json|state\.json|orchestration\.json|"
    r"rounds/round-[0-9]{3}/lane-runs/[A-Za-z0-9_.-]+\.json|"
    r"rounds/round-[0-9]{3}/integration\.json|"
    r"rounds/round-[0-9]{3}/fixtures/[A-Za-z0-9_./-]+)"
)


def load_json(path: Path, failures: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"Invalid JSON in {path}: {exc}")
    except OSError as exc:
        failures.append(f"Cannot read {path}: {exc}")
    return None


def require_nonempty_file(path: Path, failures: list[str]) -> None:
    if not path.is_file():
        failures.append(f"Missing file: {path}")
        return
    if not path.read_text(encoding="utf-8").strip():
        failures.append(f"Empty file: {path}")


def require_dir(path: Path, failures: list[str]) -> None:
    if not path.is_dir():
        failures.append(f"Missing directory: {path}")


def require_keys(value: Any, keys: tuple[str, ...], label: str, failures: list[str]) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label} must be an object")
        return
    for key in keys:
        if key not in value:
            failures.append(f"Missing {label} key: {key}")


def is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip().lower()
    return not stripped or stripped == "todo" or stripped.startswith("todo:")


def has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not is_placeholder(value)


def word_count(value: str) -> int:
    return len([token for token in value.replace("`", " ").split() if token.strip(".,:;()[]")])


def repo_root_for_workflow(workflow_dir: Path | None) -> Path:
    if workflow_dir is not None and workflow_dir.parent.name == ".workflow":
        return workflow_dir.parent.parent
    return Path.cwd()


def auditable_ref_paths(text: str, workflow_dir: Path | None) -> list[Path]:
    if workflow_dir is None:
        return []
    refs: list[Path] = []
    normalized = text.replace("`", "")
    repo_root = repo_root_for_workflow(workflow_dir)
    skill_root = repo_root / ".claude" / "skills" / "agent-workflow"
    for match in WORKFLOW_AUDITABLE_REF_RE.findall(normalized):
        refs.append(workflow_dir / match)
    for match in SOURCE_AUDITABLE_REF_RE.findall(normalized):
        if match.startswith(".claude/"):
            refs.append(repo_root / match)
        elif match.startswith("changelog/"):
            refs.append(repo_root / match)
        else:
            refs.append(skill_root / match)
    return refs


def has_existing_auditable_ref(text: str, workflow_dir: Path | None) -> bool:
    refs = auditable_ref_paths(text, workflow_dir)
    return bool(refs) and all(ref.is_file() for ref in refs)


WORKFLOW_CONTENT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "records",
    "record",
    "contains",
    "accepted",
    "rejected",
    "failed",
    "passed",
    "inspected",
    "evidence",
    "for",
    "from",
    "has",
    "have",
    "had",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
    "workflow",
    "rounds",
    "round",
    "lane",
    "lanes",
    "output",
    "outputs",
    "integration",
    "final",
    "report",
    "json",
    "status",
    "complete",
    "current",
    "positive",
    "negative",
    "validation",
    "validator",
    "runner",
    "native",
    "lead",
    "recorded",
    "lifecycle",
    "command",
    "result",
    "results",
    "fixture",
    "fixtures",
    "check",
    "checks",
    "criterion",
    "criteria",
    "repair",
    "packet",
    "packets",
}

SHORT_CONTENT_CLAIM_TOKENS = {
    "bug",
    "done",
    "fixed",
    "fix",
    "ok",
    "pass",
    "fail",
    "p0",
    "p1",
    "p2",
    "p3",
}

POSITIVE_CONTENT_CLAIM_TOKENS = {
    "complete",
    "completed",
    "done",
    "fix",
    "fixed",
    "ok",
    "pass",
    "passed",
    "resolved",
    "satisfied",
}

NEGATION_TOKENS = {
    "fail",
    "failed",
    "failing",
    "false",
    "falsey",
    "falsy",
    "0",
    "null",
    "not",
    "no",
    "never",
    "remains",
    "still",
    "unresolved",
    "without",
}

VALUE_NEGATION_TOKENS = {"false", "falsey", "falsy", "0", "null"}


def workflow_content_refs(text: str, workflow_dir: Path | None) -> list[Path]:
    if workflow_dir is None:
        return []
    refs: list[Path] = []
    normalized = text.replace("`", "")
    for match in WORKFLOW_AUDITABLE_REF_RE.findall(normalized):
        if "/fixtures/" in match:
            continue
        path = workflow_dir / match
        if path.is_file():
            refs.append(path)
    return refs


def source_content_refs(text: str, workflow_dir: Path | None) -> list[Path]:
    refs: list[Path] = []
    normalized = text.replace("`", "")
    root = repo_root_for_workflow(workflow_dir) if workflow_dir is not None else Path.cwd()
    skill_root = root / ".claude" / "skills" / "agent-workflow"
    for match in SOURCE_AUDITABLE_REF_RE.findall(normalized):
        if match.startswith(".claude/") or match.startswith("changelog/"):
            path = root / match
        else:
            path = skill_root / match
        if path.is_file():
            refs.append(path)
    return refs


def artifact_content_refs(text: str, workflow_dir: Path | None) -> list[Path]:
    return workflow_content_refs(text, workflow_dir) + source_content_refs(text, workflow_dir)


def artifact_content_claim_matches(text: str, workflow_dir: Path | None) -> bool:
    refs = artifact_content_refs(text, workflow_dir)
    if not refs:
        return False
    raw = " ".join(text.lower().replace("`", "").split())
    redacted = WORKFLOW_AUDITABLE_REF_RE.sub(" ", raw)
    redacted = SOURCE_AUDITABLE_REF_RE.sub(" ", redacted)
    redacted = re.sub(r"(?:(?<=\s)|^)(?::[0-9]+|#l[0-9]+)\b", " ", redacted)
    normalized = " ".join(redacted.replace("_", " ").replace("-", " ").split())
    tokens = [
        token.strip(".,:;()[]{}'\"")
        for token in re.split(r"[^a-z0-9_]+", normalized)
    ]
    claim_tokens = [
        token
        for token in tokens
        if (len(token) >= 2 or token in SHORT_CONTENT_CLAIM_TOKENS)
        and token not in WORKFLOW_CONTENT_STOPWORDS
        and not token.isdigit()
        and not token.startswith("round")
        and not token.endswith("json")
    ]
    if not claim_tokens:
        return False
    for path in refs:
        content = path.read_text(encoding="utf-8").lower().replace("_", " ").replace("-", " ")
        content_token_list = [
            token.strip(".,:;()[]{}'\"")
            for token in re.split(r"[^a-z0-9_]+", content)
            if token.strip(".,:;()[]{}'\"")
        ]
        content_tokens = set(content_token_list)
        if artifact_content_negates_claim(content_token_list, claim_tokens):
            return False
        if not all(token in content_tokens for token in claim_tokens):
            return False
    return True


def workflow_content_claim_matches(text: str, workflow_dir: Path | None) -> bool:
    return artifact_content_claim_matches(text, workflow_dir)


def artifact_content_negates_claim(content_tokens: list[str], claim_tokens: list[str]) -> bool:
    positive_claims = POSITIVE_CONTENT_CLAIM_TOKENS.intersection(claim_tokens)
    if not positive_claims:
        return False
    saw_positive_claim = False
    claim_status: dict[str, bool] = {token: False for token in positive_claims}
    for index, token in enumerate(content_tokens):
        if token not in positive_claims:
            continue
        saw_positive_claim = True
        before = content_tokens[max(0, index - 3) : index]
        after = content_tokens[index + 1 : index + 4]
        before_negations = NEGATION_TOKENS - VALUE_NEGATION_TOKENS
        after_negations = NEGATION_TOKENS - VALUE_NEGATION_TOKENS
        is_negated = (
            any(item in before_negations for item in before)
            or any(item in after_negations for item in after)
            or bool(after and after[0] in VALUE_NEGATION_TOKENS)
        )
        if not is_negated:
            claim_status[token] = True
    return saw_positive_claim and not all(claim_status.values())


def token_phrase_present(tokens: list[str], phrase: str) -> bool:
    phrase_tokens = phrase.split()
    if not phrase_tokens:
        return False
    length = len(phrase_tokens)
    for index in range(0, len(tokens) - length + 1):
        if tokens[index : index + length] == phrase_tokens:
            return True
    return False


PASSIVE_BRIDGE_TOKENS = {
    "already",
    "also",
    "actually",
    "clearly",
    "fully",
    "just",
    "now",
    "previously",
    "successfully",
    "then",
}


def is_passive_bridge_token(token: str) -> bool:
    return token in PASSIVE_BRIDGE_TOKENS or token.endswith("ly")


def passive_aux_end(window: list[str], start: int) -> int | None:
    token = window[start]
    if token in {"was", "were", "is", "are", "be", "being", "got", "gets"}:
        return start + 1
    if token in {"has", "have", "had"}:
        for index in range(start + 1, min(len(window), start + 5)):
            if window[index] in NEGATION_TOKENS:
                return None
            if window[index] == "been":
                return index + 1
            if not is_passive_bridge_token(window[index]):
                return None
    if token in {"would", "will", "shall"}:
        for index in range(start + 1, min(len(window), start + 4)):
            if window[index] in NEGATION_TOKENS:
                return None
            if window[index] == "be":
                return index + 1
            if not is_passive_bridge_token(window[index]):
                return None
    if token == "did":
        for index in range(start + 1, min(len(window), start + 5)):
            if window[index] in NEGATION_TOKENS:
                return None
            if window[index] == "get":
                return index + 1
            if not is_passive_bridge_token(window[index]):
                return None
    return None


def passive_verb_after(window: list[str], start: int, verbs: set[str]) -> bool:
    for token in window[start : start + 5]:
        if token in NEGATION_TOKENS:
            return False
        if token in verbs:
            return True
    return False


def has_lead_recorded_passive_overclaim(text: str) -> bool:
    masked = re.sub(r"\boutput[_-]path\b", " outputpathfield ", text.lower())
    tokens = " ".join(re.sub(r"[^\w]+", " ", masked).split()).split()
    objects = {"output", "outputs", "result", "results", "response", "responses"}
    verbs = {"created", "written", "generated", "produced", "emitted", "yielded", "returned"}
    for index, token in enumerate(tokens):
        if token not in objects:
            continue
        window = tokens[index + 1 : index + 12]
        for start in range(len(window)):
            aux_end = passive_aux_end(window, start)
            if aux_end is not None and passive_verb_after(window, aux_end, verbs):
                return True
    return False


def fixture_result_name_spans(normalized: str, name: str) -> list[tuple[int, int]]:
    if not name:
        return []
    pattern = re.compile(r"(?<![a-z0-9_.:/-])" + re.escape(name) + r"(?![a-z0-9_.:/-])")
    return [
        (match.start(), match.end())
        for match in pattern.finditer(normalized)
        if fixture_result_follow_boundary(normalized, match.end())
    ]


def fixture_result_raw_spans(normalized: str, name: str) -> list[tuple[int, int]]:
    if not name:
        return []
    pattern = re.compile(r"(?<![a-z0-9_.:/-])" + re.escape(name))
    return [(match.start(), match.end()) for match in pattern.finditer(normalized)]


FIXTURE_RESULT_FOLLOW_WORDS = {
    "check",
    "checks",
    "command",
    "commands",
    "exit",
    "exit_code",
    "exited",
    "failed",
    "fixture",
    "passed",
    "record",
    "recorded",
    "records",
    "rejected",
    "result",
    "results",
    "status",
    "stderr",
    "stdout",
}


def fixture_result_follow_boundary(normalized: str, end: int) -> bool:
    suffix = normalized[end:]
    if not suffix:
        return True
    stripped = suffix.lstrip()
    if not stripped:
        return True
    if suffix[0] in ".,;:)]}":
        remainder = suffix[1:]
        if remainder and not remainder[0].isspace():
            return False
        stripped = remainder.lstrip()
        if not stripped:
            return True
    elif not suffix[0].isspace():
        return False
    match = re.match(r"([a-z0-9_]+)", stripped)
    return bool(match and match.group(1) in FIXTURE_RESULT_FOLLOW_WORDS)


def mentioned_fixture_results(
    normalized: str, results: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any], tuple[int, int]]]:
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    for result in results:
        name = str(result.get("name", "")).strip().lower()
        if not name:
            continue
        for start, end in fixture_result_name_spans(normalized, name):
            matches.append((start, end, name, result))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[str, dict[str, Any], tuple[int, int]]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, name, result in matches:
        if any(start < prior_end and end > prior_start for prior_start, prior_end in occupied):
            continue
        selected.append((name, result, (start, end)))
        occupied.append((start, end))
    return selected


def has_ambiguous_fixture_result_reference(normalized: str, result_names: list[str]) -> bool:
    valid_longer_starts: dict[int, int] = {}
    for name in sorted(result_names, key=len, reverse=True):
        for start, end in fixture_result_name_spans(normalized, name):
            valid_longer_starts[start] = max(valid_longer_starts.get(start, 0), end)
    for name in result_names:
        for start, end in fixture_result_raw_spans(normalized, name):
            if fixture_result_follow_boundary(normalized, end):
                continue
            if valid_longer_starts.get(start, 0) > end:
                continue
            return True
    return False


def fixture_result_claim_segment(normalized: str, name: str, result_names: list[str]) -> str:
    spans = fixture_result_name_spans(normalized, name)
    if not spans:
        return ""
    start, _ = spans[0]
    end = len(normalized)
    for other_name in result_names:
        if not other_name or other_name == name:
            continue
        for other_start, _ in fixture_result_name_spans(normalized, other_name):
            if other_start <= start:
                continue
            end = min(end, other_start)
    return normalized[start:end]


def fixture_segment_exit_codes(segment: str) -> set[int]:
    codes: set[int] = set()
    for match in re.finditer(
        r"[\"']?\b(?:exit_code|exit code|exited)\b[\"']?\s*[:=]?\s*([0-9]+)\b",
        segment,
    ):
        try:
            codes.add(int(match.group(1)))
        except ValueError:
            continue
    return codes


def referenced_fixture_logs(text: str, workflow_dir: Path | None) -> list[Path]:
    if workflow_dir is None:
        return []
    normalized = text.replace("`", "")
    refs = [
        workflow_dir / match
        for match in WORKFLOW_AUDITABLE_REF_RE.findall(normalized)
        if "/fixtures/" in match
    ]
    return [ref for ref in refs if ref.is_file()]


def fixture_expectations_match_results(value: dict[str, Any]) -> bool:
    expectations = value.get("expectations")
    results = value.get("results")
    if not isinstance(expectations, dict) or not expectations:
        return False
    if not isinstance(results, list):
        return False
    result_dicts = [result for result in results if isinstance(result, dict)]
    result_by_name: dict[str, dict[str, Any]] = {}
    for result in result_dicts:
        name = str(result.get("name", "")).strip()
        if not name or name in result_by_name:
            return False
        result_by_name[name] = result
    for name, expected_exit in expectations.items():
        if not isinstance(name, str) or name not in result_by_name:
            return False
        actual_exit = result_by_name[name].get("exit_code")
        if isinstance(expected_exit, int) and actual_exit != expected_exit:
            return False
    return True


def fixture_log_claim_matches(text: str, workflow_dir: Path | None) -> bool:
    logs = referenced_fixture_logs(text, workflow_dir)
    if not logs:
        return False
    normalized = " ".join(text.lower().split())
    for path in logs:
        value = load_json(path, [])
        if not isinstance(value, dict):
            continue
        results = value.get("results")
        if not isinstance(results, list):
            continue
        result_dicts = [result for result in results if isinstance(result, dict)]
        all_result_names = [
            str(result.get("name", "")).strip().lower()
            for result in result_dicts
            if str(result.get("name", "")).strip()
        ]
        duplicate_names = {name for name in all_result_names if all_result_names.count(name) > 1}
        if duplicate_names:
            return False
        if has_ambiguous_fixture_result_reference(normalized, all_result_names):
            return False
        mentioned_results = mentioned_fixture_results(normalized, result_dicts)
        result_names = [name for name, _, _ in mentioned_results]
        mentions_specific_result = bool(mentioned_results)
        mentions_exit_code = "exit_code" in normalized or bool(re.search(r"\bexited\s+[01]\b", normalized))
        mentions_command = any(
            term in normalized
            for term in ("python3 ", "verify_workflow.py", "scripts/", "git diff --check", "py_compile", "--mode")
        )
        if "expectations_match true" in normalized and value.get("expectations_match") is True:
            if not fixture_expectations_match_results(value):
                return False
            if not (mentions_specific_result or mentions_exit_code or mentions_command):
                return True
        matched_results: list[tuple[str, dict[str, Any], str, set[int]]] = []
        for name, result, _ in mentioned_results:
            exit_code = result.get("exit_code")
            command = str(result.get("command", "")).strip().lower()
            claim_segment = fixture_result_claim_segment(normalized, name, result_names)
            segment_exit_codes = fixture_segment_exit_codes(claim_segment)
            if segment_exit_codes and isinstance(exit_code, int) and any(
                code != exit_code for code in segment_exit_codes
            ):
                return False
            matched_results.append((name, result, claim_segment, segment_exit_codes))
        matched_any = False
        for name, result, claim_segment, segment_exit_codes in matched_results:
            exit_code = result.get("exit_code")
            command = str(result.get("command", "")).strip().lower()
            has_matching_exit = (
                isinstance(exit_code, int)
                and (
                    exit_code in segment_exit_codes
                    or bool(
                        re.search(
                            re.escape(name)
                            + r".{0,240}[\"']?\bexit_code\b[\"']?\s*[:=]?\s*"
                            + str(exit_code),
                            claim_segment,
                        )
                    )
                    or bool(re.search(re.escape(name) + r".{0,240}exited\s+" + str(exit_code), claim_segment))
                )
            )
            has_matching_command = bool(command) and command in claim_segment
            if mentions_command and not has_matching_command:
                return False
            if (mentions_exit_code or mentions_command) and not has_matching_exit:
                return False
            if has_matching_exit or has_matching_command:
                matched_any = True
        if matched_any:
            return True
    return False


def fixture_result_matching_exit(name: str, result: dict[str, Any], claim_segment: str) -> bool:
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return False
    segment_exit_codes = fixture_segment_exit_codes(claim_segment)
    return (
        exit_code in segment_exit_codes
        or bool(
            re.search(
                re.escape(name)
                + r".{0,240}[\"']?\bexit_code\b[\"']?\s*[:=]?\s*"
                + str(exit_code),
                claim_segment,
            )
        )
        or bool(re.search(re.escape(name) + r".{0,240}exited\s+" + str(exit_code), claim_segment))
    )


def fixture_result_command_key(result: dict[str, Any]) -> str:
    return normalized_command(result.get("command")).lower()


def fixture_result_is_positive_for_check(result: dict[str, Any], check: dict[str, Any]) -> bool:
    exit_code = result.get("exit_code")
    if isinstance(exit_code, bool):
        return False
    if isinstance(exit_code, int):
        if exit_code == 0:
            return True
        return command_check_allows_expected_failure(check, result)
    status = command_record_status(result)
    if status in PASS_STATUS_VALUES:
        return True
    if status in FAIL_STATUS_VALUES:
        return command_check_allows_expected_failure(check, result)
    return False


def fixture_log_command_check_matches(
    check: dict[str, Any], text: str, workflow_dir: Path | None
) -> bool:
    if not fixture_log_claim_matches(text, workflow_dir):
        return False
    check_command = normalized_command(check.get("command")).lower()
    if not check_command:
        return True
    logs = referenced_fixture_logs(text, workflow_dir)
    normalized = " ".join(text.lower().split())
    for path in logs:
        value = load_json(path, [])
        if not isinstance(value, dict):
            continue
        results = value.get("results")
        if not isinstance(results, list):
            continue
        result_dicts = [result for result in results if isinstance(result, dict)]
        all_result_names = [
            str(result.get("name", "")).strip().lower()
            for result in result_dicts
            if str(result.get("name", "")).strip()
        ]
        if has_ambiguous_fixture_result_reference(normalized, all_result_names):
            return False
        mentioned_results = mentioned_fixture_results(normalized, result_dicts)
        result_names = [name for name, _, _ in mentioned_results]
        for name, result, _ in mentioned_results:
            if fixture_result_command_key(result) != check_command:
                continue
            claim_segment = fixture_result_claim_segment(normalized, name, result_names)
            if not fixture_result_matching_exit(name, result, claim_segment):
                continue
            if not fixture_result_is_positive_for_check(result, check):
                continue
            return True
    return False


def concrete_final_report_evidence_lines(body: str, workflow_dir: Path | None = None) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current:
                units.append(" ".join(current).strip())
                current = []
            continue
        starts_unit = (
            stripped.startswith(("- ", "* "))
            or bool(re.match(r"^\d+[.)]\s+", stripped))
            or stripped.startswith("|")
        )
        if starts_unit and current:
            units.append(" ".join(current).strip())
            current = [stripped]
        elif starts_unit:
            current = [stripped]
        elif current:
            current.append(stripped)
        else:
            current = [stripped]
    if current:
        units.append(" ".join(current).strip())
    evidence_units: list[str] = []
    for unit in units:
        lowered = unit.lower()
        if not has_substantive_evidence(unit, workflow_dir):
            continue
        if (
            any(anchor in lowered for anchor in FINAL_REPORT_EVIDENCE_ANCHORS)
            and any(term in lowered for term in FINAL_REPORT_RESULT_TERMS)
        ) or (
            any(term in lowered for term in FINAL_REPORT_COMMAND_TERMS)
            and any(term in lowered for term in FINAL_REPORT_RESULT_TERMS)
        ):
            evidence_units.append(unit.strip())
    return evidence_units


def final_report_existing_refs(workflow_dir: Path, lines: list[str]) -> set[str]:
    refs: set[str] = set()
    for line in lines:
        for match in FINAL_REPORT_REF_RE.findall(line):
            if (workflow_dir / match).is_file():
                refs.add(match)
    return refs


def report_term_in_text(term: str, text: str) -> bool:
    normalized = term.strip().lower().rstrip(".:;")
    normalized_text = " ".join(text.lower().split())
    return bool(normalized) and normalized in normalized_text


def report_evidence_in_text(evidence: str, text: str) -> bool:
    normalized = " ".join(evidence.strip().lower().split()).rstrip(".:;")
    if not normalized:
        return False
    return normalized in " ".join(text.split())


def has_substantive_evidence(value: Any, workflow_dir: Path | None = None) -> bool:
    if isinstance(value, dict):
        return any(has_substantive_evidence(item, workflow_dir) for item in value.values())
    if isinstance(value, list):
        return any(has_substantive_evidence(item, workflow_dir) for item in value)
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.strip().lower().split()).rstrip(".,:;")
    if not normalized or normalized in LOW_INFORMATION_EVIDENCE:
        return False
    if word_count(normalized) < 4:
        return workflow_dir is not None and artifact_content_claim_matches(normalized, workflow_dir)
    if any(phrase in normalized for phrase in VACUOUS_EVIDENCE_PHRASES) and not re.search(
        r"\b(exit(?:ed)?|exit code)\s*[=:]?\s*[01]\b", normalized
    ):
        return False
    has_ephemeral_path = bool(re.search(r"(/tmp/|/var/folders/)", normalized))
    has_persisted_fixture_ref = bool(
        re.search(r"rounds/round-[0-9]{3}/fixtures/[A-Za-z0-9_./-]+", normalized)
        and (workflow_dir is None or has_existing_auditable_ref(normalized, workflow_dir))
    )
    if has_ephemeral_path and not has_persisted_fixture_ref:
        return False
    has_fixture_ref = bool(re.search(r"rounds/round-[0-9]{3}/fixtures/[A-Za-z0-9_./-]+", normalized))
    if workflow_dir is not None and has_fixture_ref:
        if not fixture_log_claim_matches(normalized, workflow_dir):
            return False
        return True
    has_strong_result_claim = bool(
        "exit_code" in normalized
        or "fixture" in normalized
        or re.search(r"\bghost[_a-z0-9-]*\b", normalized)
        or "impossible evidence" in normalized
    )
    if workflow_dir is not None and has_strong_result_claim and not fixture_log_claim_matches(normalized, workflow_dir):
        return False
    has_exit_code = bool(re.search(r"\b(exit(?:ed)?|exit code)\s*[=:]?\s*[01]\b", normalized))
    has_command_term = any(
        term in normalized
        for term in ("python3 ", "verify_workflow.py", "scripts/", "git diff --check", "py_compile", "--mode")
    )
    has_command_result = has_exit_code or bool(re.search(r"\b(exited|failed|passed|rejected)\b", normalized))
    if workflow_dir is not None and has_command_term and has_command_result:
        return fixture_log_claim_matches(normalized, workflow_dir)
    has_file_or_path = bool(
        AUDITABLE_EVIDENCE_PATH_RE.search(normalized)
        and re.search(r"\b(records?|contains?|accepted|rejected|failed|passed|inspected|exited)\b", normalized)
        and (
            workflow_dir is None
            or (
                has_existing_auditable_ref(normalized, workflow_dir)
                and (
                    has_fixture_ref
                    or not artifact_content_refs(normalized, workflow_dir)
                    or artifact_content_claim_matches(normalized, workflow_dir)
                )
            )
        )
    )
    has_specific_command = has_command_term and has_command_result
    has_fixture = (
        "fixture" in normalized
        and ("exited" in normalized or "rejected" in normalized or "failed" in normalized)
        and (workflow_dir is None or fixture_log_claim_matches(normalized, workflow_dir))
    )
    return has_file_or_path or has_specific_command or has_fixture


def final_report_current_verify_terms(
    workflow_dir: Path,
) -> tuple[str | None, set[str], list[str], list[str], list[tuple[str, str]]]:
    state_value = load_json(workflow_dir / "state.json", [])
    current_round = None
    if isinstance(state_value, dict) and isinstance(state_value.get("current_round"), str):
        current_round = state_value["current_round"]
    if not current_round:
        return None, set(), [], [], []
    lane_runs_dir = workflow_dir / "rounds" / current_round / "lane-runs"
    if not lane_runs_dir.is_dir():
        return current_round, set(), [], [], []
    verify_refs: set[str] = set()
    check_terms: list[str] = []
    check_evidence_pairs: list[tuple[str, str]] = []
    criterion_terms: list[str] = []
    for lane_output_path in sorted(lane_runs_dir.glob("*.json")):
        lane_output = load_json(lane_output_path, [])
        if not isinstance(lane_output, dict):
            continue
        gate = lane_output.get("gate") if isinstance(lane_output.get("gate"), dict) else {}
        if (
            lane_output.get("lane") != "verify"
            or lane_output.get("status") != "complete"
            or gate.get("decision") != "pass"
        ):
            continue
        verify_refs.add(str(lane_output_path.relative_to(workflow_dir)))
        payload = lane_output.get("payload") if isinstance(lane_output.get("payload"), dict) else {}
        checks = payload.get("checks")
        if isinstance(checks, list):
            for check in checks:
                if isinstance(check, dict) and check.get("status") == "pass" and has_text(check.get("name")):
                    check_name = str(check["name"]).strip().lower()
                    check_terms.append(check_name)
                    if has_substantive_evidence(check.get("evidence"), workflow_dir):
                        check_evidence_pairs.append((check_name, str(check["evidence"]).strip().lower()))
        criteria = payload.get("success_criteria_status")
        if isinstance(criteria, list):
            for criterion in criteria:
                if (
                    isinstance(criterion, dict)
                    and criterion.get("status") == "pass"
                    and has_text(criterion.get("criterion"))
                ):
                    criterion_terms.append(str(criterion["criterion"]).strip().lower())
    return current_round, verify_refs, check_terms, criterion_terms, check_evidence_pairs


def runner_evidence_level(workflow_dir: Path) -> str | None:
    value = load_json(workflow_dir / "runner-evidence.json", [])
    if isinstance(value, dict) and isinstance(value.get("evidence_level"), str):
        return value["evidence_level"]
    return None


def validate_token_usage(workflow_dir: Path, failures: list[str], mode: str) -> dict[str, Any] | None:
    path = workflow_dir / "token-usage.json"
    if not path.is_file():
        if mode == "final":
            failures.append(f"Missing token usage file: {path}")
        return None
    value = load_json(path, failures)
    if isinstance(value, dict) and value.get("schema_version") == TOKEN_USAGE_SCHEMA:
        failures.extend(validate_v2(value, workflow_dir, final=mode == "final"))
        accounting = value.get("accounting")
        if mode == "planned" and isinstance(accounting, dict):
            if accounting.get("started_at") is not None:
                failures.append(
                    f"{path}.accounting must remain unstarted in planned mode; "
                    "use workflow_controller.py start for the boundary"
                )
            if accounting.get("participants") not in ([], None):
                failures.append(
                    f"{path}.accounting.participants must be empty in planned mode"
                )
        return value
    require_keys(
        value,
        (
            "schema_version",
            "status",
            "source",
            "confidence",
            "total_tokens",
            "method",
            "round_breakdown",
            "agent_breakdown",
        ),
        str(path),
        failures,
    )
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != "agent-loops.token-usage.v1":
        failures.append(f"{path}.schema_version must be agent-loops.token-usage.v1")
    if value.get("status") not in TOKEN_USAGE_STATUSES:
        failures.append(f"{path}.status must be one of {sorted(TOKEN_USAGE_STATUSES)}")
    if value.get("source") not in TOKEN_USAGE_SOURCES:
        failures.append(f"{path}.source must be one of {sorted(TOKEN_USAGE_SOURCES)}")
    if value.get("confidence") not in TOKEN_USAGE_CONFIDENCE:
        failures.append(f"{path}.confidence must be one of {sorted(TOKEN_USAGE_CONFIDENCE)}")
    if not isinstance(value.get("round_breakdown"), list):
        failures.append(f"{path}.round_breakdown must be a list")
    if not isinstance(value.get("agent_breakdown"), list):
        failures.append(f"{path}.agent_breakdown must be a list")
    method = value.get("method")
    if mode == "final":
        if value.get("status") != "complete":
            failures.append(f"{path}.status must be complete in final mode")
        total = value.get("total_tokens")
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            failures.append(f"{path}.total_tokens must be an integer greater than zero in final mode")
        if not isinstance(method, str) or not method.strip() or is_placeholder(method):
            failures.append(f"{path}.method must describe the token accounting method in final mode")
        if value.get("confidence") == "exact" or value.get("source") in {
            "runtime_reported",
            "runner_reported",
        }:
            failures.append(
                f"{path} v1 cannot prove exact runtime usage; use {TOKEN_USAGE_SCHEMA} "
                "with start/end session evidence"
            )
    return value


def final_report_token_usage_matches(workflow_dir: Path, body: str) -> bool:
    value = load_json(workflow_dir / "token-usage.json", [])
    if not isinstance(value, dict):
        return False
    total = value.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return False
    normalized = " ".join(body.lower().split())
    total_variants = {str(total), f"{total:,}"}
    has_total = any(variant in normalized for variant in total_variants)
    has_token_word = "token" in normalized or "tokens" in normalized
    source = value.get("source")
    confidence = value.get("confidence")
    source_variants = {source.lower(), source.lower().replace("_", " ")} if isinstance(source, str) else set()
    confidence_variants = (
        {confidence.lower(), confidence.lower().replace("_", " ")}
        if isinstance(confidence, str)
        else set()
    )
    has_source = any(variant in normalized for variant in source_variants)
    has_confidence = any(variant in normalized for variant in confidence_variants)
    return has_total and has_token_word and has_source and has_confidence


def validate_token_participant_coverage(
    value: dict[str, Any] | None,
    lifecycle_evidence: dict[str, dict[str, Any]],
    mode: str,
    failures: list[str],
) -> None:
    if (
        mode != "final"
        or not isinstance(value, dict)
        or value.get("schema_version") != TOKEN_USAGE_SCHEMA
    ):
        return
    accounting = value.get("accounting")
    participants = accounting.get("participants") if isinstance(accounting, dict) else None
    if not isinstance(participants, list):
        return
    expected: list[dict[str, Any]] = []
    for lane_key, record in lifecycle_evidence.items():
        if not isinstance(record, dict):
            continue
        round_id, lane_id = lane_key.split(":", 1)
        attempts = record.get("attempts")
        if isinstance(attempts, list) and attempts:
            for attempt in attempts:
                lifecycle = attempt.get("lifecycle") if isinstance(attempt, dict) else None
                if not isinstance(lifecycle, dict):
                    continue
                identities = {
                    item
                    for item in (lifecycle.get("agent_id"), lifecycle.get("native_handle"))
                    if isinstance(item, str) and item
                }
                if identities:
                    expected.append(
                        {"round_id": round_id, "lane_id": lane_id, "identities": identities}
                    )
            continue
        identities = {
            item
            for item in (record.get("agent_id"), record.get("native_handle"))
            if isinstance(item, str) and item
        }
        if identities:
            expected.append({"round_id": round_id, "lane_id": lane_id, "identities": identities})
    unmatched = list(expected)
    extras: list[str] = []
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        match_index = next(
            (
                index
                for index, item in enumerate(unmatched)
                if item["round_id"] == participant.get("round_id")
                and item["lane_id"] == participant.get("lane_id")
                and participant.get("agent_id") in item["identities"]
            ),
            None,
        )
        if match_index is None:
            extras.append(str(participant.get("execution_ref")))
        else:
            unmatched.pop(match_index)
    if unmatched:
        missing = [
            f"{item['round_id']}:{item['lane_id']}:{'/'.join(sorted(item['identities']))}"
            for item in unmatched
        ]
        failures.append(
            "token-usage v2 is missing runner execution participant(s): " + ", ".join(missing)
        )
    if extras:
        failures.append(
            "token-usage v2 contains participant(s) absent from runner evidence: "
            + ", ".join(extras)
        )


def validate_token_schema_requirement(
    state: dict[str, Any] | None,
    value: dict[str, Any] | None,
    mode: str,
    failures: list[str],
) -> None:
    if not isinstance(state, dict):
        return
    if state.get("schema_version") == "agent-workflow.workflow.v2":
        if value is None or value.get("schema_version") != TOKEN_USAGE_SCHEMA:
            failures.append(
                "workflow.v2 requires token-usage v2; this workspace cannot downgrade to legacy v1"
            )
        if mode == "final" and value is not None and value.get("confidence") != "exact":
            failures.append("workflow.v2 requires exact token accounting before final mode")
        return
    contract = state.get("token_accounting")
    if not isinstance(contract, dict):
        return
    if value is None or value.get("schema_version") != contract.get("required_schema"):
        failures.append(
            "state.json requires token-usage v2; this workspace cannot downgrade to legacy v1"
        )
    if mode == "final" and value is not None and value.get("confidence") != "exact":
        failures.append("state.json requires exact token accounting before final mode")


def require_no_placeholder(value: Any, label: str, failures: list[str]) -> None:
    if is_placeholder(value):
        failures.append(f"{label} must be populated before executed/final validation")


def validate_passing_checks(
    value: Any,
    label: str,
    failures: list[str],
    workflow_dir: Path | None = None,
) -> None:
    if not isinstance(value, list) or not value:
        failures.append(f"{label} must be a non-empty list")
        return
    for index, check in enumerate(value, start=1):
        item_label = f"{label}[{index}]"
        if not isinstance(check, dict):
            failures.append(f"{item_label} must be an object")
            continue
        if check.get("status") != "pass":
            failures.append(f"{item_label}.status must be pass")
        if not has_substantive_evidence(check.get("evidence"), workflow_dir):
            failures.append(f"{item_label}.evidence must be substantive")
        if not command_check_has_bound_evidence(check, workflow_dir):
            failures.append(f"{item_label}.command evidence must be bound to a command result or inspected artifact")


def command_check_has_bound_evidence(check: dict[str, Any], workflow_dir: Path | None) -> bool:
    if check.get("kind") != "command":
        return True
    evidence = check.get("evidence")
    if not isinstance(evidence, str) or workflow_dir is None:
        return False
    normalized = " ".join(evidence.lower().split())
    has_fixture_ref = bool(re.search(r"rounds/round-[0-9]{3}/fixtures/[A-Za-z0-9_./-]+", normalized))
    has_local_result = (
        has_fixture_ref
        and fixture_log_command_check_matches(check, normalized, workflow_dir)
        and (
            "exit_code" in normalized
            or bool(re.search(r"\bexited\s+[01]\b", normalized))
            or any(
                term in normalized
                for term in ("python3 ", "verify_workflow.py", "scripts/", "git diff --check", "py_compile", "--mode")
            )
        )
        and "expectations_match true" not in normalized
    )
    if has_fixture_ref:
        return has_local_result
    return command_check_artifact_claim_matches(check, evidence, workflow_dir)


def command_claim_tokens(check: dict[str, Any]) -> list[str]:
    name = str(check.get("name", "")).strip().lower()
    normalized_name = name.replace("_", " ").replace("-", " ")
    name_tokens = [
        token.strip(".,:;()[]{}'\"")
        for token in re.split(r"[^a-z0-9_]+", normalized_name)
    ]
    return [
        token
        for token in name_tokens
        if (len(token) >= 2 or token in SHORT_CONTENT_CLAIM_TOKENS)
        and token not in WORKFLOW_CONTENT_STOPWORDS
        and not token.isdigit()
    ]


def value_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def text_tokens(value: str) -> set[str]:
    return {
        token.strip(".,:;()[]{}'\"")
        for token in re.split(r"[^a-z0-9_]+", value.lower().replace("_", " ").replace("-", " "))
        if token.strip(".,:;()[]{}'\"")
    }


def iter_dict_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        records.append(value)
        for child in value.values():
            records.extend(iter_dict_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(iter_dict_records(child))
    return records


EXPECTED_FAILURE_TERMS = (
    "expected failure",
    "expected fail",
    "expected to fail",
    "fails as expected",
    "failed as expected",
    "expected rejection",
    "rejection expected",
    "expected nonzero",
    "nonzero expected",
)

PASS_STATUS_VALUES = {
    "ok",
    "pass",
    "passed",
    "success",
    "successful",
    "complete",
    "completed",
}
FAIL_STATUS_VALUES = {
    "blocked",
    "error",
    "fail",
    "failed",
    "failure",
    "invalid",
}


def normalized_command(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().strip("`").split())


def collect_command_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        command = normalized_command(value)
        return [command] if command else []
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(collect_command_strings(item))
        return commands
    return []


def command_record_commands(record: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for key, value in record.items():
        normalized_key = str(key).lower().replace("-", "_")
        if normalized_key in {"command", "commands"}:
            commands.extend(collect_command_strings(value))
    return commands


def expected_failure_context(check: dict[str, Any], record: dict[str, Any]) -> str:
    fields = ("expectation", "expected")
    values: list[str] = []
    for source in (check, record):
        for field in fields:
            value = source.get(field)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, dict):
                values.extend(str(item) for item in value.values() if isinstance(item, str))
    if record.get("expected_failure") is True or check.get("expected_failure") is True:
        values.append("expected failure")
    return " ".join(values).lower()


EXPECTED_FAILURE_NEGATION_TOKENS = NEGATION_TOKENS | {"unexpected"}


def expected_failure_phrase_present(text: str, phrase: str) -> bool:
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", text.lower().replace("_", " ").replace("-", " "))
        if token
    ]
    phrase_tokens = phrase.split()
    if not tokens or not phrase_tokens:
        return False
    length = len(phrase_tokens)
    for index in range(0, len(tokens) - length + 1):
        if tokens[index : index + length] != phrase_tokens:
            continue
        before = tokens[max(0, index - 3) : index]
        after = tokens[index + length : index + length + 3]
        if any(token in EXPECTED_FAILURE_NEGATION_TOKENS for token in before):
            continue
        if any(token in VALUE_NEGATION_TOKENS for token in after):
            continue
        return True
    return False


def command_check_allows_expected_failure(check: dict[str, Any], record: dict[str, Any]) -> bool:
    context = expected_failure_context(check, record)
    return any(expected_failure_phrase_present(context, term) for term in EXPECTED_FAILURE_TERMS)


def command_record_exit_codes(record: dict[str, Any]) -> list[int]:
    values: list[int] = []
    for key, value in record.items():
        if str(key).lower().replace("-", "_") != "exit_code":
            continue
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                values.append(item)
            elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
                values.append(int(item.strip()))
    return values


def command_exit_code_values(value: Any) -> list[int]:
    candidates = value if isinstance(value, list) else [value]
    values: list[int] = []
    for item in candidates:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            values.append(item)
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            values.append(int(item.strip()))
    return values


def normalized_status(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def command_record_status(record: dict[str, Any]) -> str:
    return normalized_status(record.get("status"))


def command_record_nested_failure(value: Any, *, at_root: bool = True) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if at_root and normalized_key in {"exit_code", "status"}:
                continue
            if normalized_key == "exit_code" and any(code != 0 for code in command_exit_code_values(item)):
                return True
            if normalized_key == "status" and normalized_status(item) in FAIL_STATUS_VALUES:
                return True
            if command_record_nested_failure(item, at_root=False):
                return True
    elif isinstance(value, list):
        for item in value:
            if command_record_nested_failure(item, at_root=False):
                return True
    return False


def command_record_has_result_signal(record: dict[str, Any]) -> bool:
    keys = {str(key).lower().replace("-", "_") for key in record}
    return bool({"status", "exit_code", "stdout", "stderr"} & keys)


def command_record_has_positive_result(record: dict[str, Any], check: dict[str, Any]) -> bool:
    allow_expected_failure = command_check_allows_expected_failure(check, record)
    if command_record_nested_failure(record) and not allow_expected_failure:
        return False
    exit_codes = command_record_exit_codes(record)
    if exit_codes:
        if any(code != 0 for code in exit_codes):
            return allow_expected_failure
        if any(code == 0 for code in exit_codes):
            return True
        return allow_expected_failure
    status = command_record_status(record)
    if status in PASS_STATUS_VALUES:
        return True
    if status in FAIL_STATUS_VALUES:
        return allow_expected_failure
    return False


def command_record_matches_check(
    record: dict[str, Any], claim_tokens: list[str], check: dict[str, Any]
) -> bool:
    keys = {str(key).lower().replace("-", "_") for key in record}
    if not ({"command", "commands"} & keys):
        return False
    if not command_record_has_result_signal(record):
        return False
    if not command_record_has_positive_result(record, check):
        return False
    commands = command_record_commands(record)
    check_command = normalized_command(check.get("command"))
    if check_command and check_command not in commands:
        return False
    record_text = value_text(record)
    tokens = text_tokens(record_text)
    required_matches = min(2, len(claim_tokens))
    if required_matches and sum(1 for token in claim_tokens if token in tokens) < required_matches:
        return False
    return True


def command_artifact_record_matches(path: Path, check: dict[str, Any]) -> bool:
    if path.suffix != ".json" or "/fixtures/" in path.as_posix():
        return False
    value = load_json(path, [])
    if value is None:
        return False
    claim_tokens = command_claim_tokens(check)
    if not claim_tokens:
        return False
    return any(command_record_matches_check(record, claim_tokens, check) for record in iter_dict_records(value))


def command_artifact_refs(evidence: str, workflow_dir: Path | None) -> list[Path]:
    if workflow_dir is None or source_content_refs(evidence, workflow_dir):
        return []
    refs = workflow_content_refs(evidence, workflow_dir)
    workflow_root = workflow_dir.resolve()
    valid_refs: list[Path] = []
    for ref in refs:
        try:
            ref.resolve().relative_to(workflow_root)
        except ValueError:
            return []
        if "/fixtures/" in ref.as_posix():
            return []
        valid_refs.append(ref)
    return valid_refs


def command_check_artifact_claim_matches(
    check: dict[str, Any], evidence: str, workflow_dir: Path | None
) -> bool:
    if workflow_dir is None:
        return False
    refs = command_artifact_refs(evidence, workflow_dir)
    if not refs:
        return False
    return all(command_artifact_record_matches(ref, check) for ref in refs)


def has_actionable_repair_or_resolution(finding: dict[str, Any]) -> bool:
    if isinstance(finding.get("repair_packet"), dict):
        return True
    return has_concrete_finding_resolution(finding)


def has_concrete_finding_resolution(finding: dict[str, Any]) -> bool:
    for key in (
        "non_actionable_reason",
        "deferred_with_reason",
        "human_gate",
        "rejected_with_reason",
        "blocked_reason",
    ):
        value = finding.get(key)
        if has_text(value):
            return True
        if isinstance(value, dict) and any(has_text(item) for item in value.values()):
            return True
        if isinstance(value, list) and value:
            return True
    resolution = finding.get("resolution")
    if isinstance(resolution, dict):
        return any(has_text(item) for item in resolution.values())
    return False


def severity_at_or_above(value: Any, levels: set[str]) -> bool:
    return isinstance(value, str) and value in levels


def validate_confidence(value: Any, label: str, failures: list[str]) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}.confidence must be an object")
        return
    for key in ("self", "independent"):
        score = value.get(key)
        if score is None:
            continue
        if not isinstance(score, (int, float)) or not 0 <= score <= 1:
            failures.append(f"{label}.confidence.{key} must be null or a number 0..1")
    if "source" not in value:
        failures.append(f"{label}.confidence missing key: source")
    if "rationale" not in value:
        failures.append(f"{label}.confidence missing key: rationale")


def validate_findings(
    value: Any,
    label: str,
    failures: list[str],
    mode: str = "scaffold",
) -> None:
    if not isinstance(value, list):
        failures.append(f"{label}.findings must be a list")
        return
    for index, finding in enumerate(value, start=1):
        item_label = f"{label}.findings[{index}]"
        if not isinstance(finding, dict):
            failures.append(f"{item_label} must be an object")
            continue
        severity = finding.get("severity")
        if severity is not None and severity not in SEVERITIES:
            failures.append(f"{item_label}.severity must be one of {sorted(SEVERITIES)}")
        if mode in STRICT_MODES:
            for key in ("severity", "claim", "evidence", "recommendation"):
                if key not in finding:
                    failures.append(f"{item_label} missing key: {key}")
            if not isinstance(finding.get("claim"), str) or not finding.get("claim"):
                failures.append(f"{item_label}.claim must be a non-empty string")
            evidence = finding.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                failures.append(f"{item_label}.evidence must be a non-empty list")
            if not isinstance(finding.get("recommendation"), str) or not finding.get(
                "recommendation"
            ):
                failures.append(f"{item_label}.recommendation must be a non-empty string")
            if severity in HIGH_SEVERITIES and not has_actionable_repair_or_resolution(finding):
                failures.append(
                    f"{item_label} must include repair_packet or explicit resolution/deferral"
                )


def validate_gate(value: Any, label: str, failures: list[str]) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}.gate must be an object")
        return
    decision = value.get("decision")
    if decision not in GATE_DECISIONS:
        failures.append(f"{label}.gate.decision must be one of {sorted(GATE_DECISIONS)}")
    if "reason" not in value:
        failures.append(f"{label}.gate missing key: reason")
    next_lanes = value.get("next_lanes", [])
    if not isinstance(next_lanes, list):
        failures.append(f"{label}.gate.next_lanes must be a list")


def validate_runner_adapter(
    value: Any,
    label: str,
    failures: list[str],
    validation_mode: str = "scaffold",
) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}.runner_adapter must be an object")
        return
    runner_mode = value.get("mode")
    if runner_mode not in RUNNER_MODES:
        failures.append(f"{label}.runner_adapter.mode must be one of {sorted(RUNNER_MODES)}")
    dispatch_surface = value.get("dispatch_surface")
    if dispatch_surface not in DISPATCH_SURFACES:
        failures.append(
            f"{label}.runner_adapter.dispatch_surface must be one of {sorted(DISPATCH_SURFACES)}"
        )
    expected_surface = RUNNER_DISPATCH_SURFACE.get(runner_mode)
    if expected_surface is not None and dispatch_surface != expected_surface:
        failures.append(
            f"{label}.runner_adapter.dispatch_surface must be {expected_surface} for {runner_mode}"
        )
    if value.get("cross_runtime_calls_allowed") is not False:
        failures.append(f"{label}.runner_adapter.cross_runtime_calls_allowed must be false")
    if validation_mode in STRICT_MODES and runner_mode != "manual_simulation":
        evidence = value.get("capability_evidence")
        if not isinstance(evidence, dict):
            failures.append(f"{label}.runner_adapter.capability_evidence is required for native modes")
        elif evidence.get("verified") is not True:
            failures.append(f"{label}.runner_adapter.capability_evidence.verified must be true")
        elif not evidence.get("summary"):
            failures.append(f"{label}.runner_adapter.capability_evidence.summary is required")


def validate_lane_runner(
    value: Any,
    mode: str | None,
    label: str,
    failures: list[str],
    lane_type: str | None = None,
) -> None:
    if not isinstance(value, dict):
        failures.append(f"{label}.runner must be an object")
        return
    runner_mode = value.get("mode")
    if runner_mode not in RUNNER_MODES:
        failures.append(f"{label}.runner.mode must be one of {sorted(RUNNER_MODES)}")
    if mode and runner_mode != mode:
        failures.append(f"{label}.runner.mode must match orchestration runner mode {mode}")
    if not isinstance(value.get("agent_type"), str) or not value.get("agent_type"):
        failures.append(f"{label}.runner.agent_type must be a non-empty string")
    if not isinstance(value.get("dispatch_method"), str) or not value.get("dispatch_method"):
        failures.append(f"{label}.runner.dispatch_method must be a non-empty string")
    expected_method = RUNNER_LANE_DISPATCH_METHOD.get(runner_mode)
    allowed_methods = {expected_method} if expected_method is not None else set()
    if lane_type in LEAD_OWNED_LANES:
        allowed_methods.update({"lead_owned", "lead_agent_repair"})
    if allowed_methods and value.get("dispatch_method") not in allowed_methods:
        failures.append(
            f"{label}.runner.dispatch_method must be one of {sorted(allowed_methods)} for {runner_mode}"
        )


def all_lane_findings(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    top = value.get("findings")
    if isinstance(top, list):
        for item in top:
            if not isinstance(item, dict):
                continue
            digest = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            seen.add(digest)
            items.append(("top-level", item))
    payload = value.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("findings")
        if isinstance(nested, list):
            for item in nested:
                if not isinstance(item, dict):
                    continue
                digest = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if digest in seen:
                    continue
                seen.add(digest)
                items.append(("payload", item))
    return items


def validate_finding_mirrors(
    value: dict[str, Any],
    label: str,
    failures: list[str],
    mode: str,
) -> None:
    if mode not in STRICT_MODES:
        return
    top = value.get("findings")
    payload = value.get("payload")
    nested = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(top, list) or not isinstance(nested, list):
        return
    top_by_id = {
        item.get("id"): item
        for item in top
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for index, item in enumerate(nested, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        canonical = top_by_id.get(item["id"])
        if canonical is None:
            failures.append(
                f"{label}.payload.findings[{index}] must be mirrored at top level"
            )
        elif canonical != item:
            failures.append(
                f"{label}.payload.findings[{index}] diverges from top-level finding {item['id']}"
            )


def validate_payload_shape(
    value: dict[str, Any],
    output_schema: str | None,
    label: str,
    failures: list[str],
    mode: str,
    workflow_dir: Path | None = None,
) -> None:
    payload = value.get("payload")
    if not isinstance(payload, dict):
        failures.append(f"{label}.payload must be an object")
        return
    if mode not in STRICT_MODES or not output_schema:
        return
    for key in PAYLOAD_REQUIRED_KEYS.get(output_schema, ()):
        if key not in payload:
            failures.append(f"{label}.payload missing key for {output_schema}: {key}")
    gate = value.get("gate") if isinstance(value.get("gate"), dict) else {}
    if output_schema == "verify_payload.v1" and gate.get("decision") == "pass":
        checks = payload.get("checks")
        criteria = payload.get("success_criteria_status")
        validate_passing_checks(checks, f"{label}.payload.checks", failures, workflow_dir)
        if not isinstance(criteria, list) or not criteria:
            failures.append(
                f"{label}.payload.success_criteria_status must be a non-empty list for pass"
            )
        if isinstance(criteria, list):
            for index, criterion in enumerate(criteria, start=1):
                if not isinstance(criterion, dict):
                    failures.append(f"{label}.payload.success_criteria_status[{index}] must be an object")
                    continue
                if criterion.get("status") != "pass":
                    failures.append(
                        f"{label}.payload.success_criteria_status[{index}].status must be pass"
                    )
                if not has_substantive_evidence(criterion.get("evidence"), workflow_dir):
                    failures.append(
                        f"{label}.payload.success_criteria_status[{index}].evidence must be substantive"
                    )


def validate_lane_semantics(
    value: dict[str, Any],
    label: str,
    failures: list[str],
    mode: str,
) -> None:
    if mode not in STRICT_MODES:
        return
    gate = value.get("gate") if isinstance(value.get("gate"), dict) else {}
    decision = gate.get("decision")
    findings = all_lane_findings(value)
    if decision in PASS_DECISIONS:
        for source, finding in findings:
            severity = finding.get("severity")
            if severity_at_or_above(severity, BLOCKING_SEVERITIES):
                failures.append(f"{label}.{source} finding {severity} blocks pass")
            if severity == "P2" and not has_concrete_finding_resolution(finding):
                failures.append(
                    f"{label}.{source} P2 finding must have an explicit resolution before pass"
                )
    lane = value.get("lane")
    if decision in PASS_DECISIONS and lane in {"verify", "challenge"}:
        confidence = value.get("confidence") if isinstance(value.get("confidence"), dict) else {}
        independent = confidence.get("independent")
        if not isinstance(independent, (int, float)):
            failures.append(f"{label}.confidence.independent is required for {lane} pass")
        elif independent < DEFAULT_CONFIDENCE_THRESHOLD:
            failures.append(
                f"{label}.confidence.independent must be >= {DEFAULT_CONFIDENCE_THRESHOLD} for {lane} pass"
            )


def validate_lane_output(
    path: Path,
    failures: list[str],
    mode: str = "scaffold",
    expected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    value = load_json(path, failures)
    label = str(path)
    workflow_dir = path.parents[3] if len(path.parents) > 3 else None
    if value is None:
        return None
    required = (
        "schema_version",
        "run_id",
        "round_id",
        "lane_id",
        "lane",
        "status",
        "summary",
        "confidence",
        "findings",
        "gate",
        "payload",
    )
    require_keys(value, required, label, failures)
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != "agent-loops.lane-output.v1":
        failures.append(f"{label}.schema_version must be agent-loops.lane-output.v1")
    lane = value.get("lane")
    if lane not in ALLOWED_LANES and lane not in LEGACY_LANES:
        failures.append(f"{label}.lane must be one of {sorted(ALLOWED_LANES | LEGACY_LANES)}")
    if mode in STRICT_MODES and lane in LEGACY_LANES:
        failures.append(f"{label}.lane integrate is legacy/advisory and cannot pass {mode} mode")
    if value.get("status") not in LANE_STATUSES:
        failures.append(f"{label}.status must be one of {sorted(LANE_STATUSES)}")
    validate_confidence(value.get("confidence"), label, failures)
    validate_findings(value.get("findings"), label, failures, mode)
    validate_gate(value.get("gate"), label, failures)
    if expected is not None:
        if value.get("lane_id") != expected.get("id"):
            failures.append(f"{label}.lane_id must match planned lane {expected.get('id')}")
        if value.get("lane") != expected.get("lane"):
            failures.append(f"{label}.lane must match planned lane type {expected.get('lane')}")
        expected_run_id = f"{value.get('round_id')}-{value.get('lane_id')}"
        if value.get("run_id") != expected_run_id:
            failures.append(f"{label}.run_id must be {expected_run_id}")
    output_schema = expected.get("output_schema") if expected else None
    validate_payload_shape(value, output_schema, label, failures, mode, workflow_dir)
    payload = value.get("payload")
    if isinstance(payload, dict) and "findings" in payload:
        validate_findings(payload.get("findings"), f"{label}.payload", failures, mode)
    validate_finding_mirrors(value, label, failures, mode)
    validate_lane_semantics(value, label, failures, mode)
    if mode == "final" and isinstance(payload, dict):
        payload_findings = payload.get("findings")
        if isinstance(payload_findings, list) and payload_findings and not value.get("findings"):
            failures.append(f"{label}.payload.findings must be mirrored or resolved before final mode")
    return value


def validate_lane_spec(
    value: Any,
    runner_mode: str | None,
    label: str,
    failures: list[str],
    validation_mode: str = "scaffold",
) -> None:
    required = (
        "id",
        "lane",
        "enabled",
        "required",
        "agent_count",
        "purpose",
        "prompt",
        "input_refs",
        "output_schema",
        "gate",
        "runner",
    )
    require_keys(value, required, label, failures)
    if not isinstance(value, dict):
        return
    lane_id = value.get("id")
    if not isinstance(lane_id, str) or not lane_id:
        failures.append(f"{label}.id must be a non-empty string")
    lane = value.get("lane")
    if lane not in ALLOWED_LANES:
        failures.append(f"{label}.lane must be one of {sorted(ALLOWED_LANES)}")
    if lane == "custom" and not value.get("custom_name"):
        failures.append(f"{label}.custom_name is required for custom lanes")
    if not isinstance(value.get("enabled"), bool):
        failures.append(f"{label}.enabled must be boolean")
    if not isinstance(value.get("required"), bool):
        failures.append(f"{label}.required must be boolean")
    agent_count = value.get("agent_count")
    if not isinstance(agent_count, int) or agent_count < 1:
        failures.append(f"{label}.agent_count must be an integer >= 1")
    if not isinstance(value.get("input_refs"), list):
        failures.append(f"{label}.input_refs must be a list")
    if not isinstance(value.get("gate"), dict):
        failures.append(f"{label}.gate must be an object")
    if validation_mode in STRICT_MODES:
        require_no_placeholder(value.get("purpose"), f"{label}.purpose", failures)
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or "Return JSON only using the Agent Workflow lane-output envelope." == prompt.strip():
            failures.append(f"{label}.prompt must be specific before executed/final validation")
    validate_lane_runner(
        value.get("runner"), runner_mode, label, failures, str(lane) if lane else None
    )


def validate_state(
    workflow_dir: Path,
    failures: list[str],
    mode: str = "scaffold",
) -> dict[str, Any] | None:
    state_path = workflow_dir / "state.json"
    state = load_json(state_path, failures)
    required = (
        "schema_version",
        "title",
        "slug",
        "created_at",
        "status",
        "current_round",
        "round_budget",
        "runner_mode",
        "runner_adapter",
        "approval",
        "gates",
        "rounds",
        "final_status",
    )
    require_keys(state, required, str(state_path), failures)
    if not isinstance(state, dict):
        return None
    state_schema = state.get("schema_version")
    if state_schema not in {"agent-loops.workflow.v1", "agent-workflow.workflow.v2"}:
        failures.append(
            f"{state_path}.schema_version must be agent-loops.workflow.v1 or agent-workflow.workflow.v2"
        )
    if state.get("status") not in WORKFLOW_STATUSES:
        failures.append(f"{state_path}.status must be one of {sorted(WORKFLOW_STATUSES)}")
    if state.get("runner_mode") not in RUNNER_MODES:
        failures.append(f"{state_path}.runner_mode must be one of {sorted(RUNNER_MODES)}")
    validate_runner_adapter(state.get("runner_adapter"), str(state_path), failures, mode)
    token_contract = state.get("token_accounting")
    if state_schema == "agent-workflow.workflow.v2" and token_contract is None:
        failures.append(f"{state_path}.token_accounting is required for workflow.v2")
    if token_contract is not None:
        if not isinstance(token_contract, dict):
            failures.append(f"{state_path}.token_accounting must be an object")
        elif token_contract != {
            "required_schema": TOKEN_USAGE_SCHEMA,
            "exact_required": True,
        }:
            failures.append(
                f"{state_path}.token_accounting must require {TOKEN_USAGE_SCHEMA} exact accounting"
            )
    round_budget = state.get("round_budget")
    if not isinstance(round_budget, int) or round_budget < 1:
        failures.append(f"{state_path}.round_budget must be an integer >= 1")
    if not isinstance(state.get("rounds"), list):
        failures.append(f"{state_path}.rounds must be a list")
    else:
        seen_round_ids: set[str] = set()
        for index, round_state in enumerate(state["rounds"], start=1):
            label = f"{state_path}.rounds[{index}]"
            require_keys(
                round_state,
                ("round_id", "status", "objective", "enabled_lanes", "gate_decision"),
                label,
                failures,
            )
            if isinstance(round_state, dict):
                round_id = round_state.get("round_id")
                if isinstance(round_id, str):
                    if round_id in seen_round_ids:
                        failures.append(f"{label}.round_id duplicates {round_id}")
                    seen_round_ids.add(round_id)
                if round_state.get("status") not in WORKFLOW_STATUSES:
                    failures.append(f"{label}.status must be one of {sorted(WORKFLOW_STATUSES)}")
                if round_state.get("gate_decision") not in GATE_DECISIONS:
                    failures.append(
                        f"{label}.gate_decision must be one of {sorted(GATE_DECISIONS)}"
                    )
                if mode in STRICT_MODES:
                    enabled = round_state.get("enabled_lanes")
                    if not isinstance(enabled, list):
                        failures.append(f"{label}.enabled_lanes must be a list")
                    else:
                        seen_enabled: set[str] = set()
                        for lane_index, lane_id in enumerate(enabled, start=1):
                            if not isinstance(lane_id, str) or not lane_id:
                                failures.append(
                                    f"{label}.enabled_lanes[{lane_index}] must be a non-empty string"
                                )
                                continue
                            if lane_id in seen_enabled:
                                failures.append(f"{label}.enabled_lanes duplicates {lane_id}")
                            seen_enabled.add(lane_id)
                    require_no_placeholder(round_state.get("objective"), f"{label}.objective", failures)
    return state


def validate_orchestration(
    workflow_dir: Path,
    failures: list[str],
    mode: str = "scaffold",
) -> dict[str, Any] | None:
    orchestration_path = workflow_dir / "orchestration.json"
    orchestration = load_json(orchestration_path, failures)
    require_keys(
        orchestration,
        ("schema_version", "workflow", "orchestrator", "rounds"),
        str(orchestration_path),
        failures,
    )
    if not isinstance(orchestration, dict):
        return None
    if orchestration.get("schema_version") not in {
        "agent-loops.orchestration.v1",
        "agent-loops.orchestration.v2",
    }:
        failures.append(
            f"{orchestration_path}.schema_version must be agent-loops.orchestration.v1 or v2"
        )

    workflow = orchestration.get("workflow")
    require_keys(
        workflow,
        ("title", "slug", "goal", "success_criteria", "constraints", "non_goals"),
        f"{orchestration_path}.workflow",
        failures,
    )
    if isinstance(workflow, dict) and mode in STRICT_MODES:
        require_no_placeholder(workflow.get("goal"), f"{orchestration_path}.workflow.goal", failures)
        success_criteria = workflow.get("success_criteria")
        if not isinstance(success_criteria, list) or not success_criteria:
            failures.append(
                f"{orchestration_path}.workflow.success_criteria must be non-empty before executed/final validation"
            )
    orchestrator = orchestration.get("orchestrator")
    require_keys(
        orchestrator,
        (
            "planning_mode",
            "runner_mode",
            "runner_adapter",
            "round_budget",
            "stop_conditions",
            "invalid_json_policy",
        ),
        f"{orchestration_path}.orchestrator",
        failures,
    )
    if isinstance(orchestrator, dict):
        if orchestrator.get("planning_mode") != "planner_first":
            failures.append(f"{orchestration_path}.orchestrator.planning_mode must be planner_first")
        if orchestrator.get("runner_mode") not in RUNNER_MODES:
            failures.append(
                f"{orchestration_path}.orchestrator.runner_mode must be one of {sorted(RUNNER_MODES)}"
            )
        validate_runner_adapter(
            orchestrator.get("runner_adapter"),
            f"{orchestration_path}.orchestrator",
            failures,
            mode,
        )
        if orchestrator.get("invalid_json_policy") != "repair_once_then_invalid_output":
            failures.append(
                f"{orchestration_path}.orchestrator.invalid_json_policy must be repair_once_then_invalid_output"
            )

    rounds = orchestration.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        failures.append(f"{orchestration_path}.rounds must be a non-empty list")
    else:
        seen_round_ids: set[str] = set()
        for index, round_plan in enumerate(rounds, start=1):
            label = f"{orchestration_path}.rounds[{index}]"
            require_keys(round_plan, ("round_id", "objective", "lanes"), label, failures)
            if not isinstance(round_plan, dict):
                continue
            round_id = round_plan.get("round_id")
            if isinstance(round_id, str):
                if round_id in seen_round_ids:
                    failures.append(f"{label}.round_id duplicates {round_id}")
                seen_round_ids.add(round_id)
            lanes = round_plan.get("lanes")
            if not isinstance(lanes, list):
                failures.append(f"{label}.lanes must be a list")
                continue
            runner_mode = orchestrator.get("runner_mode") if isinstance(orchestrator, dict) else None
            seen_lane_ids: set[str] = set()
            for lane_index, lane_spec in enumerate(lanes, start=1):
                if isinstance(lane_spec, dict):
                    lane_id = lane_spec.get("id")
                    if isinstance(lane_id, str):
                        if lane_id in seen_lane_ids:
                            failures.append(f"{label}.lanes[{lane_index}].id duplicates {lane_id}")
                        seen_lane_ids.add(lane_id)
                validate_lane_spec(
                    lane_spec,
                    runner_mode,
                    f"{label}.lanes[{lane_index}]",
                    failures,
                    mode,
                )
    return orchestration


def validate_execution_efficiency_contract(
    workflow_dir: Path,
    orchestration: dict[str, Any] | None,
    mode: str,
    failures: list[str],
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    if not isinstance(orchestration, dict):
        return None, {}
    value = orchestration.get("execution_efficiency")
    if value is None:
        return None, {}
    if isinstance(value, dict) and value.get("enabled") is False:
        return None, {}
    orchestrator = orchestration.get("orchestrator")
    runner_mode = orchestrator.get("runner_mode") if isinstance(orchestrator, dict) else None
    if not isinstance(runner_mode, str):
        failures.append("execution_efficiency requires orchestration.orchestrator.runner_mode")
        return None, {}
    try:
        policy = validate_execution_policy(value, runner_mode)
        lanes_by_ref = validate_orchestration_efficiency(
            orchestration,
            runner_mode,
            policy,
            allow_draft=mode == "scaffold",
            workflow_dir=workflow_dir,
            allow_terminal_input_drift=mode in EXECUTION_MODES,
        )
    except ExecutionEfficiencyError as exc:
        failures.append(f"execution_efficiency contract: {exc}")
        return None, {}

    if mode not in EXECUTION_MODES:
        return policy, lanes_by_ref

    expected_receipts: set[str] = set()
    for lane_ref, lane in lanes_by_ref.items():
        round_id, lane_id = lane_ref.split(":", 1)
        relative = receipt_relative_path(round_id, lane_id)
        expected_receipts.add(relative)
        receipt_path = workflow_dir / relative
        if not receipt_path.is_file():
            failures.append(f"Missing execution-efficiency lane receipt: {relative}")
            continue
        receipt = load_json(receipt_path, failures)
        if receipt is None:
            continue
        try:
            validate_lane_receipt(workflow_dir, lane, round_id, receipt)
        except (OSError, json.JSONDecodeError, ExecutionEfficiencyError) as exc:
            failures.append(f"{relative}: {exc}")

        if lane.get("lane") == "verify":
            output_path = workflow_dir / str(lane["execution"]["output_path"])
            output = load_json(output_path, failures)
            gate = output.get("gate") if isinstance(output, dict) else None
            if isinstance(gate, dict) and gate.get("decision") == "pass":
                workflow = orchestration.get("workflow")
                expected_criteria = (
                    workflow.get("success_criteria") if isinstance(workflow, dict) else None
                )
                payload = output.get("payload") if isinstance(output, dict) else None
                actual_items = (
                    payload.get("success_criteria_status")
                    if isinstance(payload, dict)
                    else None
                )
                actual_criteria = [
                    item.get("criterion")
                    for item in actual_items
                    if isinstance(item, dict)
                ] if isinstance(actual_items, list) else None
                if actual_criteria != expected_criteria:
                    failures.append(
                        f"{relative} verify pass must cover the exact current workflow success criteria in order"
                    )

    actual_receipts = {
        path.relative_to(workflow_dir).as_posix()
        for path in (workflow_dir / "rounds").glob("*/receipts/*.json")
        if path.is_file()
    }
    unexpected_receipts = sorted(actual_receipts - expected_receipts)
    if unexpected_receipts:
        failures.append(
            "Unexpected execution-efficiency receipt(s): " + ", ".join(unexpected_receipts)
        )

    index_path = workflow_dir / "integration-index.json"
    if not index_path.is_file():
        failures.append(f"Missing compact integration index: {index_path}")
    else:
        actual_index = load_json(index_path, failures)
        if actual_index is not None:
            try:
                expected_index = build_integration_index(workflow_dir, orchestration)
                if actual_index != expected_index:
                    failures.append(
                        "integration-index.json must exactly match validated lane receipts and outputs"
                    )
            except (OSError, json.JSONDecodeError, ExecutionEfficiencyError) as exc:
                failures.append(f"integration-index.json: {exc}")
    return policy, lanes_by_ref


def validate_execution_efficiency_runtime(
    workflow_dir: Path,
    policy: dict[str, Any] | None,
    lanes_by_ref: dict[str, dict[str, Any]],
    lifecycle_evidence: dict[str, dict[str, Any]],
    mode: str,
    failures: list[str],
) -> None:
    if policy is None:
        return
    evidence = load_json(workflow_dir / "runner-evidence.json", failures)
    if not isinstance(evidence, dict):
        return
    telemetry_value = evidence.get("execution_efficiency")
    if telemetry_value is None:
        failures.append("runner-evidence.json.execution_efficiency is required")
        return
    try:
        telemetry = validate_wait_telemetry(
            telemetry_value,
            final=mode == "final",
            policy=policy,
        )
        validate_agent_execution_evidence(
            lanes_by_ref,
            lifecycle_evidence,
            telemetry,
            final=mode == "final",
        )
    except ExecutionEfficiencyError as exc:
        failures.append(f"execution_efficiency runtime: {exc}")


def validate_integration(path: Path, round_id: str, failures: list[str]) -> dict[str, Any] | None:
    integration = load_json(path, failures)
    required = (
        "schema_version",
        "round_id",
        "status",
        "accepted",
        "rejected",
        "conflicts",
        "repair_packets",
        "verification_evidence",
        "remaining_risks",
        "next_round",
        "stop_reason",
    )
    require_keys(integration, required, str(path), failures)
    if not isinstance(integration, dict):
        return None
    if integration.get("schema_version") != "agent-loops.integration.v1":
        failures.append(f"{path}.schema_version must be agent-loops.integration.v1")
    if integration.get("round_id") != round_id:
        failures.append(f"{path}.round_id must match {round_id}")
    if integration.get("status") not in INTEGRATION_STATUSES:
        failures.append(f"{path}.status must be one of {sorted(INTEGRATION_STATUSES)}")
    for key in (
        "accepted",
        "rejected",
        "conflicts",
        "repair_packets",
        "verification_evidence",
        "remaining_risks",
    ):
        if not isinstance(integration.get(key), list):
            failures.append(f"{path}.{key} must be a list")
    if "finding_resolutions" in integration and not isinstance(
        integration.get("finding_resolutions"), list
    ):
        failures.append(f"{path}.finding_resolutions must be a list")
    return integration


def final_report_has_substance(
    path: Path,
    *,
    allow_exact_token_markers: bool = False,
) -> bool:
    workflow_dir = path.parent
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = text.lower()
    content_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if any(is_placeholder(line) for line in content_lines):
        return False
    if len(content_lines) < 5 or word_count(text) < FINAL_REPORT_MIN_WORDS:
        return False
    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if current_title is not None:
                sections.append((current_title, "\n".join(current_body).strip()))
            current_title = stripped.lstrip("#").strip().lower()
            current_body = []
        elif current_title is not None:
            current_body.append(line)
    if current_title is not None:
        sections.append((current_title, "\n".join(current_body).strip()))
    if not sections:
        return False

    def section_body(terms: tuple[str, ...]) -> str:
        for title, body in sections:
            if any(term in title for term in terms):
                return body
        for title, body in sections:
            haystack = f"{title}\n{body}".lower()
            if any(term in haystack for term in terms):
                return body
        return ""

    def section_matches(terms: tuple[str, ...]) -> bool:
        for title, body in sections:
            haystack = f"{title}\n{body}".lower()
            if any(term in haystack for term in terms) and word_count(body) >= FINAL_REPORT_MIN_SECTION_WORDS:
                return True
        return False

    if not all(section_matches(group) for group in FINAL_REPORT_REQUIRED_SECTIONS):
        return False
    token_body = section_body(("token", "tokens", "token usage", "消耗"))
    if allow_exact_token_markers:
        if not all(
            marker in token_body
            for marker in (
                "{{WORKFLOW_TOTAL_TOKENS}}",
                "{{WORKFLOW_TOKEN_SOURCE}}",
                "{{WORKFLOW_TOKEN_CONFIDENCE}}",
            )
        ):
            return False
    elif not final_report_token_usage_matches(workflow_dir, token_body):
        return False
    level = runner_evidence_level(workflow_dir)
    if level == "lead_recorded":
        if "lead-recorded" not in lowered and "lead recorded" not in lowered:
            return False
        if has_lead_recorded_passive_overclaim(text):
            return False
        claim_sentences = re.split(r"(?<=[.!?。；;])\s+|\n+", lowered)
        native_claim_terms = (
            "native subagent",
            "native subagents",
            "native agent",
            "native agents",
            "native execution",
            "native lifecycle",
            "lifecycle records",
            "lifecycle record",
            "multi_agent_v1",
            "spawned",
            "subagent execution",
            "agent execution",
            "runner execution",
            "runner executed",
            "agent ran",
            "subagent ran",
            "agents ran",
            "subagents ran",
            "agent did run",
            "subagent did run",
            "agent has run",
            "subagent has run",
            "agent was run",
            "subagent was run",
            "agent is running",
            "subagent is running",
            "agent running",
            "subagent running",
            "agent finished",
            "subagent finished",
            "agent returned",
            "subagent returned",
            "agent succeeded",
            "subagent succeeded",
            "agent produced output",
            "subagent produced output",
            "agent generated output",
            "subagent generated output",
            "agent emitted output",
            "subagent emitted output",
            "agent yielded output",
            "subagent yielded output",
            "agent produced result",
            "subagent produced result",
            "agent generated result",
            "subagent generated result",
            "agent ended",
            "subagent ended",
            "spawn wait close",
        )
        overclaim_terms = (
            "actually",
            "spawned",
            "loaded",
            "did exist",
            "was used",
            "certified",
            "certification",
            "certifies",
            "certify",
            "confirmed",
            "confirmation",
            "confirms",
            "confirm",
            "verified",
            "verifies",
            "verification",
            "independently verified",
            "completes",
            "complete native",
            "completed",
            "completion",
            "closed",
            "closed successfully",
            "validated",
            "validation",
            "validates",
            "validate",
            "established",
            "establishment",
            "establishes",
            "establish",
            "proven",
            "proved",
            "proves",
            "proof",
            "actual native",
            "execute",
            "executes",
            "executed",
            "executing",
            "execution",
            "execution completed",
            "agent ran",
            "agents ran",
            "subagent ran",
            "subagents ran",
            "agent did run",
            "subagent did run",
            "agent has run",
            "subagent has run",
            "agent was run",
            "subagent was run",
            "agent is running",
            "subagent is running",
            "agent running",
            "subagent running",
            "has run",
            "have run",
            "is running",
            "was running",
            "running",
            "finished",
            "returned",
            "succeeded",
            "produced output",
            "produces output",
            "generated output",
            "emitted output",
            "yielded output",
            "produced result",
            "produced a result",
            "generated result",
            "generated a result",
            "emitted result",
            "yielded result",
            "yielded a result",
            "returned output",
            "returned result",
            "output was generated",
            "output is generated",
            "output has been generated",
            "output got generated",
            "output was produced",
            "output is produced",
            "output has been produced",
            "output got produced",
            "output was emitted",
            "output is emitted",
            "output has been emitted",
            "output got emitted",
            "output was yielded",
            "output is yielded",
            "output has been yielded",
            "output got yielded",
            "output was returned",
            "output is returned",
            "output has been returned",
            "output got returned",
            "output was created",
            "output is created",
            "output has been created",
            "output got created",
            "output was written",
            "output is written",
            "output has been written",
            "output got written",
            "result was generated",
            "result is generated",
            "result has been generated",
            "result got generated",
            "result was produced",
            "result is produced",
            "result has been produced",
            "result got produced",
            "result was emitted",
            "result is emitted",
            "result has been emitted",
            "result got emitted",
            "result was yielded",
            "result is yielded",
            "result has been yielded",
            "result got yielded",
            "result was returned",
            "result is returned",
            "result has been returned",
            "result got returned",
            "result was created",
            "result is created",
            "result has been created",
            "result got created",
            "result was written",
            "result is written",
            "result has been written",
            "result got written",
            "response was generated",
            "response is generated",
            "response has been generated",
            "response got generated",
            "response was produced",
            "response is produced",
            "response has been produced",
            "response got produced",
            "response was emitted",
            "response is emitted",
            "response has been emitted",
            "response got emitted",
            "response was yielded",
            "response is yielded",
            "response has been yielded",
            "response got yielded",
            "response was returned",
            "response is returned",
            "response has been returned",
            "response got returned",
            "response was created",
            "response is created",
            "response has been created",
            "response got created",
            "response was written",
            "response is written",
            "response has been written",
            "response got written",
            "ended",
            "ended successfully",
            "has returned",
            "did return",
            "multi_agent_v1 runs",
            "multi_agent_v1 ran",
            "runs lanes",
            "ran lanes",
            "ran native",
            "runs native",
            "run native",
            "tool event",
            "tool-event",
            "tool verified",
            "tool-verified",
            "completed through",
            "ran for",
        )
        allowed_action_terms = (
            "record",
            "records",
            "recorded",
            "list",
            "lists",
            "listed",
            "store",
            "stores",
            "stored",
            "include",
            "includes",
            "included",
            "contain",
            "contains",
            "contained",
            "name",
            "names",
            "named",
            "describe",
            "describes",
            "described",
            "treats",
            "treat",
        )
        for sentence in claim_sentences:
            normalized_sentence = " ".join(re.sub(r"[^\w]+", " ", sentence).split())
            sentence_tokens = normalized_sentence.split()
            if not any(token_phrase_present(sentence_tokens, term) for term in native_claim_terms):
                continue
            if not token_phrase_present(sentence_tokens, "lead recorded"):
                return False
            if not any(token_phrase_present(sentence_tokens, term) for term in allowed_action_terms):
                return False
            allowed_scope = (
                token_phrase_present(sentence_tokens, "lifecycle evidence")
                or token_phrase_present(sentence_tokens, "lifecycle fields")
                or token_phrase_present(sentence_tokens, "lifecycle entries")
                or token_phrase_present(sentence_tokens, "lifecycle ledger")
                or token_phrase_present(sentence_tokens, "lifecycle records")
                or token_phrase_present(sentence_tokens, "lifecycle record")
            )
            if not allowed_scope:
                return False
            if any(token_phrase_present(sentence_tokens, term) for term in overclaim_terms):
                return False
    verification_body = section_body(("verification", "verify", "驗證"))
    evidence_lines = concrete_final_report_evidence_lines(verification_body, workflow_dir)
    if len(evidence_lines) < 3:
        return False
    if not any("--mode final" in line.lower() and any(term in line.lower() for term in FINAL_REPORT_RESULT_TERMS) for line in evidence_lines):
        return False
    if not any(("runner-evidence.json" in line.lower() or "lane-runs/" in line.lower()) for line in evidence_lines):
        return False
    if not any(any(term in line.lower() for term in FINAL_REPORT_COMMAND_TERMS) for line in evidence_lines):
        return False
    existing_refs = final_report_existing_refs(workflow_dir, evidence_lines)
    if len(existing_refs) < 2:
        return False
    (
        current_round,
        current_verify_refs,
        verify_check_terms,
        verify_criterion_terms,
        verify_check_evidence_pairs,
    ) = (
        final_report_current_verify_terms(workflow_dir)
    )
    if current_round and not any(
        ref.startswith(f"rounds/{current_round}/lane-runs/") for ref in existing_refs
    ):
        return False
    verification_lower = verification_body.lower()
    evidence_unit_lowers = [unit.lower() for unit in evidence_lines]
    if current_verify_refs and not any(ref.lower() in verification_lower for ref in current_verify_refs):
        return False
    verify_evidence_terms = verify_check_terms + verify_criterion_terms
    required_verify_term_matches = min(2, len(verify_evidence_terms))
    if required_verify_term_matches and (
        sum(1 for term in verify_evidence_terms if report_term_in_text(term, verification_lower))
        < required_verify_term_matches
    ):
        return False
    required_pair_matches = min(2, len(verify_check_evidence_pairs))
    if required_pair_matches and (
        sum(
            1
            for check_name, check_evidence in verify_check_evidence_pairs
            if any(
                report_term_in_text(check_name, unit)
                and report_evidence_in_text(check_evidence, unit)
                for unit in evidence_unit_lowers
            )
        )
        < required_pair_matches
    ):
        return False
    normalized_lines = [
        " ".join(line.lower().split())
        for line in content_lines
        if word_count(line) >= FINAL_REPORT_MIN_SECTION_WORDS
    ]
    if normalized_lines and len(set(normalized_lines)) / len(normalized_lines) < 0.7:
        return False
    evidence_anchor_count = sum(1 for anchor in FINAL_REPORT_EVIDENCE_ANCHORS if anchor in lowered)
    return evidence_anchor_count >= 3


def validate_terminal_candidate_template(workflow_dir: Path) -> list[str]:
    """Pre-boundary gate for terminal enums and authoritative report substance."""

    failures: list[str] = []
    try:
        state = json.loads((workflow_dir / "state.json").read_text(encoding="utf-8"))
        (workflow_dir / "final-report.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"terminal template is unreadable: {exc}"]
    if not isinstance(state, dict):
        return ["terminal template state must be an object"]
    if state.get("status") != "complete":
        failures.append('terminal template requires state.status="complete"')
    if state.get("final_status") != "complete":
        failures.append('terminal template requires state.final_status="complete"')
    rounds = state.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        failures.append("terminal template requires at least one round state")
    else:
        for index, round_state in enumerate(rounds, start=1):
            if not isinstance(round_state, dict) or round_state.get("status") != "complete":
                failures.append(
                    f'terminal template requires state.rounds[{index}].status="complete"'
                )
    if not final_report_has_substance(
        workflow_dir / "final-report.md",
        allow_exact_token_markers=True,
    ):
        failures.append(
            "terminal template report failed authoritative final-report substance validation"
        )
    return failures


def planned_lane_specs(orchestration: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    by_round: dict[str, dict[str, dict[str, Any]]] = {}
    if not orchestration:
        return by_round
    for round_plan in orchestration.get("rounds", []):
        if not isinstance(round_plan, dict):
            continue
        round_id = round_plan.get("round_id")
        if not isinstance(round_id, str):
            continue
        by_round.setdefault(round_id, {})
        for lane_spec in round_plan.get("lanes", []):
            if not isinstance(lane_spec, dict) or not lane_spec.get("enabled", True):
                continue
            lane_id = lane_spec.get("id")
            if isinstance(lane_id, str):
                by_round[round_id][lane_id] = lane_spec
    return by_round


def _routing_snapshot_path(
    workflow_dir: Path, value: Any, expected_name: str, label: str, failures: list[str]
) -> Path | None:
    if value != expected_name:
        failures.append(f"{label} must be the portable root artifact {expected_name}")
        return None
    path = workflow_dir / expected_name
    if not path.is_file():
        failures.append(f"Missing routing snapshot: {path}")
        return None
    return path


def validate_model_routing(
    workflow_dir: Path,
    orchestration: dict[str, Any] | None,
    mode: str,
    failures: list[str],
) -> dict[str, Any] | None:
    if not isinstance(orchestration, dict):
        return None
    block = orchestration.get("model_routing")
    orchestrator = orchestration.get("orchestrator")
    runner_mode = orchestrator.get("runner_mode") if isinstance(orchestrator, dict) else None
    requirement = orchestration.get("model_routing_requirement")
    mandatory_requirement = {
        "mode": "mandatory_native",
        "fallback": "fail_closed",
        "effort_source": "runtime_turn_context",
        "actual_dispatch_evidence": "child_runtime_attested",
    }
    mandatory = (
        orchestration.get("schema_version") == "agent-loops.orchestration.v2"
        and runner_mode == "codex_builtin_subagents"
    )
    if mandatory and requirement != mandatory_requirement:
        failures.append(
            "orchestration.model_routing_requirement must be the exact mandatory-native contract"
        )
    if requirement is not None and runner_mode != "codex_builtin_subagents":
        failures.append(
            "orchestration.model_routing_requirement requires codex_builtin_subagents"
        )
    if block is None:
        if mandatory:
            failures.append(
                "orchestration.model_routing is mandatory for new Codex native workflows"
            )
        return None
    if not isinstance(block, dict):
        failures.append("orchestration.model_routing must be an object")
        return None
    if block.get("enabled") is False:
        if mandatory:
            failures.append(
                "orchestration.model_routing cannot be disabled for new Codex native workflows"
            )
        return None
    if block.get("enabled") is not True:
        failures.append("orchestration.model_routing.enabled must be boolean")
        return None
    activation = block.get("activation")
    expected_keys = {
        "enabled",
        "activation",
        "adapter",
        "policy_snapshot",
        "capability_snapshot",
        "reasoning_effort",
        "dispatch_gate",
    }
    if mandatory:
        expected_keys.add("session_profile")
    missing = sorted(expected_keys - set(block))
    unknown = sorted(set(block) - expected_keys)
    if missing:
        failures.append("orchestration.model_routing missing keys: " + ", ".join(missing))
    if unknown:
        failures.append("orchestration.model_routing unknown keys: " + ", ".join(unknown))
    if block.get("activation") not in {"native_default", "explicit_opt_in"}:
        failures.append(
            "orchestration.model_routing.activation must be native_default or "
            "explicit_opt_in"
        )
    if block.get("adapter") != "codex_builtin_subagents":
        failures.append("orchestration.model_routing.adapter must be codex_builtin_subagents")
    if runner_mode != "codex_builtin_subagents":
        failures.append("enabled model routing requires codex_builtin_subagents")
    dispatch_gate = block.get("dispatch_gate")
    if not isinstance(dispatch_gate, str) or "--mode planned" not in dispatch_gate:
        failures.append("orchestration.model_routing.dispatch_gate must require --mode planned")

    snapshots: dict[str, dict[str, Any]] = {}
    snapshot_specs = (
        ("policy_snapshot", "routing-policy.json", validate_policy_snapshot),
        ("capability_snapshot", "runtime-capabilities.json", validate_capability_snapshot),
    )
    for key, filename, validator in snapshot_specs:
        ref = block.get(key)
        label = f"orchestration.model_routing.{key}"
        if not isinstance(ref, dict):
            failures.append(f"{label} must be an object")
            continue
        if set(ref) != {"path", "snapshot_id", "content_sha256"}:
            failures.append(
                f"{label} must contain only path, snapshot_id, and content_sha256"
            )
            continue
        path = _routing_snapshot_path(workflow_dir, ref.get("path"), filename, label, failures)
        if path is None:
            continue
        value = load_json(path, failures)
        try:
            validated = validator(value)
        except RoutingError as exc:
            failures.append(f"{path}: {exc}")
            continue
        if ref.get("snapshot_id") != validated.get("snapshot_id"):
            failures.append(f"{label}.snapshot_id does not match {filename}")
        if ref.get("content_sha256") != validated.get("content_sha256"):
            failures.append(f"{label}.content_sha256 does not match {filename}")
        snapshots[key] = validated
    policy = snapshots.get("policy_snapshot")
    capabilities = snapshots.get("capability_snapshot")
    if not isinstance(policy, dict) or not isinstance(capabilities, dict):
        return None
    if mandatory and capabilities.get("schema_version") != CAPABILITY_SCHEMA:
        failures.append(
            "native-default model routing requires runtime-capabilities.v3 with "
            "model-selectable spawn evidence"
        )
    if mandatory:
        profile = block.get("session_profile")
        expected_profile_keys = {
            "schema_version",
            "runtime",
            "session_id",
            "model",
            "reasoning_effort",
            "source",
            "event_line",
            "event_sha256",
            "prefix_sha256",
            "observed_at",
        }
        if not isinstance(profile, dict) or set(profile) != expected_profile_keys:
            failures.append(
                "orchestration.model_routing.session_profile must contain the exact "
                "runtime turn-context binding"
            )
        else:
            if profile.get("schema_version") != "agent-workflow.routing-session-profile.v1":
                failures.append(
                    "orchestration.model_routing.session_profile schema is invalid"
                )
            if profile.get("runtime") != "codex" or profile.get("source") != "runtime_turn_context":
                failures.append(
                    "orchestration.model_routing.session_profile must come from Codex turn_context"
                )
            effort = capabilities.get("reasoning_effort", {}).get("value")
            if profile.get("reasoning_effort") != effort:
                failures.append(
                    "orchestration.model_routing.session_profile effort must match capabilities"
                )
    if block.get("reasoning_effort") != capabilities.get("reasoning_effort"):
        failures.append(
            "orchestration.model_routing.reasoning_effort must match the locked "
            "user-session effort in runtime-capabilities.json"
        )
    if mode in STRICT_MODES:
        orchestrator = orchestration.get("orchestrator")
        adapter = (
            orchestrator.get("runner_adapter")
            if isinstance(orchestrator, dict)
            and isinstance(orchestrator.get("runner_adapter"), dict)
            else {}
        )
        try:
            validate_capability_availability_evidence(
                adapter.get("capability_evidence"), capabilities
            )
        except RoutingError as exc:
            failures.append(f"orchestration.model_routing capability availability: {exc}")

    decisions: dict[str, dict[str, Any]] = {}
    seen_packets: set[str] = set()
    seen_decisions: set[str] = set()
    rounds = orchestration.get("rounds")
    if not isinstance(rounds, list):
        return None
    for round_index, round_plan in enumerate(rounds, start=1):
        if not isinstance(round_plan, dict):
            continue
        round_id = round_plan.get("round_id")
        lanes = round_plan.get("lanes")
        if not isinstance(round_id, str) or not isinstance(lanes, list):
            continue
        for lane_index, lane_spec in enumerate(lanes, start=1):
            if not isinstance(lane_spec, dict) or lane_spec.get("enabled") is not True:
                continue
            label = (
                f"orchestration.rounds[{round_index}].lanes[{lane_index}].routing"
            )
            if lane_spec.get("agent_count") != 1:
                failures.append(f"{label} requires agent_count=1 in routed v2")
            routing = lane_spec.get("routing")
            if not isinstance(routing, dict):
                failures.append(f"{label} is required when model routing is enabled")
                continue
            try:
                decision = validate_planned_decision(
                    routing,
                    policy,
                    capabilities,
                    allow_draft=mode == "scaffold",
                    evidence_root=workflow_dir,
                )
            except RoutingError as exc:
                failures.append(f"{label}: {exc}")
                continue
            if mode != "scaffold" and decision.get("status") != "planned":
                failures.append(
                    f"{label}.status must be planned before dispatch; "
                    f"{decision.get('status')} requires a human/blocked gate"
                )
            facts = decision.get("facts")
            if isinstance(facts, dict) and facts.get("role") != lane_spec.get("lane"):
                failures.append(f"{label}.facts.role must match the lane type")
            packet_id = decision.get("packet_id")
            decision_id = decision.get("decision_id")
            if isinstance(packet_id, str):
                if packet_id in seen_packets:
                    failures.append(f"routing packet_id duplicates {packet_id}")
                seen_packets.add(packet_id)
            if isinstance(decision_id, str):
                if decision_id in seen_decisions:
                    failures.append(f"routing decision_id duplicates {decision_id}")
                seen_decisions.add(decision_id)
            decisions[f"{round_id}:{lane_spec.get('id')}"] = decision
    result = {
        "policy": policy,
        "capabilities": capabilities,
        "decisions": decisions,
    }
    validate_routing_verifier_plans(
        result,
        planned_lane_specs(orchestration),
        mode,
        failures,
    )
    return result


def validate_routing_verifier_plans(
    routing: dict[str, Any] | None,
    expected_lanes: dict[str, dict[str, dict[str, Any]]],
    mode: str,
    failures: list[str],
) -> None:
    if not routing or mode == "scaffold":
        return
    policy = routing["policy"]
    decisions = routing["decisions"]
    for lane_key, decision in decisions.items():
        floor = decision.get("verification_floor")
        if not isinstance(floor, dict) or floor.get("required") is not True:
            continue
        round_id, author_lane_id = lane_key.split(":", 1)
        round_lanes = expected_lanes.get(round_id, {})
        verifier_ids = floor.get("verifier_lane_ids")
        independent_ids = floor.get("independent_of_lane_ids")
        required_evidence = floor.get("required_evidence")
        if not isinstance(verifier_ids, list) or not verifier_ids:
            failures.append(f"{lane_key} requires verifier_lane_ids before dispatch")
            continue
        if not isinstance(independent_ids, list) or author_lane_id not in independent_ids:
            failures.append(f"{lane_key} verifier plan must name its author lane as independent")
        if not isinstance(required_evidence, list) or not required_evidence:
            failures.append(f"{lane_key} requires evidence names before dispatch")
        for independent_lane_id in independent_ids if isinstance(independent_ids, list) else []:
            if independent_lane_id not in round_lanes:
                failures.append(
                    f"{lane_key} independent author lane {independent_lane_id} must be enabled"
                )
        for verifier_lane_id in verifier_ids:
            verifier_spec = round_lanes.get(str(verifier_lane_id))
            if not isinstance(verifier_spec, dict) or verifier_spec.get("lane") != "verify":
                failures.append(
                    f"{lane_key} verifier {verifier_lane_id} must be an enabled verify lane"
                )
                continue
            if isinstance(independent_ids, list) and verifier_lane_id in independent_ids:
                failures.append(f"{lane_key} verifier cannot be independent_of itself")
            verifier_key = f"{round_id}:{verifier_lane_id}"
            verifier_decision = decisions.get(verifier_key)
            selected = (
                verifier_decision.get("selected")
                if isinstance(verifier_decision, dict)
                and verifier_decision.get("status") == "planned"
                else None
            )
            if not isinstance(selected, dict):
                failures.append(
                    f"{lane_key} verifier {verifier_lane_id} needs a planned route before dispatch"
                )
                continue
            try:
                if route_rank(policy, selected) < route_rank(policy, floor["minimum_route"]):
                    failures.append(
                        f"{lane_key} planned verifier {verifier_lane_id} is below verifier floor"
                    )
            except (KeyError, RoutingError) as exc:
                failures.append(f"{lane_key} planned verifier route: {exc}")


def validate_routed_attempt_ledgers(
    routing: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    mode: str,
    failures: list[str],
    *,
    workflow_dir: Path | None = None,
) -> None:
    if not routing or mode not in EXECUTION_MODES:
        return
    policy = routing["policy"]
    capabilities = routing["capabilities"]
    seen_attempt_ids: set[str] = set()
    for lane_key, decision in routing["decisions"].items():
        record = evidence.get(lane_key)
        if not isinstance(record, dict):
            failures.append(f"{lane_key} missing routed runner attempts")
            continue
        try:
            validate_attempts(
                record,
                decision,
                policy,
                capabilities,
                evidence_root=workflow_dir,
                require_completed_terminal=mode == "final",
            )
        except RoutingError as exc:
            failures.append(f"runner-evidence {lane_key}: {exc}")
            continue
        for attempt in record.get("attempts", []):
            attempt_id = attempt.get("attempt_id") if isinstance(attempt, dict) else None
            if not isinstance(attempt_id, str):
                continue
            if attempt_id in seen_attempt_ids:
                failures.append(f"runner-evidence duplicate global attempt_id {attempt_id}")
            seen_attempt_ids.add(attempt_id)


def validate_routed_terminal_outcomes(
    routing: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    lane_outputs: list[dict[str, Any]],
    state: dict[str, Any] | None,
    mode: str,
    failures: list[str],
) -> None:
    if not routing or mode != "executed":
        return
    outputs: dict[str, dict[str, Any]] = {}
    for output in lane_outputs:
        round_id = output.get("round_id")
        lane_id = output.get("lane_id")
        if isinstance(round_id, str) and isinstance(lane_id, str):
            outputs[f"{round_id}:{lane_id}"] = output
    round_states = state_rounds(state)
    for lane_key in routing["decisions"]:
        record = evidence.get(lane_key)
        attempts = record.get("attempts") if isinstance(record, dict) else None
        terminal = attempts[-1] if isinstance(attempts, list) and attempts else None
        if not isinstance(terminal, dict) or terminal.get("outcome") == "completed":
            continue
        output = outputs.get(lane_key)
        gate = output.get("gate") if isinstance(output, dict) else None
        lane_gate = gate.get("decision") if isinstance(gate, dict) else None
        round_id = lane_key.split(":", 1)[0]
        round_gate = round_states.get(round_id, {}).get("gate_decision")
        if lane_gate == "pass" or round_gate == "pass":
            failures.append(
                f"{lane_key} terminal outcome {terminal.get('outcome')} cannot retain a pass gate"
            )


def _runner_identity(record: dict[str, Any] | None) -> set[str]:
    if not isinstance(record, dict):
        return set()
    return {
        str(record[key])
        for key in ("agent_id", "native_handle")
        if isinstance(record.get(key), str) and record.get(key)
    }


def _terminal_actual_route(record: dict[str, Any] | None) -> dict[str, str] | None:
    attempts = record.get("attempts") if isinstance(record, dict) else None
    if not isinstance(attempts, list) or not attempts:
        return None
    terminal = attempts[-1]
    if not isinstance(terminal, dict) or terminal.get("outcome") != "completed":
        return None
    route = terminal.get("actual_route")
    return route if isinstance(route, dict) else None


def _passing_verify_checks(output: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(output, dict):
        return {}
    payload = output.get("payload")
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "pass":
            continue
        name = check.get("name") or check.get("id")
        if isinstance(name, str) and name:
            result[name] = check
    return result


def validate_routing_verifier_floors(
    workflow_dir: Path,
    routing: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    expected_lanes: dict[str, dict[str, dict[str, Any]]],
    lane_outputs: list[dict[str, Any]],
    mode: str,
    failures: list[str],
) -> None:
    if not routing or mode != "final":
        return
    outputs: dict[str, dict[str, Any]] = {}
    for output in lane_outputs:
        round_id = output.get("round_id")
        lane_id = output.get("lane_id")
        if isinstance(round_id, str) and isinstance(lane_id, str):
            outputs[f"{round_id}:{lane_id}"] = output
    policy = routing["policy"]
    for lane_key, decision in routing["decisions"].items():
        floor = decision.get("verification_floor")
        if not isinstance(floor, dict) or floor.get("required") is not True:
            continue
        round_id, author_lane_id = lane_key.split(":", 1)
        verifier_ids = floor.get("verifier_lane_ids")
        independent = floor.get("independent_of_lane_ids")
        required_evidence = floor.get("required_evidence")
        missing_action = floor.get("missing_evidence_action")
        if not isinstance(verifier_ids, list) or not verifier_ids:
            failures.append(
                f"{lane_key} requires verifier_lane_ids; missing action is {missing_action}"
            )
            continue
        if not isinstance(independent, list) or author_lane_id not in independent:
            failures.append(f"{lane_key} verifier floor must be independent of its author lane")
        if not isinstance(required_evidence, list) or not required_evidence:
            failures.append(
                f"{lane_key} verifier floor requires substantive evidence bindings; "
                f"missing action is {missing_action}"
            )
        author_identities: dict[str, set[str]] = {}
        for independent_lane_id in independent if isinstance(independent, list) else []:
            independent_key = f"{round_id}:{independent_lane_id}"
            identity = _runner_identity(evidence.get(independent_key))
            if not identity:
                failures.append(
                    f"{lane_key} independent author {independent_lane_id} identity evidence is required"
                )
            author_identities[str(independent_lane_id)] = identity
        evidence_names: set[str] = set()
        for verifier_lane_id in verifier_ids:
            verifier_key = f"{round_id}:{verifier_lane_id}"
            verifier_spec = expected_lanes.get(round_id, {}).get(str(verifier_lane_id))
            if not isinstance(verifier_spec, dict) or verifier_spec.get("lane") != "verify":
                failures.append(f"{lane_key} verifier {verifier_lane_id} must be a planned verify lane")
                continue
            if verifier_lane_id in independent:
                failures.append(f"{lane_key} verifier cannot be independent_of itself")
            verifier_output = outputs.get(verifier_key)
            verifier_gate = (
                verifier_output.get("gate")
                if isinstance(verifier_output, dict)
                and isinstance(verifier_output.get("gate"), dict)
                else {}
            )
            if (
                not isinstance(verifier_output, dict)
                or verifier_output.get("status") != "complete"
                or verifier_gate.get("decision") != "pass"
            ):
                failures.append(
                    f"{lane_key} verifier {verifier_lane_id} must complete with a pass gate"
                )
            verifier_record = evidence.get(verifier_key)
            verifier_identity = _runner_identity(verifier_record)
            if not verifier_identity:
                failures.append(f"{lane_key} verifier identity evidence is required")
            for independent_lane_id, author_identity in author_identities.items():
                if author_identity and verifier_identity & author_identity:
                    failures.append(
                        f"{lane_key} verifier identity must differ from independent author "
                        f"{independent_lane_id}"
                    )
            actual_route = _terminal_actual_route(verifier_record)
            if actual_route is None:
                failures.append(f"{lane_key} verifier {verifier_lane_id} lacks terminal actual route")
            else:
                try:
                    if route_rank(policy, actual_route) < route_rank(
                        policy, floor["minimum_route"]
                    ):
                        failures.append(
                            f"{lane_key} verifier {verifier_lane_id} is below verifier floor"
                        )
                except RoutingError as exc:
                    failures.append(f"{lane_key} verifier route: {exc}")
            checks = _passing_verify_checks(verifier_output)
            for name, check in checks.items():
                if has_substantive_evidence(check.get("evidence"), workflow_dir):
                    evidence_names.add(name)
        if isinstance(required_evidence, list):
            missing = sorted(set(required_evidence) - evidence_names)
            if missing:
                failures.append(
                    f"{lane_key} verifier missing passing substantive checks: "
                    + ", ".join(missing)
                    + f"; action={missing_action}"
                )


def validate_swarm_routing_projection(
    workflow_dir: Path,
    routing: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    mode: str,
    failures: list[str],
) -> None:
    if not routing or mode != "final":
        return
    path = workflow_dir / "swarm-card.json"
    if not path.is_file():
        return
    card = load_json(path, failures)
    if not isinstance(card, dict):
        return
    phases = card.get("phases")
    if not isinstance(phases, list):
        failures.append(f"{path}.phases must be a list")
        return
    agents: list[dict[str, Any]] = []
    for phase in phases:
        phase_agents = phase.get("agents") if isinstance(phase, dict) else None
        if isinstance(phase_agents, list):
            agents.extend(item for item in phase_agents if isinstance(item, dict))
    for lane_key, decision in routing["decisions"].items():
        round_id, lane_id = lane_key.split(":", 1)
        matches = [
            item
            for item in agents
            if item.get("lane_id") == lane_id
            and item.get("round_id", round_id) == round_id
        ]
        if len(matches) != 1:
            failures.append(f"{path} must contain exactly one agent projection for {lane_key}")
            continue
        expected = expected_swarm_projection(decision, evidence.get(lane_key))
        if matches[0].get("routing") != expected:
            failures.append(f"{path} routing projection drift for {lane_key}")


def validate_swarm_card_state(
    workflow_dir: Path,
    mode: str,
    failures: list[str],
) -> None:
    path = workflow_dir / "swarm-card.json"
    if not path.is_file():
        return
    card = load_json(path, failures)
    if not isinstance(card, dict):
        return
    schema = card.get("schema_version")
    if schema not in {CARD_SCHEMA, LEGACY_CARD_SCHEMA}:
        # Pre-v2 projection-only cards remain readable by the routing validator.
        return
    try:
        validate_card(card)
    except SwarmCardError as exc:
        failures.append(f"{path}: {exc}")


def state_rounds(state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    rounds: dict[str, dict[str, Any]] = {}
    if not isinstance(state, dict):
        return rounds
    for item in state.get("rounds", []):
        if isinstance(item, dict) and isinstance(item.get("round_id"), str):
            rounds[item["round_id"]] = item
    return rounds


def validate_runner_mode_consistency(
    state: dict[str, Any] | None,
    orchestration: dict[str, Any] | None,
    failures: list[str],
) -> None:
    if not isinstance(state, dict) or not isinstance(orchestration, dict):
        return
    state_mode = state.get("runner_mode")
    orch = orchestration.get("orchestrator") if isinstance(orchestration.get("orchestrator"), dict) else {}
    orch_mode = orch.get("runner_mode")
    if state_mode != orch_mode:
        failures.append("state.runner_mode must match orchestration.orchestrator.runner_mode")
    state_adapter = state.get("runner_adapter") if isinstance(state.get("runner_adapter"), dict) else {}
    orch_adapter = orch.get("runner_adapter") if isinstance(orch.get("runner_adapter"), dict) else {}
    if state_adapter.get("mode") != state_mode:
        failures.append("state.runner_adapter.mode must match state.runner_mode")
    if orch_adapter.get("mode") != orch_mode:
        failures.append("orchestration.orchestrator.runner_adapter.mode must match runner_mode")
    if state_adapter.get("dispatch_surface") != orch_adapter.get("dispatch_surface"):
        failures.append("state/orchestration runner_adapter.dispatch_surface must match")


def validate_progress_consistency(
    state: dict[str, Any] | None,
    orchestration: dict[str, Any] | None,
    expected_lanes: dict[str, dict[str, dict[str, Any]]],
    failures: list[str],
    mode: str,
) -> None:
    if mode not in STRICT_MODES or not isinstance(state, dict) or not isinstance(orchestration, dict):
        return
    orch = orchestration.get("orchestrator") if isinstance(orchestration.get("orchestrator"), dict) else {}
    if state.get("round_budget") != orch.get("round_budget"):
        failures.append("state.round_budget must match orchestration.orchestrator.round_budget")

    state_by_round = state_rounds(state)
    state_round_items = state.get("rounds") if isinstance(state.get("rounds"), list) else []
    orchestration_round_items = (
        orchestration.get("rounds") if isinstance(orchestration.get("rounds"), list) else []
    )
    state_round_ids = set(state_by_round)
    orchestration_round_ids = set(expected_lanes)
    missing_from_state = sorted(orchestration_round_ids - state_round_ids)
    missing_from_orchestration = sorted(state_round_ids - orchestration_round_ids)
    if missing_from_state:
        failures.append(
            "orchestration round(s) missing from state.rounds: " + ", ".join(missing_from_state)
        )
    if missing_from_orchestration:
        failures.append(
            "state round(s) missing from orchestration.rounds: " + ", ".join(missing_from_orchestration)
        )

    round_budget = state.get("round_budget")
    declared_count = max(len(state_round_items), len(orchestration_round_items))
    if isinstance(round_budget, int) and declared_count > round_budget:
        failures.append("declared round count exceeds round_budget")

    for round_id, round_state in state_by_round.items():
        enabled = round_state.get("enabled_lanes")
        if not isinstance(enabled, list):
            continue
        state_enabled = {item for item in enabled if isinstance(item, str)}
        planned_enabled = set(expected_lanes.get(round_id, {}))
        if state_enabled != planned_enabled:
            failures.append(
                f"{round_id}.enabled_lanes must match enabled orchestration lane ids"
            )


def runner_expectations(
    state: dict[str, Any] | None,
    orchestration: dict[str, Any] | None,
) -> tuple[Any, Any, Any]:
    state_mode = state.get("runner_mode") if isinstance(state, dict) else None
    orch = orchestration.get("orchestrator") if isinstance(orchestration, dict) else None
    orch_adapter = orch.get("runner_adapter") if isinstance(orch, dict) else None
    state_adapter = state.get("runner_adapter") if isinstance(state, dict) else None
    dispatch_surface = None
    if isinstance(state_adapter, dict):
        dispatch_surface = state_adapter.get("dispatch_surface")
    if dispatch_surface is None and isinstance(orch_adapter, dict):
        dispatch_surface = orch_adapter.get("dispatch_surface")
    capability = None
    if isinstance(state_adapter, dict):
        capability = state_adapter.get("capability_evidence")
    if capability is None and isinstance(orch_adapter, dict):
        capability = orch_adapter.get("capability_evidence")
    return state_mode, dispatch_surface, capability


def runner_evidence_by_lane(
    workflow_dir: Path,
    mode: str,
    state: dict[str, Any] | None,
    orchestration: dict[str, Any] | None,
    failures: list[str],
) -> dict[str, dict[str, Any]]:
    path = workflow_dir / "runner-evidence.json"
    expected_mode, expected_surface, expected_capability = runner_expectations(state, orchestration)
    native_mode = expected_mode in {"codex_builtin_subagents", "claude_code_builtin_subagents"}
    if not path.exists():
        if mode == "final" and native_mode:
            failures.append(f"Missing native runner evidence file: {path}")
        return {}
    value = load_json(path, failures)
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version") != "agent-loops.runner-evidence.v1":
        failures.append(f"{path}.schema_version must be agent-loops.runner-evidence.v1")
    if value.get("cross_runtime_calls_allowed") is not False:
        failures.append(f"{path}.cross_runtime_calls_allowed must be false")
    root_evidence_level = value.get("evidence_level")
    if mode in STRICT_MODES and native_mode:
        if root_evidence_level not in RUNNER_EVIDENCE_LEVELS:
            failures.append(f"{path}.evidence_level must be one of {sorted(RUNNER_EVIDENCE_LEVELS)}")
        if root_evidence_level == "tool_event_verified":
            failures.append(
                f"{path}.evidence_level tool_event_verified requires an external runtime attestation verifier; use lead_recorded until that verifier exists"
            )
            event_log_path = value.get("event_log_path")
            if not isinstance(event_log_path, str) or not event_log_path:
                failures.append(f"{path}.event_log_path is required for tool_event_verified")
            elif not (workflow_dir / event_log_path).is_file():
                failures.append(f"{path}.event_log_path must point to an existing workflow artifact")
        if value.get("runner_mode") != expected_mode:
            failures.append(f"{path}.runner_mode must match state.runner_mode")
        if value.get("dispatch_surface") != expected_surface:
            failures.append(f"{path}.dispatch_surface must match runner_adapter.dispatch_surface")
        capability = value.get("capability_evidence")
        if not isinstance(capability, dict):
            failures.append(f"{path}.capability_evidence is required for native modes")
        else:
            if capability.get("verified") is not True:
                failures.append(f"{path}.capability_evidence.verified must be true")
            if not capability.get("summary"):
                failures.append(f"{path}.capability_evidence.summary is required")
        if isinstance(expected_capability, dict) and isinstance(capability, dict):
            if capability.get("verified") != expected_capability.get("verified"):
                failures.append(f"{path}.capability_evidence.verified must match state/orchestration")
    agents = value.get("agents")
    if not isinstance(agents, list):
        if mode == "final" and native_mode:
            failures.append(f"{path}.agents must be a list for final native validation")
        return {}
    event_records: dict[str, dict[str, Any]] = {}
    if mode in STRICT_MODES and native_mode and root_evidence_level == "tool_event_verified":
        event_log_path = value.get("event_log_path")
        if isinstance(event_log_path, str):
            event_log = load_json(workflow_dir / event_log_path, failures)
            if isinstance(event_log, dict):
                events = event_log.get("events")
            else:
                events = None
            if isinstance(event_log, dict):
                if event_log.get("schema_version") != "agent-loops.runner-events.v1":
                    failures.append(f"{path}.event_log_path.schema_version must be agent-loops.runner-events.v1")
                source = event_log.get("source") or event_log.get("generated_by")
                if source not in DISPATCH_SURFACES:
                    failures.append(f"{path}.event_log_path source/generated_by must be a dispatch surface")
                provenance = event_log.get("provenance") if isinstance(event_log.get("provenance"), dict) else {}
                if provenance.get("trusted_capture") is not True:
                    failures.append(f"{path}.event_log_path.provenance.trusted_capture must be true")
                capture_source = provenance.get("capture_source") or provenance.get("source")
                if capture_source not in TRUSTED_RUNNER_EVENT_CAPTURE_SOURCES:
                    failures.append(
                        f"{path}.event_log_path.provenance.capture_source must be a trusted runtime event source"
                    )
                transcript_hash = provenance.get("transcript_hash")
                event_hash_chain = provenance.get("event_hash_chain")
                has_transcript_hash = isinstance(transcript_hash, str) and len(transcript_hash.strip()) >= 16
                has_event_hash_chain = (
                    isinstance(event_hash_chain, list)
                    and bool(event_hash_chain)
                    and all(isinstance(item, str) and len(item.strip()) >= 16 for item in event_hash_chain)
                )
                if not (has_transcript_hash or has_event_hash_chain):
                    failures.append(
                        f"{path}.event_log_path.provenance must include transcript_hash or event_hash_chain"
                    )
            if not isinstance(events, list):
                failures.append(f"{path}.event_log_path must contain an events list")
            else:
                for event_index, event in enumerate(events, start=1):
                    if not isinstance(event, dict):
                        failures.append(f"{path}.event_log_path.events[{event_index}] must be an object")
                        continue
                    event_id = event.get("event_ref") or event.get("id")
                    if not isinstance(event_id, str) or not event_id:
                        failures.append(f"{path}.event_log_path.events[{event_index}].event_ref is required")
                        continue
                    if event_id in event_records:
                        failures.append(f"{path}.event_log_path duplicate event_ref {event_id}")
                    event_records[event_id] = event
    by_lane: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(agents, start=1):
        label = f"{path}.agents[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{label} must be an object")
            continue
        lane_id = item.get("lane_id")
        if not isinstance(lane_id, str):
            failures.append(f"{label}.lane_id must be a string")
            continue
        round_id = item.get("round_id")
        if mode == "final" and native_mode and not isinstance(round_id, str):
            failures.append(f"{label}.round_id must be a string for final native validation")
        key = f"{round_id}:{lane_id}" if isinstance(round_id, str) else lane_id
        if key in by_lane:
            failures.append(f"{label} duplicates runner evidence for {key}")
        by_lane[key] = item
        item_evidence_level = item.get("evidence_level")
        if mode == "final" and native_mode:
            if item_evidence_level not in RUNNER_EVIDENCE_LEVELS:
                failures.append(f"{label}.evidence_level must be one of {sorted(RUNNER_EVIDENCE_LEVELS)}")
            elif item_evidence_level != root_evidence_level:
                failures.append(f"{label}.evidence_level must match runner-evidence.json.evidence_level")
            if item_evidence_level == "tool_event_verified" and not isinstance(item.get("event_ref"), str):
                failures.append(f"{label}.event_ref is required for tool_event_verified")
            elif item_evidence_level == "tool_event_verified":
                event_ref = str(item.get("event_ref"))
                event_chain = [
                    event
                    for event in event_records.values()
                    if event.get("event_ref") == event_ref or event.get("lane_ref") == event_ref
                ]
                event_types = {event.get("event_type") for event in event_chain if isinstance(event, dict)}
                required_event_types = {"spawn", "wait", "close"}
                if not required_event_types.issubset(event_types):
                    failures.append(f"{label}.event_ref must bind spawn, wait, and close events")
                for event in event_chain:
                    for event_key in ("round_id", "lane_id", "spawn_tool", "output_path"):
                        if event.get(event_key) != item.get(event_key):
                            failures.append(f"{label}.event_ref {event_key} must match event log")
                    if event.get("event_type") == "wait" and event.get("wait_status") != item.get("wait_status"):
                        failures.append(f"{label}.event_ref wait_status must match event log")
                    if event.get("event_type") == "close" and event.get("close_status") != item.get("close_status"):
                        failures.append(f"{label}.event_ref close_status must match event log")
                    if item.get("agent_id") and event.get("agent_id") != item.get("agent_id"):
                        failures.append(f"{label}.event_ref agent_id must match event log")
                    if item.get("native_handle") and event.get("native_handle") != item.get("native_handle"):
                        failures.append(f"{label}.event_ref native_handle must match event log")
        if mode != "final" and not isinstance(round_id, str) and lane_id not in by_lane:
            by_lane[lane_id] = item
    return by_lane


def validate_runner_lifecycle(
    lane_output_path: Path,
    lane_output: dict[str, Any],
    expected: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
    failures: list[str],
    mode: str,
) -> None:
    if mode != "final" or not expected:
        return
    runner = expected.get("runner") if isinstance(expected.get("runner"), dict) else {}
    if runner.get("mode") == "manual_simulation":
        return
    lane_id = lane_output.get("lane_id")
    round_id = lane_output.get("round_id")
    lane = lane_output.get("lane")
    dispatch_method = runner.get("dispatch_method")
    if dispatch_method in {"lead_agent_repair", "lead_owned"}:
        if lane not in LEAD_OWNED_LANES:
            failures.append(f"{lane_output_path} {lane} lane cannot be lead_owned in final native mode")
        return
    record = evidence.get(f"{round_id}:{lane_id}")
    if not isinstance(record, dict) and mode != "final":
        record = evidence.get(str(lane_id))
    if not isinstance(record, dict):
        failures.append(f"{lane_output_path} missing runner lifecycle evidence for native lane {lane_id}")
        return
    if record.get("round_id") != round_id:
        failures.append(f"{lane_output_path} runner evidence round_id must match lane output")
    evidence_level = record.get("evidence_level")
    execution_kind = record.get("execution_kind")
    if evidence_level == "tool_event_verified":
        if execution_kind != "native_spawned":
            failures.append(
                f"{lane_output_path} runner evidence execution_kind must be native_spawned for tool_event_verified"
            )
    elif evidence_level == "lead_recorded":
        if execution_kind != "lead_recorded_native":
            failures.append(
                f"{lane_output_path} runner evidence execution_kind must be lead_recorded_native for lead_recorded"
            )
    else:
        failures.append(f"{lane_output_path} runner evidence evidence_level is invalid")
    if not (record.get("agent_id") or record.get("native_handle")):
        failures.append(f"{lane_output_path} runner evidence agent_id or native_handle is required")
    dispatch_surface = None
    runner_mode = runner.get("mode")
    if runner_mode == "codex_builtin_subagents":
        dispatch_surface = "multi_agent_v1"
    elif runner_mode == "claude_code_builtin_subagents":
        dispatch_surface = "claude_code_agent_tool"
    if dispatch_surface and record.get("spawn_tool") != dispatch_surface:
        failures.append(f"{lane_output_path} runner evidence spawn_tool must be {dispatch_surface}")
    if record.get("wait_status") != "completed":
        failures.append(f"{lane_output_path} runner evidence wait_status must be completed")
    if record.get("close_status") != "closed":
        failures.append(f"{lane_output_path} runner evidence close_status must be closed")
    output_path = record.get("output_path")
    if output_path:
        workflow_dir = lane_output_path.parents[3]
        expected_path = workflow_dir / str(output_path)
        if expected_path.resolve() != lane_output_path.resolve():
            failures.append(f"{lane_output_path} runner evidence output_path does not match lane output")
    else:
        failures.append(f"{lane_output_path} runner evidence output_path is required in final mode")


def high_finding_ids(lane_outputs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []
    for output in lane_outputs:
        round_id = output.get("round_id")
        lane_id = output.get("lane_id")
        for index, (_, finding) in enumerate(all_lane_findings(output), start=1):
            severity = finding.get("severity")
            if severity_at_or_above(severity, HIGH_SEVERITIES):
                finding_id = finding.get("id") or f"{round_id}:{lane_id}:finding-{index}"
                ids.append((str(finding_id), str(severity)))
    return ids


def validate_resolution_shape(
    item: Any,
    label: str,
    severity_by_id: dict[str, str],
    failures: list[str],
    workflow_dir: Path | None = None,
) -> str | None:
    valid_resolutions = {
        "repaired_by",
        "deferred_with_reason",
        "human_gate",
        "rejected_with_reason",
        "blocked",
    }
    if not isinstance(item, dict):
        failures.append(f"{label} must be an object")
        return None
    finding_id = item.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id:
        failures.append(f"{label}.finding_id must be a non-empty string")
        return None
    resolution = item.get("resolution")
    if resolution not in valid_resolutions:
        failures.append(f"{label}.resolution must be one of {sorted(valid_resolutions)}")
        return None
    severity = severity_by_id.get(finding_id)
    if severity is None:
        failures.append(f"{label}.finding_id must match a collected P2+ finding id")
    if resolution == "repaired_by":
        checks = item.get("checks")
        if not (
            has_text(item.get("repaired_by"))
            or has_text(item.get("repair_ref"))
            or item.get("verification_evidence")
            or checks
        ):
            failures.append(
                f"{label}.repaired_by requires repaired_by, repair_ref, verification_evidence, or checks"
            )
        if checks is not None:
            validate_passing_checks(checks, f"{label}.checks", failures, workflow_dir)
    elif resolution == "deferred_with_reason":
        for key in ("owner", "scope", "reason", "non_blocking_rationale"):
            if not has_text(item.get(key)):
                failures.append(f"{label}.{key} is required for deferred_with_reason")
        if severity in BLOCKING_SEVERITIES:
            failures.append(f"{label} cannot defer {severity} without human_gate")
    elif resolution == "human_gate":
        if not has_text(item.get("decision")):
            failures.append(f"{label}.decision is required for human_gate")
        if not (has_text(item.get("evidence")) or item.get("verification_evidence")):
            failures.append(f"{label}.evidence is required for human_gate")
    elif resolution == "rejected_with_reason":
        if not has_text(item.get("reason")):
            failures.append(f"{label}.reason is required for rejected_with_reason")
    elif resolution == "blocked":
        if not (has_text(item.get("reason")) or has_text(item.get("blocker"))):
            failures.append(f"{label}.reason or blocker is required for blocked")
    return finding_id


def validate_finding_resolutions(
    lane_outputs: list[dict[str, Any]],
    integrations: dict[str, dict[str, Any]],
    state: dict[str, Any] | None,
    failures: list[str],
    mode: str,
    workflow_dir: Path | None = None,
) -> None:
    if mode != "final" or not isinstance(state, dict) or state.get("final_status") != "complete":
        return
    high_ids = high_finding_ids(lane_outputs)
    severity_by_id: dict[str, str] = {}
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for finding_id, severity in high_ids:
        if finding_id in seen_ids:
            duplicate_ids.add(finding_id)
        seen_ids.add(finding_id)
        severity_by_id.setdefault(finding_id, severity)
    for finding_id in sorted(duplicate_ids):
        failures.append(f"Duplicate high-severity finding id: {finding_id}")
    resolution_ids: set[str] = set()
    current_integration = integrations.get(str(state.get("current_round")))
    final_is_pass_like = (
        isinstance(current_integration, dict)
        and current_integration.get("status") == "complete"
        and current_integration.get("stop_reason") in PASS_STOP_REASONS
    )
    for round_id, integration in integrations.items():
        resolutions = integration.get("finding_resolutions", [])
        if not isinstance(resolutions, list):
            continue
        for index, item in enumerate(resolutions, start=1):
            finding_id = validate_resolution_shape(
                item,
                f"{round_id}.integration.finding_resolutions[{index}]",
                severity_by_id,
                failures,
                workflow_dir,
            )
            if finding_id:
                if finding_id in resolution_ids:
                    failures.append(f"Duplicate finding resolution id: {finding_id}")
                if (
                    isinstance(item, dict)
                    and item.get("resolution") == "blocked"
                    and final_is_pass_like
                ):
                    failures.append(
                        f"{round_id}.integration.finding_resolutions[{index}] cannot be blocked when final stop_reason is pass-like"
                    )
                resolution_ids.add(finding_id)
    for finding_id, severity in high_ids:
        if finding_id not in resolution_ids:
            failures.append(f"Missing final finding resolution for {severity} finding {finding_id}")


def validate_round_graph(
    workflow_dir: Path,
    state: dict[str, Any] | None,
    orchestration: dict[str, Any] | None,
    integrations: dict[str, dict[str, Any]],
    failures: list[str],
    mode: str,
) -> None:
    if not isinstance(state, dict) or not isinstance(orchestration, dict):
        return
    declared_rounds = set()
    for round_plan in orchestration.get("rounds", []):
        if isinstance(round_plan, dict) and isinstance(round_plan.get("round_id"), str):
            declared_rounds.add(round_plan["round_id"])
    for round_id in state_rounds(state):
        declared_rounds.add(round_id)
    current_round = state.get("current_round")
    if isinstance(current_round, str) and current_round not in declared_rounds:
        failures.append("state.current_round must exist in state/orchestration rounds")
    for round_id in declared_rounds:
        if not (workflow_dir / "rounds" / round_id).is_dir():
            failures.append(f"Missing declared round directory: {round_id}")
    if mode not in STRICT_MODES:
        return
    for round_id, integration in integrations.items():
        next_round = integration.get("next_round")
        if isinstance(next_round, str) and not (workflow_dir / "rounds" / next_round).is_dir():
            failures.append(f"{round_id}.integration.next_round points to missing round {next_round}")
        repair_packets = integration.get("repair_packets")
        stop_reason = integration.get("stop_reason")
        if isinstance(repair_packets, list) and repair_packets:
            unresolved = [
                packet
                for packet in repair_packets
                if isinstance(packet, dict)
                and packet.get("status") not in {"resolved", "resolved_in_plan", "deferred", "rejected"}
            ]
            if stop_reason in {"verify_pass", "pass"} and unresolved:
                failures.append(f"{round_id}.integration has unresolved repair_packets with pass stop_reason")
    if mode == "final":
        if state.get("final_status") != "complete":
            failures.append("state.final_status must be complete in final mode")


def validate_final_terminal_state(
    workflow_dir: Path,
    state: dict[str, Any] | None,
    integrations: dict[str, dict[str, Any]],
    lane_outputs: list[dict[str, Any]],
    failures: list[str],
    mode: str,
) -> None:
    if mode != "final" or not isinstance(state, dict):
        return
    if not final_report_has_substance(workflow_dir / "final-report.md"):
        failures.append(
            "final-report.md must contain substantive outcome, verification, risk, "
            "token usage, stop, and runner details"
        )
    if state.get("final_status") != "complete":
        return
    if state.get("status") != "complete":
        failures.append("state.status must be complete when final_status is complete")
    current_round = state.get("current_round")
    round_state = state_rounds(state).get(current_round) if isinstance(current_round, str) else None
    if not isinstance(round_state, dict):
        failures.append("state.current_round must reference a round state in final mode")
        return
    integration = integrations.get(str(current_round))
    if not isinstance(integration, dict):
        failures.append("current round integration is required in final mode")
        return
    if round_state.get("status") != "complete":
        failures.append("current round status must be complete in final mode")
    if integration.get("status") != "complete":
        failures.append("current round integration.status must be complete in final mode")
    gate_decision = round_state.get("gate_decision")
    stop_reason = integration.get("stop_reason")
    if stop_reason not in TERMINAL_STOP_REASONS:
        failures.append(f"current round integration.stop_reason must be one of {sorted(TERMINAL_STOP_REASONS)}")
    if stop_reason in PASS_STOP_REASONS and gate_decision != "pass":
        failures.append("current round gate_decision must be pass for pass stop_reason")
    if stop_reason in PASS_STOP_REASONS and integration.get("next_round") is not None:
        failures.append("current round next_round must be null for pass stop_reason")
    verification_evidence = integration.get("verification_evidence")
    if stop_reason in PASS_STOP_REASONS and (
        not isinstance(verification_evidence, list) or not verification_evidence
    ):
        failures.append("current round verification_evidence must be non-empty for pass stop_reason")
    current_outputs = [
        output
        for output in lane_outputs
        if output.get("round_id") == current_round and output.get("lane") == "verify"
    ]
    has_verify_pass = any(
        output.get("status") == "complete"
        and isinstance(output.get("gate"), dict)
        and output["gate"].get("decision") == "pass"
        for output in current_outputs
    )
    if stop_reason in PASS_STOP_REASONS and not has_verify_pass:
        failures.append("final pass requires at least one completed verify lane with gate.decision pass")

    if stop_reason in PASS_STOP_REASONS:
        for output in lane_outputs:
            if output.get("round_id") != current_round:
                continue
            gate = output.get("gate") if isinstance(output.get("gate"), dict) else {}
            if gate.get("decision") != "pass":
                failures.append(
                    f"{current_round}.{output.get('lane_id')}.gate.decision must be pass before final pass"
                )

    for round_id, item in state_rounds(state).items():
        if round_id == current_round:
            continue
        integration_item = integrations.get(round_id)
        if item.get("status") not in NON_CURRENT_TERMINAL_ROUND_STATUSES:
            failures.append(f"{round_id}.state.status must be terminal before final mode")
        gate_decision = item.get("gate_decision")
        if gate_decision == "pending":
            failures.append(f"{round_id}.state.gate_decision must not be pending before final mode")
        if not isinstance(integration_item, dict):
            failures.append(f"{round_id}.integration is required before final mode")
            continue
        integration_status = integration_item.get("status")
        round_status = item.get("status")
        stop_reason_item = integration_item.get("stop_reason")
        if integration_item.get("status") not in NON_CURRENT_TERMINAL_INTEGRATION_STATUSES:
            failures.append(f"{round_id}.integration.status must be terminal before final mode")
        if not stop_reason_item:
            failures.append(f"{round_id}.integration.stop_reason is required before final mode")
        elif stop_reason_item not in TERMINAL_STOP_REASONS | REVISION_STOP_REASONS:
            failures.append(f"{round_id}.integration.stop_reason is not a recognized terminal reason")
        if round_status in {"passed", "complete"} and gate_decision != "pass":
            failures.append(f"{round_id}.state.gate_decision must be pass for completed rounds")
        if round_status == "revising" and gate_decision != "revise":
            failures.append(f"{round_id}.state.gate_decision must be revise for revising rounds")
        if round_status == "blocked" and gate_decision != "blocked":
            failures.append(f"{round_id}.state.gate_decision must be blocked for blocked rounds")
        if integration_status == "complete" and stop_reason_item not in PASS_STOP_REASONS:
            failures.append(f"{round_id}.integration.stop_reason must be pass-like for complete integration")
        if integration_status == "revise" and stop_reason_item not in REVISION_STOP_REASONS:
            failures.append(f"{round_id}.integration.stop_reason must be revise for revised rounds")
        if round_status in {"passed", "complete"}:
            if integration_status != "complete":
                failures.append(f"{round_id}.integration.status must be complete for completed rounds")
            if stop_reason_item in PASS_STOP_REASONS:
                for output in lane_outputs:
                    if output.get("round_id") != round_id:
                        continue
                    gate = output.get("gate") if isinstance(output.get("gate"), dict) else {}
                    if output.get("status") != "complete" or gate.get("decision") != "pass":
                        failures.append(
                            f"{round_id}.{output.get('lane_id')}.gate.decision must be pass for completed rounds"
                        )
            if integration_item.get("next_round") is not None:
                failures.append(f"{round_id}.integration.next_round must be null for completed rounds")
        if round_status == "revising":
            if integration_status != "revise":
                failures.append(f"{round_id}.integration.status must be revise for revising rounds")
            if stop_reason_item != "revise":
                failures.append(f"{round_id}.integration.stop_reason must be revise for revising rounds")
        if round_status == "revising" and not integration_item.get("next_round"):
            failures.append(f"{round_id}.integration.next_round is required for revised rounds")
        if round_status == "blocked":
            if integration_status != "blocked":
                failures.append(f"{round_id}.integration.status must be blocked for blocked rounds")
            if stop_reason_item != "blocked":
                failures.append(f"{round_id}.integration.stop_reason must be blocked for blocked rounds")
            if integration_item.get("next_round") is not None:
                failures.append(f"{round_id}.integration.next_round must be null for blocked rounds")


def validate_v1(workflow_dir: Path, mode: str, require_lane_runs: bool) -> list[str]:
    failures: list[str] = []
    if not workflow_dir.is_dir():
        return [f"Missing workflow directory: {workflow_dir}"]
    for name in REQUIRED_V1_FILES:
        require_nonempty_file(workflow_dir / name, failures)
    require_dir(workflow_dir / "rounds", failures)

    state = validate_state(workflow_dir, failures, mode) if (workflow_dir / "state.json").is_file() else None
    orchestration = (
        validate_orchestration(workflow_dir, failures, mode)
        if (workflow_dir / "orchestration.json").is_file()
        else None
    )
    clean_runtime = None
    if isinstance(orchestration, dict):
        runtime_contract = state.get("runtime_contract") if isinstance(state, dict) else None
        clean_required = (
            isinstance(runtime_contract, dict)
            and runtime_contract.get("required_schema") == CLEAN_RUNTIME_SCHEMA
        )
        try:
            clean_runtime = validate_clean_runtime_contract(
                orchestration,
                allow_draft=mode == "scaffold",
                required=clean_required,
            )
        except CleanOrchestratorError as exc:
            failures.append(f"clean orchestrator runtime: {exc}")
    execution_policy, efficiency_lanes = validate_execution_efficiency_contract(
        workflow_dir,
        orchestration,
        mode,
        failures,
    )
    model_routing = validate_model_routing(
        workflow_dir, orchestration, mode, failures
    )
    validate_runner_mode_consistency(state, orchestration, failures)
    token_usage = validate_token_usage(workflow_dir, failures, mode)
    validate_token_schema_requirement(state, token_usage, mode, failures)
    expected_lanes = planned_lane_specs(orchestration)
    validate_progress_consistency(state, orchestration, expected_lanes, failures, mode)
    lifecycle_evidence = runner_evidence_by_lane(workflow_dir, mode, state, orchestration, failures)
    if clean_runtime is not None and mode in EXECUTION_MODES:
        try:
            runner_value = json.loads(
                (workflow_dir / "runner-evidence.json").read_text(encoding="utf-8")
            )
            validate_completion_density(
                runner_value.get("completion_density"),
                orchestration,
                final=mode == "final",
            )
        except (OSError, json.JSONDecodeError, CleanOrchestratorError) as exc:
            failures.append(f"clean orchestrator completion density: {exc}")
        if mode == "final" or (workflow_dir / "runtime-observations.json").is_file():
            try:
                validate_runtime_observations(
                    workflow_dir,
                    final=mode == "final",
                )
            except RuntimeHarnessError as exc:
                failures.append(f"clean orchestrator raw runtime observations: {exc}")
    mandatory_routing_runtime = (
        isinstance(orchestration, dict)
        and orchestration.get("schema_version") == "agent-loops.orchestration.v2"
        and isinstance(orchestration.get("orchestrator"), dict)
        and orchestration["orchestrator"].get("runner_mode")
        == "codex_builtin_subagents"
    )
    if mandatory_routing_runtime and mode == "final" and clean_runtime is None:
        try:
            validate_runtime_observations(workflow_dir, final=True)
        except RuntimeHarnessError as exc:
            failures.append(f"mandatory routing child-runtime observations: {exc}")
    validate_token_participant_coverage(token_usage, lifecycle_evidence, mode, failures)
    validate_execution_efficiency_runtime(
        workflow_dir,
        execution_policy,
        efficiency_lanes,
        lifecycle_evidence,
        mode,
        failures,
    )
    validate_routed_attempt_ledgers(
        model_routing,
        lifecycle_evidence,
        mode,
        failures,
        workflow_dir=workflow_dir,
    )
    observed_lanes: dict[str, set[str]] = {}
    lane_outputs_for_resolution: list[dict[str, Any]] = []
    integrations: dict[str, dict[str, Any]] = {}

    round_ids: set[str] = set()
    if isinstance(orchestration, dict):
        for round_plan in orchestration.get("rounds", []):
            if isinstance(round_plan, dict) and isinstance(round_plan.get("round_id"), str):
                round_ids.add(round_plan["round_id"])
    if isinstance(state, dict):
        current_round = state.get("current_round")
        if isinstance(current_round, str):
            round_ids.add(current_round)
        for round_state in state.get("rounds", []):
            if isinstance(round_state, dict) and isinstance(round_state.get("round_id"), str):
                round_ids.add(round_state["round_id"])

    if not round_ids:
        failures.append("No rounds declared in state.json or orchestration.json")
    if mode in STRICT_MODES:
        rounds_dir = workflow_dir / "rounds"
        filesystem_rounds = {
            path.name
            for path in rounds_dir.iterdir()
            if path.is_dir()
        } if rounds_dir.is_dir() else set()
        undeclared_rounds = sorted(filesystem_rounds - round_ids)
        if undeclared_rounds:
            failures.append(
                "Undeclared round directories under rounds/: " + ", ".join(undeclared_rounds)
            )

    for round_id in sorted(round_ids):
        round_dir = workflow_dir / "rounds" / round_id
        lane_runs_dir = round_dir / "lane-runs"
        require_dir(round_dir, failures)
        require_dir(lane_runs_dir, failures)
        require_nonempty_file(round_dir / "integration.md", failures)
        require_nonempty_file(round_dir / "integration.json", failures)
        if (round_dir / "integration.json").is_file():
            integration = validate_integration(round_dir / "integration.json", round_id, failures)
            if isinstance(integration, dict):
                integrations[round_id] = integration

        if mode in STRICT_MODES and lane_runs_dir.is_dir():
            nested_entries = sorted(
                path.relative_to(lane_runs_dir)
                for path in lane_runs_dir.rglob("*")
                if path.is_dir() or path.parent != lane_runs_dir
            )
            if nested_entries:
                failures.append(
                    f"Nested entries under {round_id}/lane-runs are not allowed in {mode} mode: "
                    + ", ".join(str(path) for path in nested_entries[:10])
                )
        lane_outputs = sorted(lane_runs_dir.glob("*.json")) if lane_runs_dir.is_dir() else []
        for lane_output_path in lane_outputs:
            expected = expected_lanes.get(round_id, {}).get(lane_output_path.stem)
            if mode in STRICT_MODES and expected is None:
                failures.append(f"Unexpected lane output in {round_id}: {lane_output_path.name}")
            lane_output = validate_lane_output(lane_output_path, failures, mode, expected)
            if not lane_output:
                continue
            output_round = lane_output.get("round_id")
            lane_id = lane_output.get("lane_id")
            if output_round != round_id:
                failures.append(f"{lane_output_path}.round_id must match {round_id}")
            if isinstance(lane_id, str):
                observed_lanes.setdefault(round_id, set()).add(lane_id)
            if mode == "final" and expected:
                if lane_output.get("status") != "complete":
                    failures.append(
                        f"{lane_output_path}.status must be complete for enabled lane in final mode"
                    )
            elif mode in EXECUTION_MODES and expected and expected.get("required") is True:
                if lane_output.get("status") != "complete":
                    failures.append(
                        f"{lane_output_path}.status must be complete for required lane in {mode} mode"
                    )
            if isinstance(lane_output, dict):
                lane_outputs_for_resolution.append(lane_output)
                validate_runner_lifecycle(
                    lane_output_path,
                    lane_output,
                    expected,
                    lifecycle_evidence,
                    failures,
                    mode,
                )

    if require_lane_runs or mode in EXECUTION_MODES:
        for round_id, lane_specs in expected_lanes.items():
            lane_ids = set(lane_specs)
            missing = sorted(lane_ids - observed_lanes.get(round_id, set()))
            if missing:
                failures.append(
                    f"Missing lane run output(s) for {round_id}: {', '.join(missing)}"
                )

    validate_routed_terminal_outcomes(
        model_routing,
        lifecycle_evidence,
        lane_outputs_for_resolution,
        state,
        mode,
        failures,
    )

    validate_round_graph(workflow_dir, state, orchestration, integrations, failures, mode)
    validate_final_terminal_state(
        workflow_dir,
        state,
        integrations,
        lane_outputs_for_resolution,
        failures,
        mode,
    )
    validate_finding_resolutions(
        lane_outputs_for_resolution,
        integrations,
        state,
        failures,
        mode,
        workflow_dir,
    )
    validate_routing_verifier_floors(
        workflow_dir,
        model_routing,
        lifecycle_evidence,
        expected_lanes,
        lane_outputs_for_resolution,
        mode,
        failures,
    )
    validate_swarm_card_state(workflow_dir, mode, failures)
    validate_swarm_routing_projection(
        workflow_dir,
        model_routing,
        lifecycle_evidence,
        mode,
        failures,
    )
    validate_terminal_commit_manifest(
        workflow_dir,
        failures,
        mode,
        required=clean_runtime is not None,
    )

    return failures


def validate_legacy(workflow_dir: Path) -> list[str]:
    failures: list[str] = []
    if not workflow_dir.is_dir():
        return [f"Missing workflow directory: {workflow_dir}"]
    for name in LEGACY_REQUIRED_FILES:
        require_nonempty_file(workflow_dir / name, failures)
    for name in LEGACY_REQUIRED_DIRS:
        require_dir(workflow_dir / name, failures)

    state_path = workflow_dir / "state.json"
    if state_path.is_file():
        state = load_json(state_path, failures)
        require_keys(
            state,
            ("title", "slug", "status", "approval", "packets", "verification"),
            str(state_path),
            failures,
        )

    packets_dir = workflow_dir / "packets"
    results_dir = workflow_dir / "results"
    packet_files = sorted(packets_dir.glob("*.md")) if packets_dir.is_dir() else []
    result_files = sorted(results_dir.glob("*.md")) if results_dir.is_dir() else []
    if not packet_files:
        failures.append("No packet files found under packets/")
    if not result_files:
        failures.append("No result files found under results/")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", help="Path to .workflow/<slug>")
    parser.add_argument(
        "--mode",
        choices=("scaffold", "planned", "executed", "final", "terminal-template"),
        help=(
            "Validation strictness for v1 workspaces. Required for v1 unless "
            "--require-lane-runs is used, which maps to executed for compatibility. "
            "Routed workflows must pass planned before dispatch."
        ),
    )
    parser.add_argument(
        "--require-lane-runs",
        action="store_true",
        help=(
            "Compatibility alias for executed lane-output requirements. Prefer "
            "--mode executed or --mode final."
        ),
    )
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if (workflow_dir / "orchestration.json").is_file():
        if args.mode == "terminal-template":
            failures = validate_terminal_candidate_template(workflow_dir)
            mode = "v1/terminal-template"
            if failures:
                print("Workflow verification failed:")
                for failure in failures:
                    print(f"- {failure}")
                return 1
            print(f"Workflow verification passed ({mode}): {workflow_dir}")
            return 0
        if args.mode:
            validation_mode = args.mode
        elif args.require_lane_runs:
            validation_mode = "executed"
        else:
            failures = [
                "v1 workflows require --mode scaffold, --mode planned, --mode executed, "
                "or --mode final. "
                "Bare validation is not final evidence."
            ]
            validation_mode = "v1"
            mode = "v1"
            if failures:
                print("Workflow verification failed:")
                for failure in failures:
                    print(f"- {failure}")
                return 1
        failures = validate_v1(workflow_dir, validation_mode, args.require_lane_runs)
        mode = f"v1/{validation_mode}"
    else:
        failures = validate_legacy(workflow_dir)
        mode = "legacy"

    if failures:
        print("Workflow verification failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Workflow verification passed ({mode}): {workflow_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
