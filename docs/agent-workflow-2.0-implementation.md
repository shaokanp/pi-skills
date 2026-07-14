# Agent Workflow 2.0 — Native Thin Team Implementation

狀態：Source implementation complete；release promotion pending blind quality/elapsed evaluation
依據：`skills/agent-workflow/SKILL.md`
日期：2026-07-15

## 1. Source shape

```text
skills/agent-workflow/
├── SKILL.md                         # canonical native behavior
├── README.md                        # zh-TW guide
├── README.en.md                     # English guide
├── agents/openai.yaml               # host-facing invocation metadata
├── evals/evals.json                 # behavior/evaluation corpus
└── scripts/test_native_team_skill.py # repository contract test only
```

The shipped skill contains no agent runtime, process supervisor, routing engine,
artifact protocol, compatibility executor, or persisted workflow workspace.
Agent lifecycle is provided entirely by native host collaboration tools.

## 2. Implemented contract

- The current agent is the Orchestrator.
- Specialists receive self-contained packets with `fork_turns="none"`.
- Team size and roles derive from independence, risk, proof value, and capacity.
- Ready independent work launches in one wave.
- Parallel writers require visibly disjoint ownership; uncertainty yields one writer.
- Material challenge uses `CLAIM / EVIDENCE / RISK / REQUEST`.
- Findings return to the original owner for one bounded repair.
- Source changes require a fresh read-only verifier and before/after source fingerprint.
- Research and decisions add a Challenger or Judge only when risk warrants it.
- Polling, progress narration, recursive orchestration, and external lifecycle runners
  are forbidden.
- Commit, publish, release, and production mutations remain explicit approval gates.

## 3. Deterministic validation

`scripts/validate-skill.sh agent-workflow` runs one package-owned suite:

```text
skills/agent-workflow/scripts/test_native_team_skill.py
```

Static assertions check that the instruction contract declares trigger behavior,
native tool vocabulary, team design, parallelism, write ownership, task packets,
challenge and repair, fresh verification, polling prohibitions, and authority gates.
They also check guides, host metadata, registry/changelog consistency, eval-corpus
structure, and package hygiene. They do not execute the host's native agent lifecycle.

Repository-level preflight additionally validates all skills, public-tree safety,
maintenance tests, package contents, and generated artifacts.

## 4. Native dogfood receipt

The source cutover was reviewed through the same native pattern:

- the current agent orchestrated two fresh read-only specialists in parallel;
- material findings were repaired by the original owner;
- a fresh verifier rejected an unconditional-verification diagram;
- the bounded diagram repair went through a distinct fresh re-verifier;
- the re-verifier passed with no unresolved material finding;
- relevant source fingerprints matched before and after read-only verification.

This proves the challenge and repair mechanics can detect and correct contract drift.
The native collaboration surface did not expose exact descendant token totals or a
complete elapsed timer for the already-started run, so this receipt does not claim
token or performance promotion evidence.

## 5. Promotion gate

Blind evaluation must judge frozen tasks in this order:

1. correctness and required outcomes;
2. evidence quality and regression coverage;
3. blind artifact quality;
4. elapsed time and parallelism;
5. total tokens when the host exposes complete accounting.

Promotion requires correctness non-inferiority, no terminal failure, and either a
material quality improvement or an elapsed-time improvement without quality loss.

## 6. Repository and release gates

After source freeze, run one authoritative `bash scripts/preflight.sh` receipt.
Then, only with explicit approval:

1. commit source;
2. dry-run local release;
3. execute local release and verify production parity;
4. push or publish.

Before release, rollback means reverting the source commit. After release, rollback
means restoring the previously packaged skill version.
