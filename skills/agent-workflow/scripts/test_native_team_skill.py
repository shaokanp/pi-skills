#!/usr/bin/env python3
"""Focused contract tests for the Agent Workflow 1.0 native thin skill."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_ROOT.parents[1]
SKILL = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
SKILL_COMPACT = " ".join(SKILL.split())
README_ZH = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")
README_EN = (SKILL_ROOT / "README.en.md").read_text(encoding="utf-8")
ROOT_README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
OPENAI_INTERFACE = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")


class NativeTeamSkillTests(unittest.TestCase):
    def test_entrypoint_is_explicit_native_and_current_agent_orchestrates(self) -> None:
        self.assertIn("user explicitly asks", SKILL)
        self.assertIn("The agent that loads this skill is the Orchestrator", SKILL)
        self.assertIn("Explicit invocation means use a real team", SKILL)
        self.assertIn("unsupported in the current host", SKILL_COMPACT)
        self.assertIn("Do not simulate a team", SKILL)

    def test_native_collaboration_surface_is_complete(self) -> None:
        for tool in (
            "spawn_agent",
            "send_message",
            "followup_task",
            "wait_agent",
            "interrupt_agent",
        ):
            with self.subTest(tool=tool):
                self.assertIn(f"`{tool}`", SKILL)
        self.assertIn('fork_turns="none"', SKILL)

    def test_agent_lifecycle_cli_is_explicitly_forbidden_and_not_a_dependency(self) -> None:
        self.assertIn("Never use external model CLIs", SKILL)
        self.assertIn("generated process supervisors", SKILL)
        self.assertIn("Ordinary task commands", SKILL)

    def test_team_design_rewards_distinct_value_not_agent_count(self) -> None:
        for phrase in (
            "distinct question",
            "owned outcome",
            "independent error-detection value",
            "Duplicate agents create consensus noise",
            "smallest high-value team",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, SKILL_COMPACT)

    def test_ready_work_is_parallel_but_overlapping_writes_are_not(self) -> None:
        self.assertIn("Launch all ready, independent tasks", SKILL)
        self.assertIn("Parallel source changes require clearly disjoint ownership", SKILL)
        self.assertIn("use one writer and parallel read-only support", SKILL_COMPACT)
        self.assertIn("Never let multiple agents edit the same file or semantic seam", SKILL)

    def test_packets_are_fresh_bounded_and_self_contained(self) -> None:
        self.assertIn("self-contained packet", SKILL)
        self.assertIn("fork_turns=\"none\"", SKILL)
        for field in (
            "owned outcome",
            "read/write ownership",
            "proof or acceptance checks",
            "compact terminal deliverable",
            "stop condition",
        ):
            with self.subTest(field=field):
                self.assertIn(field, SKILL)
        self.assertIn("must not delegate", SKILL)

    def test_challenge_requires_evidence_and_repair_is_bounded(self) -> None:
        for field in ("CLAIM:", "EVIDENCE:", "RISK:", "REQUEST:"):
            self.assertIn(field, SKILL)
        self.assertIn("Agreement without evidence does not close a finding", SKILL)
        self.assertIn("One owner response is the default", SKILL)
        self.assertIn("materially new evidence", SKILL)

    def test_source_changes_require_fresh_read_only_verification(self) -> None:
        self.assertIn("spawn a fresh verifier", SKILL)
        self.assertIn("the verifier is read-only", SKILL)
        self.assertIn("not the builder transcript", SKILL_COMPACT)
        self.assertIn("fingerprint of the relevant artifact and source diff", SKILL_COMPACT)
        self.assertIn("Any verifier mutation invalidates the verdict", SKILL_COMPACT)
        self.assertIn("fresh re-verification", SKILL)
        self.assertIn("same issue fails again", SKILL)
        self.assertIn("stop and report the blocker", SKILL)

    def test_research_uses_a_fresh_quality_lens_without_mandatory_verifier_ceremony(self) -> None:
        self.assertIn("one independent fresh quality lens", SKILL_COMPACT)
        self.assertIn("for research or decisions it may be an Explorer, Challenger, or Judge", SKILL_COMPACT)
        self.assertIn("For research or decisions, use an independent challenger or judge when", SKILL)
        self.assertNotIn("owner plus one independent fresh verifier", SKILL_COMPACT)
        for guide in (README_ZH, README_EN):
            with self.subTest(document=guide.splitlines()[0]):
                self.assertIn("Source change?", guide)
                self.assertRegex(guide, r"Risk.*conflict.*uncertainty")
                self.assertRegex(guide, r"[Cc]hallenger / [Jj]udge")

    def test_polling_and_progress_ceremony_are_rejected(self) -> None:
        self.assertIn("Do not poll agent status", SKILL_COMPACT)
        self.assertIn("narrate \"still running\"", SKILL)
        self.assertIn("or use shell waits", SKILL)
        self.assertIn("Use native terminal notifications", SKILL)

    def test_external_actions_remain_human_boundaries(self) -> None:
        for action in ("Commit", "push", "publish", "deploy", "release", "production mutation"):
            with self.subTest(action=action):
                self.assertIn(action, SKILL)
        self.assertIn("separate approval boundaries", SKILL)

    def test_canonical_instruction_is_thin(self) -> None:
        lines = SKILL.splitlines()
        self.assertLessEqual(len(lines), 250)
        self.assertLessEqual(len(SKILL.split()), 1800)
        self.assertEqual(SKILL.count("# Agent Workflow"), 1)

    def test_guides_describe_public_1_0(self) -> None:
        for text in (README_ZH, README_EN):
            self.assertIn("1.0", text)
            self.assertIn("native", text.lower())
        self.assertIn("目前的 agent", README_ZH)
        self.assertIn("current agent becomes the Orchestrator", README_EN)
        self.assertIn("ships no separate agent runtime", " ".join(ROOT_README.split()))

    def test_host_interface_and_public_readme_match_public_1_0(self) -> None:
        for phrase in (
            "current agent as Orchestrator",
            "smallest high-value native team",
            "fresh read-only verifier for source changes",
            "Never use an external CLI",
        ):
            self.assertIn(phrase, OPENAI_INTERFACE)
        self.assertEqual(OPENAI_INTERFACE.count("default_prompt:"), 1)
        self.assertIn("| `agent-workflow` | Stable |", ROOT_README)
        self.assertIn("Agent Workflow 1.0 is the public", ROOT_README)

    def test_guides_require_dry_run_before_install_execution(self) -> None:
        for guide in (README_ZH, README_EN):
            with self.subTest(document=guide.splitlines()[0]):
                dry_run = guide.index("bash scripts/install-skill.sh agent-workflow")
                execute = guide.index("--execute")
                self.assertLess(dry_run, execute)

    def test_package_contains_only_native_contract_surfaces(self) -> None:
        self.assertEqual(
            {path.name for path in SKILL_ROOT.iterdir()},
            {"README.en.md", "README.md", "SKILL.md", "agents", "evals", "scripts"},
        )
        self.assertEqual(
            {path.name for path in (SKILL_ROOT / "scripts").iterdir() if path.is_file()},
            {"test_native_team_skill.py"},
        )
        self.assertEqual(
            {path.name for path in (SKILL_ROOT / "agents").iterdir() if path.is_file()},
            {"openai.yaml"},
        )
        self.assertEqual(
            {path.name for path in (SKILL_ROOT / "evals").iterdir() if path.is_file()},
            {"evals.json"},
        )

    def test_eval_corpus_covers_parallel_overlap_challenge_and_unsupported(self) -> None:
        data = json.loads((SKILL_ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["skill_name"], "agent-workflow")
        self.assertGreaterEqual(len(data["evals"]), 4)
        names = {item["name"] for item in data["evals"]}
        self.assertEqual(len(names), len(data["evals"]))
        joined = "\n".join(json.dumps(item, ensure_ascii=False) for item in data["evals"])
        for concept in ("parallel", "disjoint", "overlap", "fresh verifier", "unsupported"):
            with self.subTest(concept=concept):
                self.assertIn(concept, joined.lower())
        for item in data["evals"]:
            self.assertTrue(item["prompt"].strip())
            self.assertTrue(item["expected_output"].strip())
            self.assertGreaterEqual(len(item["assertions"]), 3)

    def test_registry_and_release_metadata_stay_aligned(self) -> None:
        registry = json.loads((REPO_ROOT / "registry.json").read_text(encoding="utf-8"))
        item = next(skill for skill in registry["skills"] if skill["id"] == "agent-workflow")
        self.assertEqual(item["version"], "1.0.0")
        self.assertIn("native agent teams", item["description"])
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        markers = re.findall(
            r"pi-skills:(?:unreleased|release) id=agent-workflow version=1\.0\.0",
            changelog,
        )
        self.assertEqual(len(markers), 1)
        self.assertIn("Introduced Agent Workflow as a native thin-team design", changelog)
        self.assertIn("public 1.0 package is intentionally small", changelog)

    def test_skill_frontmatter_has_trigger_focused_description(self) -> None:
        match = re.match(r"^---\n(.*?)\n---\n", SKILL, re.S)
        self.assertIsNotNone(match)
        frontmatter = match.group(1)
        self.assertIn("explicitly asks for Agent Workflow", frontmatter)
        self.assertIn("parallel agents", frontmatter)
        self.assertIn("adversarial review", frontmatter)
        self.assertIn("fresh-context verification", frontmatter)


if __name__ == "__main__":
    unittest.main(verbosity=2)
