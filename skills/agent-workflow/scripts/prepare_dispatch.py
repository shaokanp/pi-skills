#!/usr/bin/env python3
"""Refresh immutable lane dispatch digests after orchestration planning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execution_efficiency import (
    bind_input_refs,
    ExecutionEfficiencyError,
    refresh_dispatch_digest,
    validate_execution_policy,
    validate_orchestration_efficiency,
    workflow_contract_sha256,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", help="Path to .workflow/<slug>")
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir)
    path = workflow_dir / "orchestration.json"
    try:
        orchestration = json.loads(path.read_text(encoding="utf-8"))
        orchestrator = orchestration["orchestrator"]
        runner_mode = orchestrator["runner_mode"]
        validate_execution_policy(orchestration["execution_efficiency"], runner_mode)
        bind_input_refs(workflow_dir, orchestration)
        contract_digest = workflow_contract_sha256(orchestration)
        count = 0
        for round_plan in orchestration["rounds"]:
            round_id = round_plan["round_id"]
            for lane in round_plan["lanes"]:
                if lane.get("enabled") is True:
                    refresh_dispatch_digest(lane, round_id, contract_digest)
                    count += 1
        validate_orchestration_efficiency(
            orchestration,
            runner_mode,
            orchestration["execution_efficiency"],
            allow_draft=False,
            workflow_dir=workflow_dir,
        )
        write_json_atomic(path, orchestration)
    except (OSError, KeyError, json.JSONDecodeError, ExecutionEfficiencyError) as exc:
        raise SystemExit(f"Cannot prepare dispatch: {exc}") from exc
    print(f"refreshed {count} dispatch digest(s): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
