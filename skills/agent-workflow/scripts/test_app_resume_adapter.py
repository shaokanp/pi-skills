#!/usr/bin/env python3
"""Deterministic fault fixtures for the one-shot App Server resume adapter."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))

from app_resume_adapter import ResumeAdapterFailure, run
from artifact_store import create_once_json


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class AppResumeAdapterTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        *,
        cwd_drift: bool = False,
        prompt_drift: bool = False,
        omit_appended_token: bool = False,
        extra_foreign_user: bool = False,
        agents_only_preamble: bool = False,
        split_host_preamble: bool = False,
        split_explicit_prompt: bool = False,
        extra_preamble_part: bool = False,
    ) -> tuple[Path, str, Path]:
        session_id = "019f566c-4899-7d03-83a5-3e7043b74fcc"
        turn_id = "019f5691-1e66-77f1-93c8-3fb7dab0218c"
        codex_home = root / "codex-home"
        rollout = codex_home / "sessions/recovery.jsonl"
        rollout.parent.mkdir(parents=True)
        initial = (json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n").encode()
        if omit_appended_token:
            initial += (json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 101, "output_tokens": 11}}}}) + "\n").encode()
        rollout.write_bytes(initial)
        prior_sha = digest(initial)
        prior_size = len(initial)
        context = {
            "turn_id": turn_id,
            "cwd": os.fspath(root / ("drifted" if cwd_drift else "workspace")),
            "model": "gpt-5.6-terra",
            "effort": "xhigh",
            "workspace_roots": [os.fspath(root / "workspace")],
        }
        expected_prompt = "foreign turn" if prompt_drift else "recover exact failed lineage\n\n[agent_workflow_resume_nonce=fixture-nonce]"
        if split_explicit_prompt and not prompt_drift:
            prompt_parts = [
                {"type": "input_text", "text": "recover exact failed lineage"},
                {"type": "input_text", "text": "\n[agent_workflow_resume_nonce=fixture-nonce]"},
            ]
        else:
            prompt_parts = [{"type": "input_text", "text": expected_prompt}]
        prompt_message = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": prompt_parts}}
        appended = [
            {"type": "turn_context", "payload": context},
            *([] if not (agents_only_preamble or split_host_preamble) else [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /fixture\n\n<INSTRUCTIONS>\nfixture overlay\n</INSTRUCTIONS>"},
                *([] if not split_host_preamble else [{"type": "input_text", "text": "<environment_context>\n  <cwd>/fixture</cwd>\n</environment_context>"}]),
                *([] if not extra_preamble_part else [{"type": "input_text", "text": "foreign trailing authority"}]),
            ]}}]),
            prompt_message,
            *([] if not extra_foreign_user else [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "foreign extra authority"}]}}]),
            *([] if omit_appended_token else [{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 7, "output_tokens": 3}}}}]),
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": '{"answer":"recovered"}'}},
        ]
        with rollout.open("ab") as target:
            target.write(b"".join((json.dumps(item) + "\n").encode() for item in appended))
        workspace = root / "workspace"
        workspace.mkdir()
        schema = root / "schema.json"
        schema.write_text('{"type":"object"}\n')
        fake = root / "fake-codex"
        fake.write_text("#!/bin/sh\nexit 99\n")
        fake.chmod(0o700)
        for ref, value in {
            "phases/002-recovery/plan.json": {"kind": "plan"},
            "generations/claims/recovery.json": {"kind": "claim"},
            "phases/001-failed/tasks/worker/result.json": {"kind": "failed"},
            "phases/001-failed/receipt.json": {"kind": "receipt"},
        }.items():
            create_once_json(root, ref, value)
        spec = {
            "schema_version": "agent-workflow.app-resume-spec.vnext.v1",
            "workflow_id": "fixture-workflow",
            "authority_revision": 1,
            "generation_id": "generation-002",
            "phase_id": "002-recovery",
            "task_id": "recovery-task",
            "lineage_id": "lineage-worker",
            "plan_sha256": digest((root / "phases/002-recovery/plan.json").read_bytes()),
            "generation_claim_ref": "generations/claims/recovery.json",
            "generation_claim_sha256": digest((root / "generations/claims/recovery.json").read_bytes()),
            "runtime_bundle_sha256": "sha256:" + "1" * 64,
            "failed_result_ref": "phases/001-failed/tasks/worker/result.json",
            "failed_result_sha256": digest((root / "phases/001-failed/tasks/worker/result.json").read_bytes()),
            "causal_receipt_ref": "phases/001-failed/receipt.json",
            "causal_receipt_sha256": digest((root / "phases/001-failed/receipt.json").read_bytes()),
            "session_id": session_id,
            "codex_home": os.fspath(codex_home),
            "session_rollout_path": os.fspath(rollout),
            "prior_rollout_sha256": prior_sha,
            "prior_rollout_size": prior_size,
            "codex_binary_sha256": digest(fake.read_bytes()),
            "model": "gpt-5.6-terra",
            "reasoning_effort": "xhigh",
            "permissions_profile": "vnext_read_only",
            "cwd": os.fspath(workspace),
            "runtime_workspace_roots": [os.fspath(workspace)],
            "prompt": "recover exact failed lineage\n\n[agent_workflow_resume_nonce=fixture-nonce]",
            "task_prompt_sha256": digest(b"recover exact failed lineage"),
            "resume_nonce": "fixture-nonce",
            "output_schema_path": os.fspath(schema),
            "output_schema_sha256": digest(schema.read_bytes()),
            "audit_marker": "agent-workflow:fixture:resume",
            "run_root": os.fspath(root),
            "claim_ref": "runtime/resume/002-recovery/recovery-task/claim.json",
            "turn_claim_ref": "runtime/resume/002-recovery/recovery-task/turn-claim.json",
            "terminal_ref": "runtime/resume/002-recovery/recovery-task/terminal.json",
        }
        spec_path = create_once_json(root, "runtime/resume/002-recovery/recovery-task/spec.json", spec)
        claim = {
            "schema_version": "agent-workflow.app-resume-claim.vnext.v1",
            "spec_sha256": digest(spec_path.read_bytes()),
            "session_id": session_id,
            "prior_rollout_sha256": prior_sha,
            "prior_rollout_size": prior_size,
        }
        create_once_json(root, spec["claim_ref"], claim)
        return spec_path, digest(spec_path.read_bytes()), fake

    def test_completed_raw_turn_is_replayed_after_adapter_crash_without_second_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root)
            output = io.BytesIO()
            with mock.patch("app_resume_adapter.sys.stdout", SimpleNamespace(buffer=output)):
                self.assertEqual(run(spec_path, spec_sha, fake), 0)
            terminal = root / "runtime/resume/002-recovery/recovery-task/terminal.json"
            self.assertTrue(terminal.is_file())
            self.assertIn(b'"type":"turn.completed"', output.getvalue())

    def test_replayed_turn_rejects_effective_cwd_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, cwd_drift=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "effective settings drifted"):
                run(spec_path, spec_sha, fake)
            self.assertFalse((root / "runtime/resume/002-recovery/recovery-task/terminal.json").exists())

    def test_replayed_turn_rejects_foreign_prompt_even_when_context_matches(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, prompt_drift=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "resume prompt authority drifted"):
                run(spec_path, spec_sha, fake)

    def test_replayed_turn_cannot_reuse_prior_turn_token_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, omit_appended_token=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "token evidence"):
                run(spec_path, spec_sha, fake)

    def test_replayed_turn_rejects_correct_nonce_plus_extra_foreign_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, extra_foreign_user=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "resume prompt authority drifted"):
                run(spec_path, spec_sha, fake)

    def test_replayed_turn_rejects_unobserved_agents_only_host_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, agents_only_preamble=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "resume prompt authority drifted"):
                run(spec_path, spec_sha, fake)

    def test_replayed_turn_accepts_current_split_agents_and_environment_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, split_host_preamble=True)
            output = io.BytesIO()
            with mock.patch("app_resume_adapter.sys.stdout", SimpleNamespace(buffer=output)):
                self.assertEqual(run(spec_path, spec_sha, fake), 0)
            self.assertIn(b'"type":"turn.completed"', output.getvalue())

    def test_replayed_turn_rejects_split_explicit_prompt_parts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root, split_explicit_prompt=True)
            with self.assertRaisesRegex(ResumeAdapterFailure, "resume prompt authority drifted"):
                run(spec_path, spec_sha, fake)

    def test_replayed_turn_rejects_extra_split_preamble_part(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(
                root, split_host_preamble=True, extra_preamble_part=True
            )
            with self.assertRaisesRegex(ResumeAdapterFailure, "resume prompt authority drifted"):
                run(spec_path, spec_sha, fake)

    def test_existing_terminal_events_must_equal_fresh_raw_projection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            spec_path, spec_sha, fake = self.fixture(root)
            output = io.BytesIO()
            with mock.patch("app_resume_adapter.sys.stdout", SimpleNamespace(buffer=output)):
                self.assertEqual(run(spec_path, spec_sha, fake), 0)
            terminal_path = root / "runtime/resume/002-recovery/recovery-task/terminal.json"
            terminal = json.loads(terminal_path.read_text())
            terminal["events"][-1]["usage"]["input_tokens"] = 999
            terminal_path.write_text(json.dumps(terminal, sort_keys=True, separators=(",", ":")) + "\n")
            with self.assertRaisesRegex(ResumeAdapterFailure, "terminal binding drifted"):
                run(spec_path, spec_sha, fake)


if __name__ == "__main__":
    unittest.main()
