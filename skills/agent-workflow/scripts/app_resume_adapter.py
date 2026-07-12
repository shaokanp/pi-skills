#!/usr/bin/env python3
"""One-shot, digest-bound Codex App Server continuation adapter.

The adapter resumes exactly one sealed failed session and projects the appended
turn back into the JSONL contract consumed by ``workflow_runtime.py``.  The
watchdog owns this process group; this module owns no durable service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from artifact_store import ArtifactError, create_once_json


class ResumeAdapterFailure(RuntimeError):
    """Raised when a continuation cannot be proven safe and exact."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SESSION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_MAX_RPC_EVENTS = 8192
_MAX_RPC_LINE = 8 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _safe_absolute_file(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ResumeAdapterFailure(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ResumeAdapterFailure(f"{label} is missing or unsafe")
    return path.resolve(strict=True)


def _load_spec(path: Path, expected_sha256: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ResumeAdapterFailure("resume spec is missing or unsafe")
    payload = path.read_bytes()
    if not payload or len(payload) > _MAX_SPEC_BYTES or _digest(payload) != expected_sha256:
        raise ResumeAdapterFailure("resume spec digest or size is invalid")
    try:
        spec = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeAdapterFailure("resume spec is invalid JSON") from exc
    expected = {
        "schema_version", "workflow_id", "authority_revision", "generation_id",
        "phase_id", "task_id", "lineage_id", "plan_sha256",
        "generation_claim_ref", "generation_claim_sha256", "runtime_bundle_sha256",
        "failed_result_ref", "failed_result_sha256", "causal_receipt_ref",
        "causal_receipt_sha256", "session_id", "codex_home", "session_rollout_path",
        "prior_rollout_sha256", "prior_rollout_size", "codex_binary_sha256", "model",
        "reasoning_effort", "permissions_profile", "cwd", "runtime_workspace_roots",
        "prompt", "task_prompt_sha256", "resume_nonce", "output_schema_path",
        "output_schema_sha256", "audit_marker", "run_root", "claim_ref",
        "turn_claim_ref", "terminal_ref",
    }
    if not isinstance(spec, dict) or set(spec) != expected:
        raise ResumeAdapterFailure("resume spec contract is invalid")
    if spec["schema_version"] != "agent-workflow.app-resume-spec.vnext.v1":
        raise ResumeAdapterFailure("resume spec schema is invalid")
    for key in (
        "workflow_id", "generation_id", "phase_id", "task_id", "lineage_id",
        "generation_claim_ref", "failed_result_ref", "causal_receipt_ref", "model",
        "reasoning_effort", "permissions_profile", "prompt", "audit_marker", "claim_ref",
        "turn_claim_ref", "terminal_ref", "resume_nonce",
    ):
        if not isinstance(spec[key], str) or not spec[key]:
            raise ResumeAdapterFailure(f"resume spec {key} is invalid")
    for key in (
        "plan_sha256", "generation_claim_sha256", "runtime_bundle_sha256",
        "failed_result_sha256", "causal_receipt_sha256", "prior_rollout_sha256",
        "codex_binary_sha256", "output_schema_sha256",
        "task_prompt_sha256",
    ):
        if not isinstance(spec[key], str) or not _DIGEST.fullmatch(spec[key]):
            raise ResumeAdapterFailure(f"resume spec {key} is invalid")
    if not isinstance(spec["authority_revision"], int) or isinstance(spec["authority_revision"], bool) or spec["authority_revision"] < 1:
        raise ResumeAdapterFailure("resume authority revision is invalid")
    if not isinstance(spec["prior_rollout_size"], int) or isinstance(spec["prior_rollout_size"], bool) or spec["prior_rollout_size"] < 1:
        raise ResumeAdapterFailure("resume prior rollout size is invalid")
    if not isinstance(spec["runtime_workspace_roots"], list) or not spec["runtime_workspace_roots"] or not all(isinstance(item, str) and Path(item).is_absolute() for item in spec["runtime_workspace_roots"]):
        raise ResumeAdapterFailure("resume runtime roots are invalid")
    if not _SESSION.fullmatch(spec["session_id"]):
        raise ResumeAdapterFailure("resume session id is invalid")
    suffix = f"\n\n[agent_workflow_resume_nonce={spec['resume_nonce']}]"
    if not spec["prompt"].endswith(suffix) or _digest(
        spec["prompt"][:-len(suffix)].encode("utf-8")
    ) != spec["task_prompt_sha256"]:
        raise ResumeAdapterFailure("resume prompt nonce binding is invalid")
    return spec, payload


def _load_json_file(path: Path, expected_sha256: str, label: str) -> Any:
    if _digest_file(path) != expected_sha256:
        raise ResumeAdapterFailure(f"{label} digest drifted")
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeAdapterFailure(f"{label} is invalid JSON") from exc


class _RPC:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.observed = 0

    def send(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise ResumeAdapterFailure("App Server stdin is unavailable")
        self.process.stdin.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def receive(self, *, request_id: int | None = None, terminal: bool = False) -> dict[str, Any]:
        if self.process.stdout is None:
            raise ResumeAdapterFailure("App Server stdout is unavailable")
        while self.observed < _MAX_RPC_EVENTS:
            line = self.process.stdout.readline(_MAX_RPC_LINE + 1)
            if not line:
                raise ResumeAdapterFailure("App Server ended before the expected response")
            if len(line.encode()) > _MAX_RPC_LINE:
                raise ResumeAdapterFailure("App Server RPC line exceeded the limit")
            self.observed += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResumeAdapterFailure("App Server emitted invalid JSON") from exc
            if not isinstance(event, dict):
                continue
            if request_id is not None and event.get("id") == request_id:
                return event
            if terminal and event.get("method") in {"turn/completed", "turn/failed"}:
                return event
        raise ResumeAdapterFailure("App Server RPC event limit exceeded")


def _session_events(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ResumeAdapterFailure("session rollout is missing, unsafe, or oversized")
    events: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResumeAdapterFailure("session rollout contains invalid JSON") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


def _project_turn(
    path: Path,
    prior_size: int,
    session_id: str,
    expected_prompt: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    payload = path.read_bytes()
    if len(payload) <= prior_size:
        raise ResumeAdapterFailure("resumed session did not append a turn")
    appended = payload[prior_size:]
    if not appended.startswith(b"\n") and payload[prior_size - 1:prior_size] != b"\n":
        raise ResumeAdapterFailure("session append boundary is ambiguous")
    events = _session_events(path)
    appended_events: list[dict[str, Any]] = []
    for line in appended.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResumeAdapterFailure("resumed session append contains invalid JSON") from exc
        if isinstance(event, dict):
            appended_events.append(event)
    appended_task_events = []
    for event in appended_events:
        if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "task_complete":
            appended_task_events.append(event["payload"])
    if len(appended_task_events) != 1:
        raise ResumeAdapterFailure("resume must append exactly one terminal turn")
    terminal = appended_task_events[0]
    turn_id = terminal.get("turn_id")
    if not isinstance(turn_id, str) or not _SESSION.fullmatch(turn_id):
        raise ResumeAdapterFailure("resumed turn id is invalid")
    contexts = [
        event.get("payload") for event in appended_events
        if event.get("type") == "turn_context"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("turn_id") == turn_id
    ]
    usages: list[dict[str, Any]] = []
    for event in appended_events:
        if event.get("type") != "event_msg" or not isinstance(event.get("payload"), dict):
            continue
        payload_value = event["payload"]
        if payload_value.get("type") == "token_count" and isinstance(payload_value.get("info"), dict):
            usage = payload_value["info"].get("last_token_usage")
            if isinstance(usage, dict):
                usages.append(usage)
    if len(contexts) != 1 or not usages:
        raise ResumeAdapterFailure("resumed turn lacks exact context or token evidence")
    user_messages: list[list[str]] = []
    for event in appended_events:
        payload_value = event.get("payload") if isinstance(event, dict) else None
        if (
            event.get("type") == "response_item"
            and isinstance(payload_value, dict)
            and payload_value.get("type") == "message"
            and payload_value.get("role") == "user"
        ):
            content = payload_value.get("content")
            if not isinstance(content, list) or not content:
                raise ResumeAdapterFailure("resume prompt authority drifted")
            text_parts: list[str] = []
            for part in content:
                if not (
                    isinstance(part, dict)
                    and set(part) in ({"type", "text"}, {"type", "text", "text_elements"})
                    and part.get("type") == "input_text"
                    and isinstance(part.get("text"), str)
                ):
                    raise ResumeAdapterFailure("resume prompt authority drifted")
                text_parts.append(part["text"])
            user_messages.append(text_parts)
    host_preamble_ok = False
    if len(user_messages) == 2:
        preamble_parts = user_messages[0]
        environment_pattern = r"<environment_context>[\s\S]*</environment_context>"
        agents_pattern = r"# AGENTS\.md instructions for [^\n]+\n\n<INSTRUCTIONS>[\s\S]*</INSTRUCTIONS>"
        if len(preamble_parts) == 1:
            preamble = preamble_parts[0]
            host_preamble_ok = (
                re.fullmatch(environment_pattern, preamble) is not None
                or re.fullmatch(
                    agents_pattern + r"\n" + environment_pattern,
                    preamble,
                ) is not None
            )
        elif len(preamble_parts) == 2:
            host_preamble_ok = (
                re.fullmatch(agents_pattern, preamble_parts[0]) is not None
                and re.fullmatch(environment_pattern, preamble_parts[1]) is not None
            )
    if not (
        (len(user_messages) == 1 and user_messages[0] == [expected_prompt])
        or (
            len(user_messages) == 2
            and host_preamble_ok
            and user_messages[1] == [expected_prompt]
        )
    ):
        raise ResumeAdapterFailure("resume prompt authority drifted")
    usage = usages[-1]
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in (input_tokens, output_tokens)):
        raise ResumeAdapterFailure("resumed turn token evidence is invalid")
    message = terminal.get("last_agent_message")
    if not isinstance(message, str):
        raise ResumeAdapterFailure("resumed turn lacks a terminal agent message")
    projected = [
        {"type": "thread.started", "thread_id": session_id},
        {"type": "app.resume.attested", "thread_id": session_id, "turn_id": turn_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    ]
    return turn_id, projected, contexts[0]


def _validate_effective_context(context: dict[str, Any], spec: dict[str, Any]) -> None:
    if (
        context.get("cwd") != spec["cwd"]
        or context.get("model") != spec["model"]
        or (context.get("effort") or context.get("collaboration_mode", {}).get("settings", {}).get("reasoning_effort")) != spec["reasoning_effort"]
        or context.get("workspace_roots") != spec["runtime_workspace_roots"]
    ):
        raise ResumeAdapterFailure("resumed turn effective settings drifted")


def _seal_terminal(
    run_root: Path,
    spec: dict[str, Any],
    spec_sha256: str,
    rollout: Path,
    turn_id: str,
    projected: list[dict[str, Any]],
) -> None:
    terminal = {
        "schema_version": "agent-workflow.app-resume-terminal.vnext.v1",
        "spec_sha256": spec_sha256,
        "session_id": spec["session_id"],
        "turn_id": turn_id,
        "rollout_sha256": _digest_file(rollout),
        "events": projected,
    }
    try:
        terminal_path = create_once_json(run_root, spec["terminal_ref"], terminal)
    except ArtifactError as exc:
        raise ResumeAdapterFailure(f"resume terminal failed: {exc}") from exc
    if json.loads(terminal_path.read_bytes()) != terminal:
        raise ResumeAdapterFailure("resume terminal authority drifted")


def _seal_turn_claim(
    run_root: Path,
    spec: dict[str, Any],
    spec_sha256: str,
    turn_id: str,
) -> None:
    if not _SESSION.fullmatch(turn_id):
        raise ResumeAdapterFailure("resume turn claim id is invalid")
    claim = {
        "schema_version": "agent-workflow.app-resume-turn-claim.vnext.v1",
        "spec_sha256": spec_sha256,
        "session_id": spec["session_id"],
        "turn_id": turn_id,
        "prompt_sha256": _digest(spec["prompt"].encode("utf-8")),
        "resume_nonce": spec["resume_nonce"],
        "audit_marker": spec["audit_marker"],
    }
    path = run_root / spec["turn_claim_ref"]
    if not path.exists():
        try:
            path = create_once_json(run_root, spec["turn_claim_ref"], claim)
        except ArtifactError:
            path = run_root / spec["turn_claim_ref"]
    if path.is_symlink() or not path.is_file() or json.loads(path.read_bytes()) != claim:
        raise ResumeAdapterFailure("resume turn claim authority drifted")


def _emit(projected: list[dict[str, Any]]) -> None:
    for event in projected:
        sys.stdout.buffer.write(_canonical(event))


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def run(spec_path: Path, spec_sha256: str, codex_binary: Path) -> int:
    spec, spec_payload = _load_spec(spec_path, spec_sha256)
    run_root = Path(spec["run_root"]).resolve(strict=True)
    codex_home = Path(spec["codex_home"]).resolve(strict=True)
    cwd = Path(spec["cwd"]).resolve(strict=True)
    rollout = _safe_absolute_file(spec["session_rollout_path"], "session rollout")
    if not rollout.is_relative_to(codex_home):
        raise ResumeAdapterFailure("session rollout escapes its Codex home")
    output_schema_path = _safe_absolute_file(spec["output_schema_path"], "output schema")
    output_schema = _load_json_file(output_schema_path, spec["output_schema_sha256"], "output schema")
    if codex_binary.is_symlink() or not codex_binary.is_file() or _digest_file(codex_binary) != spec["codex_binary_sha256"]:
        raise ResumeAdapterFailure("Codex binary authority drifted")
    authority_refs = {
        f"phases/{spec['phase_id']}/plan.json": spec["plan_sha256"],
        spec["generation_claim_ref"]: spec["generation_claim_sha256"],
        spec["failed_result_ref"]: spec["failed_result_sha256"],
        spec["causal_receipt_ref"]: spec["causal_receipt_sha256"],
    }
    for ref, sha256 in authority_refs.items():
        candidate = run_root / ref
        if candidate.is_symlink() or not candidate.is_file():
            raise ResumeAdapterFailure("resume causal authority drifted")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(run_root) or _digest_file(path) != sha256:
            raise ResumeAdapterFailure("resume causal authority drifted")
    claim = {
        "schema_version": "agent-workflow.app-resume-claim.vnext.v1",
        "spec_sha256": spec_sha256,
        "session_id": spec["session_id"],
        "prior_rollout_sha256": spec["prior_rollout_sha256"],
        "prior_rollout_size": spec["prior_rollout_size"],
    }
    claim_path = run_root / spec["claim_ref"]
    if not claim_path.exists():
        try:
            claim_path = create_once_json(run_root, spec["claim_ref"], claim)
        except ArtifactError:
            claim_path = run_root / spec["claim_ref"]
    if claim_path.is_symlink() or not claim_path.is_file():
        raise ResumeAdapterFailure("resume claim is missing or unsafe")
    if json.loads(claim_path.read_bytes()) != claim:
        raise ResumeAdapterFailure("resume claim was already consumed by different authority")
    if _digest_file(rollout) != spec["prior_rollout_sha256"] or rollout.stat().st_size != spec["prior_rollout_size"]:
        terminal_path = run_root / spec["terminal_ref"]
        if terminal_path.is_file() and not terminal_path.is_symlink():
            terminal = json.loads(terminal_path.read_bytes())
            if (
                not isinstance(terminal, dict)
                or set(terminal) != {
                    "schema_version", "spec_sha256", "session_id", "turn_id",
                    "rollout_sha256", "events",
                }
                or terminal.get("schema_version") != "agent-workflow.app-resume-terminal.vnext.v1"
                or terminal.get("spec_sha256") != spec_sha256
                or terminal.get("session_id") != spec["session_id"]
                or terminal.get("rollout_sha256") != _digest_file(rollout)
                or not isinstance(terminal.get("events"), list)
            ):
                raise ResumeAdapterFailure("resume terminal binding drifted")
            turn_id, projected, context = _project_turn(
                rollout, spec["prior_rollout_size"], spec["session_id"], spec["prompt"]
            )
            _validate_effective_context(context, spec)
            if terminal["turn_id"] != turn_id or terminal["events"] != projected:
                raise ResumeAdapterFailure("resume terminal binding drifted")
            _seal_turn_claim(
                run_root, spec, spec_sha256, turn_id
            )
            _emit(projected)
            return 0
        turn_id, projected, context = _project_turn(
            rollout, spec["prior_rollout_size"], spec["session_id"], spec["prompt"]
        )
        _validate_effective_context(context, spec)
        _seal_turn_claim(run_root, spec, spec_sha256, turn_id)
        _seal_terminal(run_root, spec, spec_sha256, rollout, turn_id, projected)
        _emit(projected)
        return 0
    env = dict(os.environ)
    env["CODEX_HOME"] = os.fspath(codex_home)
    env["AGENT_WORKFLOW_AUDIT_MARKER"] = spec["audit_marker"]
    process = subprocess.Popen(
        [os.fspath(codex_binary), "app-server", "--stdio", "--disable", "plugins"],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    rpc = _RPC(process)
    try:
        rpc.send({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "agent-workflow-resume", "version": "1"}, "capabilities": {"experimentalApi": True, "requestAttestation": False}}})
        response = rpc.receive(request_id=1)
        if "error" in response:
            raise ResumeAdapterFailure("App Server initialize failed")
        rpc.send({"method": "initialized"})
        common = {
            "threadId": spec["session_id"],
            "cwd": os.fspath(cwd),
            "runtimeWorkspaceRoots": spec["runtime_workspace_roots"],
            "approvalPolicy": "never",
            "permissions": spec["permissions_profile"],
            "model": spec["model"],
        }
        rpc.send({"method": "thread/resume", "id": 2, "params": common})
        response = rpc.receive(request_id=2)
        if "error" in response or response.get("result", {}).get("thread", {}).get("id") != spec["session_id"]:
            raise ResumeAdapterFailure("App Server resumed the wrong thread")
        rpc.send({"method": "turn/start", "id": 3, "params": {**common, "input": [{"type": "text", "text": spec["prompt"], "text_elements": []}], "effort": spec["reasoning_effort"], "outputSchema": output_schema}})
        response = rpc.receive(request_id=3)
        if "error" in response:
            raise ResumeAdapterFailure("App Server turn start failed")
        started_turn_id = response.get("result", {}).get("turn", {}).get("id")
        if not isinstance(started_turn_id, str):
            raise ResumeAdapterFailure("App Server turn start lacks an exact turn id")
        _seal_turn_claim(run_root, spec, spec_sha256, started_turn_id)
        terminal_rpc = rpc.receive(terminal=True)
        if terminal_rpc.get("method") != "turn/completed" or terminal_rpc.get("params", {}).get("turn", {}).get("status") != "completed":
            raise ResumeAdapterFailure("App Server resumed turn was not completed")
    finally:
        _terminate(process)
    turn_id, projected, context = _project_turn(
        rollout, spec["prior_rollout_size"], spec["session_id"], spec["prompt"]
    )
    if turn_id != started_turn_id:
        raise ResumeAdapterFailure("App Server terminal turn drifted from its turn claim")
    _validate_effective_context(context, spec)
    _seal_terminal(run_root, spec, _digest(spec_payload), rollout, turn_id, projected)
    _emit(projected)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--spec-sha256", required=True)
    parser.add_argument("--codex", required=True, type=Path)
    parser.add_argument("-c", dest="config", action="append", default=[])
    args = parser.parse_args(argv)
    if not _DIGEST.fullmatch(args.spec_sha256):
        parser.error("--spec-sha256 must be sha256:<hex>")
    try:
        return run(args.spec.resolve(), args.spec_sha256, args.codex.resolve())
    except ResumeAdapterFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
