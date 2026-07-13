# Agent Workflow Canary temporary-storage contract

## Incident

On 2026-07-14, the macOS temporary root contained a hidden
`.agent-workflow-canary-control` tree with 9,164 top-level workspace identities,
about 497,267 files, and 10,447,352 KiB of data. The corresponding
`TemporaryDirectory` workspaces and visible `.canary-*` siblings were already
gone.

The leak came from split lifecycle ownership. A Canary test owned only its
workspace, while canonical session/app/snapshot/artifact stores, the executable
freeze, `host-evidence.key`, and readonly `frozen-repository` lived beside that
workspace. Removing the workspace therefore did not remove its control plane.
Repeated maintenance sentinels and release entrypoints multiplied the residue.

## Ownership invariant

Every unit or fault-injection case owns one outer temporary container:

```text
case-root/
├── workspace/
├── .canary-session-store-*/
├── .canary-app-store-*/
├── .canary-snapshot-store-*/
├── .canary-artifact-store-*/
├── .canary-freeze-*.json
└── .agent-workflow-canary-control/
```

The canonical stores remain outside `workspace/`, preserving the permission
boundary under test, but no path escapes `case-root/`. Terminal teardown restores
owner write permission on readonly fixture directories and removes the whole
container after normal completion or an exception.

## Policy-managed validation temporary root

`validation_tmp.py` wraps direct Canary execution, Agent Workflow validation,
and repository preflight. Selection is explicit and fail-closed:

1. Existing `PI_SKILLS_TMP_ROOT` supplied by the operator.
2. In CI, existing `RUNNER_TEMP` when `CI` is set.
3. On the maintained Mac, `/Volumes/OWC-4TB/tmp/pi-skills` after verifying the
   OWC mount has a different device identity from `/`.
4. Otherwise validation refuses to start.

Each invocation receives a unique run root and create-once lease containing the
repository, base/run roots, nonce, process ID, and base/mount device identities.
Cleanup replays that exact lease and path containment. It does not use POSIX
ownership, because the OWC volume has ownership disabled. Authority drift blocks
cleanup and preserves the run root for diagnosis.

Moving temporary storage to OWC is defense in depth, not the leak fix. The outer
container invariant is what guarantees zero residue.

## Gate frequency

Development runs only focused tests for the changed seam. Once source is frozen,
`scripts/preflight.sh` runs one authoritative repository gate and records an
exact-fingerprint receipt. Commit, package, local-release, and pre-push paths
reuse that receipt while the source/toolchain/policy fingerprint is unchanged.
Maintenance tests inspect the registered Agent Workflow suite manifest without
executing the complete suite again.

## Retention boundary

Unit/fault control roots are temporary and must be deleted at terminal. Formal
promotion evidence is different: its sealed workspace, host authority key,
qualification receipts, frozen bundle, and independent verifier evidence must be
archived together under the designated promotion archive. A residue cleanup must
never target that archive.

Focused red/green evidence and the single final preflight receipt are recorded in
the ignored `.workflow/canary-storage-repair/` sidecar so recording gate results
does not mutate the fingerprint that the receipt authorizes.
