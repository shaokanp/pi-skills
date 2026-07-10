#!/usr/bin/env python3
"""Standard-library regressions for the Agent Workflow Swarm Card."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import render_swarm_card


SKILL_ROOT = Path(__file__).resolve().parents[1]
NEW_WORKFLOW = SKILL_ROOT / "scripts" / "new_workflow.py"
RENDER_CARD = SKILL_ROOT / "scripts" / "render_swarm_card.py"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def lane(lane_id: str, lane_name: str, model: str, effort: str) -> dict[str, Any]:
    return {
        "id": lane_id,
        "lane": lane_name,
        "runner": {"agent_type": "default"},
        "routing": {
            "packet_id": f"packet-{lane_id}",
            "decision_id": f"decision-{lane_id}",
            "selected": {"model": model, "effort": effort},
            "status": "planned",
        },
    }


class SwarmCardTests(unittest.TestCase):
    def card(self) -> dict[str, Any]:
        card = render_swarm_card.build_initial_card(
            slug="validator-hardening",
            runner_mode="codex_builtin_subagents",
            round_id="round-001",
            round_budget=3,
            lanes=[
                lane("discover-01", "discover", "gpt-5.6-terra", "xhigh"),
                lane("implement-01", "implement", "gpt-5.6-terra", "xhigh"),
                lane("repair-01", "repair", "gpt-5.6-terra", "xhigh"),
                lane("review-01", "review", "gpt-5.6-sol", "xhigh"),
                lane("verify-01", "verify", "gpt-5.6-sol", "xhigh"),
            ],
            goal="修掉 validator false-pass，直到沒有 P2+ open risk。",
        )
        agents = {
            agent["lane_id"]: agent
            for phase in card["phases"]
            for agent in phase["agents"]
        }
        agents["discover-01"]["status"] = "complete"
        agents["implement-01"]["status"] = "running"
        agents["repair-01"]["status"] = "waiting"
        agents["repair-01"]["status_note"] = "review findings"
        card["status"] = "running"
        return card

    def test_renders_cjk_safe_per_agent_phase_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "swarm-card.json"
            card = self.card()
            output = render_swarm_card.render_card(card, card_path=path)

            self.assertIn("> **Agent Workflow · RUNNING**", output)
            self.assertIn(r"修掉 validator false\-pass，直到沒有 P2\+ open risk。", output)
            self.assertIn(
                "> ■ complete · `discover-01` · discover *(Terra · xhigh · inherited)*",
                output,
            )
            self.assertIn(
                "> ◐ running · `implement-01` · implement *(Terra · xhigh · inherited)*",
                output,
            )
            self.assertIn(
                "> △ waiting: review findings · `repair-01` · repair *(Terra · xhigh · inherited)*",
                output,
            )
            self.assertIn(
                "> □ not started · `verify-01` · verify *(Sol · xhigh · inherited)*",
                output,
            )
            self.assertNotIn("◆", output)
            self.assertNotIn("◇", output)
            self.assertNotIn("●", output)
            self.assertTrue(
                all(not line or line.startswith(">") for line in output.splitlines())
            )

    def test_final_card_uses_exact_token_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "swarm-card.json"
            card = self.card()
            card["status"] = "completed"
            token_usage = {
                "status": "complete",
                "confidence": "exact",
                "total_tokens": 1_550_329,
            }
            output = render_swarm_card.render_card(
                card,
                card_path=path,
                token_usage=token_usage,
            )
            self.assertIn("> Tokens: 1,550,329 exact", output)

    def test_emit_suppresses_unchanged_card_and_reemits_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "swarm-card.json"
            write_json(path, self.card())

            first = subprocess.run(
                [sys.executable, str(RENDER_CARD), str(path), "--emit"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, first.returncode, first.stdout)
            self.assertIn("Agent Workflow · RUNNING", first.stdout)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertRegex(persisted["last_emitted_hash"], r"^sha256:[0-9a-f]{64}$")

            second = subprocess.run(
                [sys.executable, str(RENDER_CARD), str(path), "--emit"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, second.returncode, second.stdout)
            self.assertEqual("", second.stdout)

            persisted["gate"]["decision"] = "revise"
            write_json(path, persisted)
            third = subprocess.run(
                [sys.executable, str(RENDER_CARD), str(path), "--emit"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, third.returncode, third.stdout)
            self.assertIn("**Gate** Revise", third.stdout)

    def test_emit_projects_goal_lane_status_and_route_from_workflow_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workflow = Path(temp)
            card = self.card()
            card["status"] = "preview"
            for phase in card["phases"]:
                for agent in phase["agents"]:
                    agent["status"] = "planned"
                    agent["status_note"] = None
            write_json(workflow / "swarm-card.json", card)
            write_json(
                workflow / "state.json",
                {
                    "status": "running",
                    "current_round": "round-001",
                    "round_budget": 3,
                },
            )
            routed_lane = lane(
                "implement-01",
                "implement",
                "gpt-5.6-sol",
                "xhigh",
            )
            routed_lane["enabled"] = True
            write_json(
                workflow / "orchestration.json",
                {
                    "workflow": {"goal": "由 authoritative artifacts 更新。"},
                    "rounds": [
                        {
                            "round_id": "round-001",
                            "lanes": [routed_lane],
                        }
                    ],
                },
            )
            write_json(workflow / "runner-evidence.json", {"agents": []})
            write_json(
                workflow / "rounds" / "round-001" / "lane-runs" / "implement-01.json",
                {"status": "complete"},
            )

            emitted = subprocess.run(
                [sys.executable, str(RENDER_CARD), str(workflow), "--emit"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, emitted.returncode, emitted.stdout)
            self.assertIn("Agent Workflow · RUNNING", emitted.stdout)
            self.assertIn("由 authoritative artifacts 更新。", emitted.stdout)
            self.assertIn(
                "■ complete · `implement-01` · implement *(Sol · xhigh · inherited)*",
                emitted.stdout,
            )
            persisted = json.loads(
                (workflow / "swarm-card.json").read_text(encoding="utf-8")
            )
            implement_agent = next(
                agent
                for phase in persisted["phases"]
                for agent in phase["agents"]
                if agent["lane_id"] == "implement-01"
            )
            self.assertEqual(
                {"model": "gpt-5.6-sol", "effort": "xhigh"},
                implement_agent["routing"]["planned_route"],
            )

    def test_v2_rejects_executor_type_legend_symbols(self) -> None:
        card = self.card()
        card["legend"]["native"] = "◆"
        with self.assertRaisesRegex(
            render_swarm_card.SwarmCardError,
            "executor-type legend keys are not allowed",
        ):
            render_swarm_card.validate_card(card)

    def test_workflow_validator_rejects_invalid_v2_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workflows"
            result = subprocess.run(
                [
                    sys.executable,
                    str(NEW_WORKFLOW),
                    "Invalid card fixture",
                    "--root",
                    str(root),
                    "--runner-mode",
                    "manual_simulation",
                    "--lanes",
                    "discover,verify",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, result.returncode, result.stdout)
            workflow = root / "invalid-card-fixture"
            card_path = workflow / "swarm-card.json"
            card = json.loads(card_path.read_text(encoding="utf-8"))
            card["legend"]["native"] = "◆"
            write_json(card_path, card)

            verified = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_ROOT / "scripts" / "verify_workflow.py"),
                    str(workflow),
                    "--mode",
                    "scaffold",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(0, verified.returncode)
            self.assertIn("executor-type legend keys are not allowed", verified.stdout)

    def test_manual_simulation_is_textually_labeled_without_executor_symbol(self) -> None:
        card = render_swarm_card.build_initial_card(
            slug="spec-roundtable",
            runner_mode="manual_simulation",
            round_id="round-001",
            round_budget=2,
            lanes=[
                {
                    "id": "roundtable-01",
                    "lane": "roundtable",
                    "runner": {"agent_type": "none"},
                }
            ],
            goal="討論兩個規格方向。",
        )
        output = render_swarm_card.render_card(
            card,
            card_path=Path("swarm-card.json"),
        )
        self.assertIn("*(inherited model · simulated)*", output)
        self.assertNotIn("◇", output)

    def test_new_workflow_scaffolds_option_a_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workflows"
            result = subprocess.run(
                [
                    sys.executable,
                    str(NEW_WORKFLOW),
                    "中文 workflow",
                    "--root",
                    str(root),
                    "--runner-mode",
                    "manual_simulation",
                    "--lanes",
                    "discover,review,verify",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(0, result.returncode, result.stdout)
            path = root / "workflow" / "swarm-card.json"
            card = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(render_swarm_card.CARD_SCHEMA, card["schema_version"])
            self.assertNotIn("native", card["legend"])
            self.assertEqual(3, card["summary"]["agents_planned"])
            output = render_swarm_card.render_card(card, card_path=path)
            self.assertIn("**Discover**", output)
            self.assertIn("**Review & Challenge**", output)
            self.assertIn("**Verify**", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
