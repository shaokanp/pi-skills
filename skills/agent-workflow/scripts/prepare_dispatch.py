#!/usr/bin/env python3
"""Refresh immutable lane dispatch digests after orchestration planning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clean_orchestrator import (
    CleanOrchestratorError,
    build_empty_completion_density,
    prepare_clean_runtime_dispatch,
)
from execution_efficiency import (
    bind_input_refs,
    ExecutionEfficiencyError,
    refresh_dispatch_digest,
    lane_transport_state,
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
        prepare_clean_runtime_dispatch(orchestration)
        bind_input_refs(workflow_dir, orchestration)
        contract_digest = workflow_contract_sha256(orchestration)
        count = 0
        for round_plan in orchestration["rounds"]:
            round_id = round_plan["round_id"]
            for lane in round_plan["lanes"]:
                if lane.get("enabled") is True:
                    transport_state = lane_transport_state(
                        workflow_dir,
                        round_id,
                        lane,
                    )
                    if transport_state == "partial":
                        raise ExecutionEfficiencyError(
                            f"{round_id}:{lane.get('id')} has partial terminal transport"
                        )
                    if transport_state == "terminal":
                        continue
                    refresh_dispatch_digest(lane, round_id, contract_digest)
                    count += 1
        validate_orchestration_efficiency(
            orchestration,
            runner_mode,
            orchestration["execution_efficiency"],
            allow_draft=False,
            workflow_dir=workflow_dir,
            allow_terminal_input_drift=True,
        )
        write_json_atomic(path, orchestration)
        runner_path = workflow_dir / "runner-evidence.json"
        if orchestration.get("clean_orchestrator_runtime") is not None and runner_path.is_file():
            runner_evidence = json.loads(runner_path.read_text(encoding="utf-8"))
            completion_density = runner_evidence.get("completion_density")
            if isinstance(completion_density, dict) and not completion_density.get("entries"):
                runner_evidence["completion_density"] = build_empty_completion_density(
                    orchestration
                )
                write_json_atomic(runner_path, runner_evidence)
    except (
        OSError,
        KeyError,
        json.JSONDecodeError,
        ExecutionEfficiencyError,
        CleanOrchestratorError,
    ) as exc:
        raise SystemExit(f"Cannot prepare dispatch: {exc}") from exc
    print(f"refreshed {count} dispatch digest(s): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
