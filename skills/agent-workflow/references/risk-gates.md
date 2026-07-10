# Risk Gates

Use this checklist before launching or continuing an agent-loop workflow when risk is ambiguous.

## Ask For Approval

Ask one clear approval question before work that may:

- delete, overwrite, mass-rename, force-push, rewrite history, or remove user-created files
- deploy, publish, email, post, purchase, create public resources, or mutate external systems
- run database migrations, broad codemods, dependency upgrades, or long-running expensive jobs
- touch credentials, secrets, billing, production data, user accounts, private customer data, Calendar details, Slack private content, Notion, Linear, Vault, or other sensitive systems
- spawn many agents, run unusual compute, or create many persistent run workspaces
- create or switch branches/worktrees when active workspace policy requires explicit approval
- make irreversible Git or repository operations

Do not bury several risky approvals inside one broad question.

## Usually Safe Without Extra Approval

Usually safe after the user asks you to proceed:

- reading local files in the requested workspace, subject to channel restrictions
- reading active instructions, memory files, architecture docs, diffs, and public/official docs
- drafting plans, lane prompts, reports, or local run workspace files
- running narrow tests, linters, typechecks, dry runs, and non-destructive diagnostics
- creating a small `.workflow/<slug>/` run workspace for a non-trivial requested agent-loop workflow
- spawning a small number of subagents only when the user explicitly asked for agent teams, subagents, swarm, dynamic workflow, or parallel work

## Ambiguous Risk

Prefer a reversible next step:

1. Inspect read-only state.
2. Draft the exact command or action.
3. Explain likely effect and rollback path.
4. Ask for approval before execution.

If approval is denied or unavailable, continue only with safe read-only planning, local drafts, or non-destructive checks.

## Workspace Overrides

Workspace policy wins over generic workflow advice:

- Follow active workspace policy for branches, worktrees, isolated checkouts, and PR review environments.
- Dirty worktree means shrink the change set and stage/commit scope, not automatically create isolation.
- Slack-restricted contexts cannot access private files, Vault notes, or disallowed tools.
- External actions require explicit confirmation unless the user invoked an approved autopilot route that clearly includes them.
