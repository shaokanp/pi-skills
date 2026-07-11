#!/usr/bin/env python3
"""Standard-library regressions for exact runtime token accounting."""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import token_accounting
import verify_workflow


NEW_WORKFLOW = Path(__file__).with_name("new_workflow.py")


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """Keep Clean Orchestrator regressions inside the existing release gate."""

    for module_name in ("test_clean_orchestrator", "test_runtime_harness"):
        tests.addTests(loader.loadTestsFromModule(importlib.import_module(module_name)))
    return tests


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def append_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def codex_token_event(
    timestamp: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            },
        },
    }


def codex_meta(
    session_id: str, timestamp: str, *, parent_id: str | None = None
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": parent_id or session_id,
            "parent_thread_id": parent_id,
            "timestamp": timestamp,
            "thread_source": "subagent" if parent_id else "cli",
        },
    }


def claude_meta(
    session_id: str, timestamp: str, *, agent_id: str | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user",
        "sessionId": session_id,
        "message": {"role": "user", "content": "fixture"},
    }
    if agent_id is not None:
        value["agentId"] = agent_id
    return value


def claude_message(
    message_id: str,
    timestamp: str,
    *,
    stop_reason: str | None,
    input_tokens: int,
    cache_creation: int,
    cache_read: int,
    output_tokens: int,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "assistant",
        "message": {
            "id": message_id,
            "role": "assistant",
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": "fixture"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        },
    }


class TokenAccountingTests(unittest.TestCase):
    def workflow(self, root: Path) -> Path:
        workflow = root / "workflow"
        workflow.mkdir()
        write_json(workflow / "token-usage.json", token_accounting.new_token_usage())
        return workflow

    def test_codex_early_info_null_uses_later_complete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-session.jsonl"
            append_json(path, codex_meta("session", "2026-07-10T00:00:00Z"))
            append_json(
                path,
                {
                    "timestamp": "2026-07-10T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": None},
                },
            )
            append_json(
                path,
                codex_token_event(
                    "2026-07-10T00:00:02Z",
                    input_tokens=20,
                    cached_input_tokens=4,
                    output_tokens=5,
                    reasoning_tokens=1,
                ),
            )
            snapshot = token_accounting.parse_codex_session(path, "session")
            self.assertEqual(25, snapshot["usage"]["total_tokens"])
            self.assertTrue(snapshot["event_ref"].endswith(":token_count:3"))

    def test_reused_codex_agent_counts_only_post_registration_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            sessions = runtime / "sessions"
            sessions.mkdir(parents=True)
            workflow = self.workflow(root)
            lead_path = sessions / "lead.jsonl"
            agent_path = sessions / "agent.jsonl"
            append_json(lead_path, codex_meta("lead", "2099-07-10T00:00:00Z"))
            append_json(lead_path, codex_token_event("2099-07-10T00:00:01Z", input_tokens=100, cached_input_tokens=50, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, codex_meta("agent", "2020-07-10T00:00:02Z", parent_id="lead"))
            append_json(agent_path, codex_token_event("2025-07-10T00:00:03Z", input_tokens=50, cached_input_tokens=20, output_tokens=5, reasoning_tokens=1))
            append_json(agent_path, {"timestamp":"2025-07-10T00:00:04Z","type":"event_msg","payload":{"type":"task_complete"}})
            token_accounting.start_accounting(workflow, runtime="codex", lead_session_id="lead", runtime_root=runtime)
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:verify-01:attempt-2",
                agent_id="agent",
                round_id="round-001",
                lane_id="verify-01",
                runtime_root=runtime,
                reuse_existing_session=True,
            )
            append_json(agent_path, codex_token_event("2099-07-10T00:00:05Z", input_tokens=80, cached_input_tokens=30, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, {"timestamp":"2099-07-10T00:00:06Z","type":"event_msg","payload":{"type":"task_complete"}})
            append_json(lead_path, codex_token_event("2099-07-10T00:00:07Z", input_tokens=200, cached_input_tokens=100, output_tokens=20, reasoning_tokens=4))
            value = token_accounting.finalize_accounting(workflow, runtime_root=runtime)
            self.assertEqual(145, value["total_tokens"])
            self.assertEqual(35, value["agent_breakdown"][0]["tokens"])

    def test_old_unrelated_reused_codex_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            sessions = runtime / "sessions"
            sessions.mkdir(parents=True)
            workflow = self.workflow(root)
            lead_path = sessions / "lead.jsonl"
            agent_path = sessions / "outsider.jsonl"
            append_json(lead_path, codex_meta("lead", "2099-07-10T00:00:00Z"))
            append_json(lead_path, codex_token_event("2099-07-10T00:00:01Z", input_tokens=100, cached_input_tokens=50, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, codex_meta("outsider", "2020-07-10T00:00:02Z", parent_id="other-lead"))
            append_json(agent_path, codex_token_event("2025-07-10T00:00:03Z", input_tokens=50, cached_input_tokens=20, output_tokens=5, reasoning_tokens=1))
            append_json(agent_path, {"timestamp":"2025-07-10T00:00:04Z","type":"event_msg","payload":{"type":"task_complete"}})
            token_accounting.start_accounting(workflow, runtime="codex", lead_session_id="lead", runtime_root=runtime)
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:verify-01:attempt-2",
                agent_id="outsider",
                round_id="round-001",
                lane_id="verify-01",
                runtime_root=runtime,
                reuse_existing_session=True,
            )
            append_json(agent_path, codex_token_event("2099-07-10T00:00:05Z", input_tokens=80, cached_input_tokens=30, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, {"timestamp":"2099-07-10T00:00:06Z","type":"event_msg","payload":{"type":"task_complete"}})
            append_json(lead_path, codex_token_event("2099-07-10T00:00:07Z", input_tokens=200, cached_input_tokens=100, output_tokens=20, reasoning_tokens=4))
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "not a raw-attested descendant",
            ):
                token_accounting.finalize_accounting(workflow, runtime_root=runtime)

    def test_unregistered_new_child_below_old_reused_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            sessions = runtime / "sessions"
            sessions.mkdir(parents=True)
            workflow = self.workflow(root)
            lead_path = sessions / "lead.jsonl"
            reused_path = sessions / "reused.jsonl"
            child_path = sessions / "child.jsonl"
            append_json(lead_path, codex_meta("lead", "2099-07-10T00:00:00Z"))
            append_json(lead_path, codex_token_event("2099-07-10T00:00:01Z", input_tokens=100, cached_input_tokens=50, output_tokens=10, reasoning_tokens=2))
            append_json(reused_path, codex_meta("reused", "2020-07-10T00:00:02Z", parent_id="lead"))
            append_json(reused_path, codex_token_event("2025-07-10T00:00:03Z", input_tokens=50, cached_input_tokens=20, output_tokens=5, reasoning_tokens=1))
            append_json(reused_path, {"timestamp":"2025-07-10T00:00:04Z","type":"event_msg","payload":{"type":"task_complete"}})
            token_accounting.start_accounting(workflow, runtime="codex", lead_session_id="lead", runtime_root=runtime)
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:verify-01:attempt-2",
                agent_id="reused",
                round_id="round-001",
                lane_id="verify-01",
                runtime_root=runtime,
                reuse_existing_session=True,
            )
            append_json(reused_path, codex_token_event("2099-07-10T00:00:05Z", input_tokens=80, cached_input_tokens=30, output_tokens=10, reasoning_tokens=2))
            append_json(reused_path, {"timestamp":"2099-07-10T00:00:06Z","type":"event_msg","payload":{"type":"task_complete"}})
            append_json(child_path, codex_meta("child", "2099-07-10T00:00:05Z", parent_id="reused"))
            append_json(child_path, codex_token_event("2099-07-10T00:00:06Z", input_tokens=20, cached_input_tokens=5, output_tokens=3, reasoning_tokens=1))
            append_json(child_path, {"timestamp":"2099-07-10T00:00:07Z","type":"event_msg","payload":{"type":"task_complete"}})
            append_json(lead_path, codex_token_event("2099-07-10T00:00:08Z", input_tokens=200, cached_input_tokens=100, output_tokens=20, reasoning_tokens=4))
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "unregistered runtime agents: child",
            ):
                token_accounting.finalize_accounting(workflow, runtime_root=runtime)

    def test_reused_snapshot_captured_at_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            sessions = runtime / "sessions"
            sessions.mkdir(parents=True)
            workflow = self.workflow(root)
            lead_path = sessions / "lead.jsonl"
            agent_path = sessions / "agent.jsonl"
            append_json(lead_path, codex_meta("lead", "2099-07-10T00:00:00Z"))
            append_json(lead_path, codex_token_event("2099-07-10T00:00:01Z", input_tokens=100, cached_input_tokens=50, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, codex_meta("agent", "2020-07-10T00:00:02Z", parent_id="lead"))
            append_json(agent_path, codex_token_event("2025-07-10T00:00:03Z", input_tokens=50, cached_input_tokens=20, output_tokens=5, reasoning_tokens=1))
            append_json(agent_path, {"timestamp":"2025-07-10T00:00:04Z","type":"event_msg","payload":{"type":"task_complete"}})
            token_accounting.start_accounting(workflow, runtime="codex", lead_session_id="lead", runtime_root=runtime)
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:verify-01:attempt-2",
                agent_id="agent",
                round_id="round-001",
                lane_id="verify-01",
                runtime_root=runtime,
                reuse_existing_session=True,
            )
            usage = token_accounting.load_object(workflow / "token-usage.json")
            usage["accounting"]["participants"][0]["start_snapshot"]["captured_at"] = "2024-07-10T00:00:03Z"
            write_json(workflow / "token-usage.json", usage)
            append_json(agent_path, codex_token_event("2099-07-10T00:00:05Z", input_tokens=80, cached_input_tokens=30, output_tokens=10, reasoning_tokens=2))
            append_json(agent_path, {"timestamp":"2099-07-10T00:00:06Z","type":"event_msg","payload":{"type":"task_complete"}})
            append_json(lead_path, codex_token_event("2099-07-10T00:00:07Z", input_tokens=200, cached_input_tokens=100, output_tokens=20, reasoning_tokens=4))
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "captured_at does not match",
            ):
                token_accounting.finalize_accounting(workflow, runtime_root=runtime)

    def test_reused_registration_repair_uses_recorded_time_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            sessions = runtime / "sessions"
            sessions.mkdir(parents=True)
            workflow = self.workflow(root)
            lead_path = sessions / "lead.jsonl"
            agent_path = sessions / "agent.jsonl"
            append_json(lead_path, codex_meta("lead", "2099-07-10T00:00:00Z"))
            append_json(lead_path, codex_token_event("2099-07-10T00:00:01Z", input_tokens=10, cached_input_tokens=1, output_tokens=1, reasoning_tokens=0))
            append_json(agent_path, codex_meta("agent", "2099-07-09T00:00:00Z", parent_id="lead"))
            append_json(agent_path, codex_token_event("2099-07-10T00:00:02Z", input_tokens=20, cached_input_tokens=2, output_tokens=2, reasoning_tokens=0))
            token_accounting.start_accounting(workflow, runtime="codex", lead_session_id="lead", runtime_root=runtime)
            usage = json.loads((workflow / "token-usage.json").read_text())
            usage["accounting"]["participants"] = [{
                "execution_ref": "round-001:verify-01:attempt-002",
                "agent_id": "agent",
                "round_id": "round-001",
                "lane_id": "verify-01",
                "registered_at": "2099-07-10T00:00:03Z",
            }]
            (workflow / "token-usage.json").write_text(json.dumps(usage) + "\n")
            append_json(agent_path, codex_token_event("2099-07-10T00:00:04Z", input_tokens=50, cached_input_tokens=5, output_tokens=5, reasoning_tokens=1))
            repaired = token_accounting.repair_reused_agent_registration(
                workflow,
                execution_ref="round-001:verify-01:attempt-002",
                runtime_root=runtime,
            )
            participant = repaired["accounting"]["participants"][0]
            self.assertEqual(22, participant["start_snapshot"]["usage"]["total_tokens"])
            self.assertEqual("reuse_existing_session", participant["registration_mode"])

    def test_codex_info_null_only_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-session.jsonl"
            append_json(path, codex_meta("session", "2026-07-10T00:00:00Z"))
            append_json(
                path,
                {
                    "timestamp": "2026-07-10T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": None},
                },
            )
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "No complete Codex token_count event",
            ):
                token_accounting.parse_codex_session(path, "session")

    def test_codex_cumulative_usage_must_be_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-session.jsonl"
            append_json(path, codex_meta("session", "2026-07-10T00:00:00Z"))
            append_json(
                path,
                codex_token_event(
                    "2026-07-10T00:00:01Z",
                    input_tokens=20,
                    cached_input_tokens=4,
                    output_tokens=5,
                    reasoning_tokens=1,
                ),
            )
            append_json(
                path,
                codex_token_event(
                    "2026-07-10T00:00:02Z",
                    input_tokens=19,
                    cached_input_tokens=4,
                    output_tokens=5,
                    reasoning_tokens=1,
                ),
            )
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "cumulative usage field input_tokens decreased",
            ):
                token_accounting.parse_codex_session(path, "session")

    def test_portable_successor_generation_registration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "requires host-issued lineage evidence",
            ):
                token_accounting.register_lead_generation(
                    Path(temp) / "workflow",
                    lead_session_id="unrelated-session",
                )

    def test_finalizer_rejects_unattested_successor_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            workflow = self.workflow(root)
            lead_path = runtime / "sessions" / "rollout-lead.jsonl"
            append_json(lead_path, codex_meta("lead", "2026-07-10T00:00:00Z"))
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:00:01Z",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=2,
                    reasoning_tokens=0,
                ),
            )
            token_accounting.start_accounting(
                workflow,
                runtime="codex",
                lead_session_id="lead",
                runtime_root=runtime,
            )
            value = token_accounting.load_object(workflow / "token-usage.json")
            value["accounting"]["lead_generations"].append(
                {
                    "generation": 2,
                    "session_id": "unrelated-session",
                    "started_at": "2026-07-10T00:00:02Z",
                }
            )
            write_json(workflow / "token-usage.json", value)
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError,
                "Multiple Lead generations require host-issued lineage evidence",
            ):
                token_accounting.finalize_accounting(
                    workflow,
                    runtime_root=runtime,
                )

    def test_codex_actor_deltas_include_lead_and_terminal_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            workflow = self.workflow(root)
            lead_id = "lead-session"
            agent_id = "agent-session"
            lead_path = runtime / "sessions" / f"rollout-{lead_id}.jsonl"
            agent_path = runtime / "sessions" / f"rollout-{agent_id}.jsonl"
            append_json(lead_path, codex_meta(lead_id, "2026-07-10T00:00:00Z"))
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:00:00Z",
                    input_tokens=80,
                    cached_input_tokens=40,
                    output_tokens=20,
                    reasoning_tokens=5,
                ),
            )
            token_accounting.start_accounting(
                workflow,
                runtime="codex",
                lead_session_id=lead_id,
                runtime_root=runtime,
            )
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:10:00Z",
                    input_tokens=380,
                    cached_input_tokens=180,
                    output_tokens=120,
                    reasoning_tokens=25,
                ),
            )
            append_json(
                agent_path,
                codex_meta(agent_id, "2099-07-10T00:07:00Z", parent_id=lead_id),
            )
            append_json(
                agent_path,
                codex_token_event(
                    "2099-07-10T00:08:00Z",
                    input_tokens=200,
                    cached_input_tokens=100,
                    output_tokens=50,
                    reasoning_tokens=10,
                ),
            )
            append_json(
                agent_path,
                {"timestamp": "2099-07-10T00:08:01Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            )
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:review-01:attempt-1",
                agent_id=agent_id,
                round_id="round-001",
                lane_id="review-01",
            )
            value = token_accounting.finalize_accounting(workflow, runtime_root=runtime)
            self.assertEqual(650, value["total_tokens"])
            self.assertEqual(400, value["measurements"][0]["delta_tokens"])
            self.assertEqual(250, value["agent_breakdown"][0]["tokens"])
            self.assertEqual([], token_accounting.validate_v2(value, workflow, final=True))

            original_agent_log = agent_path.read_text(encoding="utf-8")
            append_json(
                agent_path,
                {"timestamp": "2099-07-10T00:09:00Z", "type": "turn_context", "payload": {}},
            )
            reopened_failures = token_accounting.validate_v2(value, workflow, final=True)
            self.assertTrue(
                any("no longer terminal" in failure for failure in reopened_failures)
            )
            agent_path.write_text(original_agent_log, encoding="utf-8")

            tampered = copy.deepcopy(value)
            tampered["total_tokens"] += 1
            failures = token_accounting.validate_v2(tampered, workflow, final=True)
            self.assertTrue(any("total_tokens must equal" in failure for failure in failures))

            identity_tampered = copy.deepcopy(value)
            identity_tampered["measurements"][1]["subject_id"] = "outsider"
            identity_failures = token_accounting.validate_v2(
                identity_tampered, workflow, final=True
            )
            self.assertTrue(
                any("session_id must match subject_id" in failure for failure in identity_failures)
            )

            lines = agent_path.read_text(encoding="utf-8").splitlines()
            changed = json.loads(lines[1])
            counters = changed["payload"]["info"]["total_token_usage"]
            counters["output_tokens"] = 51
            counters["total_tokens"] = 251
            lines[1] = json.dumps(changed, ensure_ascii=False)
            agent_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            source_failures = token_accounting.validate_v2(value, workflow, final=True)
            self.assertTrue(
                any("does not match the Codex source event" in failure for failure in source_failures)
            )

    def test_new_workflow_defers_codex_accounting_until_compound_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            lead_path = runtime / "sessions" / "rollout-auto-lead.jsonl"
            append_json(
                lead_path,
                codex_meta("auto-lead", "2026-07-10T00:00:00Z"),
            )
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:00:00Z",
                    input_tokens=10,
                    cached_input_tokens=4,
                    output_tokens=2,
                    reasoning_tokens=1,
                ),
            )
            env = os.environ.copy()
            env.update({"CODEX_HOME": str(runtime), "CODEX_THREAD_ID": "auto-lead"})
            result = subprocess.run(
                [
                    sys.executable,
                    str(NEW_WORKFLOW),
                    "Auto exact",
                    "--root",
                    str(root / "workflows"),
                    "--runner-mode",
                    "manual_simulation",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            self.assertEqual(0, result.returncode, result.stdout)
            value = token_accounting.load_object(
                root / "workflows" / "auto-exact" / "token-usage.json"
            )
            self.assertIsNone(value["accounting"]["runtime"])
            self.assertIsNone(value["accounting"]["lead_session_id"])
            self.assertIsNone(value["accounting"]["started_at"])
            self.assertEqual([], value["accounting"]["participants"])

    def test_codex_nonterminal_agent_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            workflow = self.workflow(root)
            lead_path = runtime / "sessions" / "rollout-lead.jsonl"
            agent_path = runtime / "sessions" / "rollout-agent.jsonl"
            append_json(lead_path, codex_meta("lead", "2026-07-10T00:00:00Z"))
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:00:00Z",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=2,
                    reasoning_tokens=0,
                ),
            )
            token_accounting.start_accounting(
                workflow,
                runtime="codex",
                lead_session_id="lead",
                runtime_root=runtime,
            )
            append_json(
                agent_path,
                codex_meta("agent", "2099-07-10T00:00:00Z", parent_id="lead"),
            )
            append_json(
                agent_path,
                codex_token_event(
                    "2026-07-10T00:01:00Z",
                    input_tokens=20,
                    cached_input_tokens=0,
                    output_tokens=4,
                    reasoning_tokens=0,
                ),
            )
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:verify-01:attempt-1",
                agent_id="agent",
                round_id="round-001",
                lane_id="verify-01",
            )
            with self.assertRaisesRegex(token_accounting.TokenAccountingError, "not terminal"):
                token_accounting.finalize_accounting(workflow, runtime_root=runtime)

    def test_unregistered_codex_child_session_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "codex"
            workflow = self.workflow(root)
            lead_path = runtime / "sessions" / "rollout-lead.jsonl"
            hidden_path = runtime / "sessions" / "rollout-hidden.jsonl"
            append_json(lead_path, codex_meta("lead", "2026-07-10T00:00:00Z"))
            append_json(
                lead_path,
                codex_token_event(
                    "2026-07-10T00:00:01Z",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=2,
                    reasoning_tokens=0,
                ),
            )
            token_accounting.start_accounting(
                workflow,
                runtime="codex",
                lead_session_id="lead",
                runtime_root=runtime,
            )
            append_json(
                hidden_path,
                codex_meta("hidden", "2099-07-10T00:01:00Z", parent_id="lead"),
            )
            append_json(
                hidden_path,
                codex_token_event(
                    "2099-07-10T00:01:01Z",
                    input_tokens=20,
                    cached_input_tokens=0,
                    output_tokens=4,
                    reasoning_tokens=0,
                ),
            )
            append_json(
                hidden_path,
                {"timestamp": "2099-07-10T00:01:02Z", "type": "event_msg", "payload": {"type": "task_complete"}},
            )
            with self.assertRaisesRegex(
                token_accounting.TokenAccountingError, "unregistered runtime agents: hidden"
            ):
                token_accounting.finalize_accounting(workflow, runtime_root=runtime)

    def test_claude_deduplicates_stream_records_and_sums_cache_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "claude"
            workflow = self.workflow(root)
            lead_path = runtime / "projects" / "project" / "lead.jsonl"
            agent_path = (
                runtime
                / "projects"
                / "project"
                / "lead"
                / "subagents"
                / "agent-worker.jsonl"
            )
            append_json(
                lead_path,
                claude_meta("lead", "2026-07-10T00:00:00Z"),
            )
            append_json(
                lead_path,
                claude_message(
                    "lead-before",
                    "2026-07-10T00:00:00Z",
                    stop_reason="end_turn",
                    input_tokens=5,
                    cache_creation=10,
                    cache_read=20,
                    output_tokens=5,
                ),
            )
            token_accounting.start_accounting(
                workflow,
                runtime="claude",
                lead_session_id="lead",
                runtime_root=runtime,
            )
            partial = claude_message(
                "lead-after",
                "2026-07-10T00:05:00Z",
                stop_reason=None,
                input_tokens=3,
                cache_creation=4,
                cache_read=5,
                output_tokens=1,
            )
            final = claude_message(
                "lead-after",
                "2026-07-10T00:05:01Z",
                stop_reason="end_turn",
                input_tokens=3,
                cache_creation=4,
                cache_read=5,
                output_tokens=8,
            )
            append_json(lead_path, partial)
            append_json(lead_path, final)
            append_json(lead_path, final)
            append_json(
                lead_path,
                claude_message(
                    "finalizer-tool-use",
                    "2026-07-10T00:06:00Z",
                    stop_reason="tool_use",
                    input_tokens=2,
                    cache_creation=3,
                    cache_read=4,
                    output_tokens=5,
                ),
            )
            agent_final = claude_message(
                "agent-message",
                "2099-07-10T00:04:00Z",
                stop_reason="end_turn",
                input_tokens=2,
                cache_creation=3,
                cache_read=7,
                output_tokens=6,
            )
            append_json(
                agent_path,
                claude_meta("lead", "2099-07-10T00:03:00Z", agent_id="worker"),
            )
            append_json(agent_path, agent_final)
            append_json(agent_path, agent_final)
            token_accounting.register_agent(
                workflow,
                execution_ref="round-001:challenge-01:attempt-1",
                agent_id="agent-worker",
                round_id="round-001",
                lane_id="challenge-01",
            )
            value = token_accounting.finalize_accounting(workflow, runtime_root=runtime)
            self.assertEqual(38, value["total_tokens"])
            self.assertEqual(20, value["measurements"][0]["delta_tokens"])
            self.assertEqual(18, value["agent_breakdown"][0]["tokens"])
            self.assertIsNone(value["reasoning_tokens"])
            self.assertEqual([], token_accounting.validate_v2(value, workflow, final=True))

    def test_claude_tool_use_and_later_partial_are_not_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent-worker.jsonl"
            append_json(
                path,
                claude_message(
                    "tool-call",
                    "2026-07-10T00:00:00Z",
                    stop_reason="tool_use",
                    input_tokens=1,
                    cache_creation=0,
                    cache_read=0,
                    output_tokens=4,
                ),
            )
            self.assertFalse(
                token_accounting.parse_claude_session(path, "worker")["terminal"]
            )
            append_json(
                path,
                claude_message(
                    "done",
                    "2026-07-10T00:01:00Z",
                    stop_reason="end_turn",
                    input_tokens=1,
                    cache_creation=0,
                    cache_read=0,
                    output_tokens=3,
                ),
            )
            append_json(
                path,
                claude_message(
                    "next-turn",
                    "2026-07-10T00:02:00Z",
                    stop_reason=None,
                    input_tokens=1,
                    cache_creation=0,
                    cache_read=0,
                    output_tokens=1,
                ),
            )
            self.assertFalse(
                token_accounting.parse_claude_session(path, "worker")["terminal"]
            )

    def test_codex_turn_aborted_is_terminal_but_reopened_session_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-agent.jsonl"
            append_json(
                path,
                codex_token_event(
                    "2026-07-10T00:00:00Z",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=2,
                    reasoning_tokens=0,
                ),
            )
            append_json(
                path,
                {"timestamp": "2026-07-10T00:00:01Z", "type": "event_msg", "payload": {"type": "turn_aborted"}},
            )
            self.assertTrue(
                token_accounting.parse_codex_session(path, "agent")["terminal"]
            )
            append_json(
                path,
                {"timestamp": "2026-07-10T00:00:02Z", "type": "turn_context", "payload": {}},
            )
            self.assertFalse(
                token_accounting.parse_codex_session(path, "agent")["terminal"]
            )

    def test_v1_self_declared_exact_is_rejected_in_final_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            write_json(
                workflow / "token-usage.json",
                {
                    "schema_version": "agent-loops.token-usage.v1",
                    "status": "complete",
                    "source": "runtime_reported",
                    "confidence": "exact",
                    "total_tokens": 123,
                    "method": "Lead copied a counter.",
                    "round_breakdown": [],
                    "agent_breakdown": [],
                },
            )
            failures: list[str] = []
            verify_workflow.validate_token_usage(workflow, failures, "final")
            self.assertTrue(any("v1 cannot prove exact" in failure for failure in failures))

    def test_new_workspace_cannot_downgrade_to_v1(self) -> None:
        state = {
            "schema_version": "agent-workflow.workflow.v2"
        }
        legacy = {
            "schema_version": "agent-loops.token-usage.v1",
            "confidence": "estimated",
        }
        failures: list[str] = []
        verify_workflow.validate_token_schema_requirement(
            state, legacy, "final", failures
        )
        self.assertTrue(any("cannot downgrade" in failure for failure in failures))

    def test_runner_attempt_missing_from_token_registry_is_rejected(self) -> None:
        value = token_accounting.new_token_usage()
        value["accounting"]["participants"] = [
            {
                "execution_ref": "round-001:review-01:attempt-1",
                "agent_id": "review-agent",
                "round_id": "round-001",
                "lane_id": "review-01",
            }
        ]
        lifecycle = {
            "round-001:review-01": {
                "round_id": "round-001",
                "lane_id": "review-01",
                "agent_id": "review-agent",
            },
            "round-001:verify-01": {
                "round_id": "round-001",
                "lane_id": "verify-01",
                "agent_id": "verify-agent",
            },
        }
        failures: list[str] = []
        verify_workflow.validate_token_participant_coverage(
            value, lifecycle, "final", failures
        )
        self.assertTrue(any("verify-agent" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
