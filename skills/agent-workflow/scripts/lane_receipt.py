#!/usr/bin/env python3
"""Validate a persisted lane output and emit its compact receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execution_efficiency import (
    ExecutionEfficiencyError,
    build_lane_receipt,
    receipt_relative_path,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", help="Path to .workflow/<slug>")
    parser.add_argument("round_id")
    parser.add_argument("lane_id")
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir)
    try:
        orchestration = json.loads(
            (workflow_dir / "orchestration.json").read_text(encoding="utf-8")
        )
        round_plan = next(
            item for item in orchestration["rounds"] if item["round_id"] == args.round_id
        )
        lane = next(item for item in round_plan["lanes"] if item["id"] == args.lane_id)
        receipt = build_lane_receipt(workflow_dir, lane, args.round_id)
        output = workflow_dir / receipt_relative_path(args.round_id, args.lane_id)
        write_json_atomic(output, receipt)
    except (OSError, KeyError, StopIteration, json.JSONDecodeError, ExecutionEfficiencyError) as exc:
        raise SystemExit(f"Cannot build lane receipt: {exc}") from exc
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
