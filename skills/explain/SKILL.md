---
name: explain
description: Explain complex technical or project material as an evidence-backed comprehension packet covering structure, mechanism, boundaries, and current status. Use for specs, architecture, workflows, diffs, artifacts, or multi-round progress when the user asks how it works, what changed, or what is actually complete. Answer simple facts directly; use agent-workflow to execute multi-agent work and review or audit modes to judge correctness.
---

# Explain

Turn technical or spec-heavy material into an understanding packet the user can
read once and understand. Explain the structure, mechanism, concrete shape, and
boundaries of the subject. Do not critique, audit, quiz, or review correctness
unless the user explicitly asks for that instead.

## Audience Model

Explain as if speaking to a technically literate PM or collaborator who has not
worked on the project and is not currently doing the engineering work. The
output should let that reader understand the subject's shape, moving parts,
responsibilities, runtime behavior, status, important boundaries, and omissions
without reading the code or prior session history.

This means:

- Do not teach basic computer science concepts.
- Do not assume project-specific names are meaningful.
- Translate opaque project terms into responsibilities and mechanisms.
- Show architecture and flow visually when explaining a system, spec, workflow,
  or multi-step change.
- Prefer product and spec comprehension over implementation detail unless the
  implementation detail changes the concept.

## Workflow

1. Identify the object being explained: a concept, plan, spec, artifact, diff,
   workflow, architecture, or progress across rounds.
2. If the object already exists, read current evidence first. Use source files,
   specs, diffs, logs, goal state, workflow artifacts, architecture docs,
   official docs, or result files as appropriate. Do not explain existing state
   from memory when evidence is available.
3. Choose the smallest packet a technically literate reader can understand in
   one pass. Compress large material into structure instead of dumping details.
4. For non-trivial explanations, use this fixed spine: `What this is`, `Why it
   matters`, `Concept model`, `Mechanism`, `Boundaries / not included`, and
   `What to notice`.
5. Define a thing before relying on its name. On first mention, explain what it
   does, why it exists, and how it relates to surrounding parts; only then use
   the project term or shorthand.
6. Draw the boundary explicitly. Identify likely misunderstandings,
   expected-but-undelivered features, unchanged behavior, unproven claims, and
   follow-up gates. Select the exclusions a reasonable reader might assume from
   the context instead of listing every possible non-goal.
7. Put diagrams where they teach the concept. Use a component or concept diagram
   when several named parts interact, and a sequence diagram when behavior
   crosses actors or layers.
8. Include one worked example for non-trivial explanations. Prefer the user's
   real situation.
9. Reply in the user's language. When the request is Chinese and the variant is
   unclear, default to zh-TW. Keep standard technical terms, APIs, commands,
   paths, and code identifiers in English.

## Output Order

Use this order unless a smaller answer is clearly sufficient:

1. One-line answer.
2. `Evidence checked`, when explaining existing state or artifacts.
3. `What this is`.
4. `Why it matters`.
5. `Concept model`.
6. `Mechanism`, with a sequence diagram when useful.
7. `Boundaries / not included`.
8. A status table, timeline, or other external representation for progress.
9. `Worked example`.
10. `What to notice`.

## Completion Check

Before responding, confirm every applicable item is satisfied:

- The explanation is grounded in current evidence, or its evidence limit is
  stated explicitly.
- Project-specific terms are defined by responsibility before shorthand is used.
- The packet includes the sections needed to understand the subject without
  forcing optional sections into a small answer.
- A non-trivial subject has a fitting visual representation and one worked
  example; a simple answer omits them when they add no clarity.
- Boundaries distinguish confirmed exclusions from claims that are merely
  unsupported by the checked evidence.
- The answer explains rather than silently reviewing, auditing, or implementing
  the subject.

## Evidence Rules

- Existing spec or draft: read the spec or draft.
- Existing diff or change: read the diff, touched files, and relevant docs when
  the change affects architecture or workflow.
- Existing progress or rounds: read goal state, workflow logs, results, state
  files, or round summaries.
- Existing architecture or workflow: read the corresponding docs or runtime
  state.
- External technology: use official docs or primary sources.
- Include a short `Evidence checked` list. Do not add line-by-line citations
  unless the user asks for an audit, blame analysis, or correctness review.
- If evidence is incomplete, say exactly what the explanation is based on.
- Separate confirmed exclusions from things merely absent in the checked
  evidence. Use language such as "I did not see evidence that..." when the
  evidence cannot prove a negative.

## External Representation Routing

Choose the representation that exposes the structure best and place it inside
the section it explains.

| Content type | Default representation |
|---|---|
| Concepts, names, boundaries | Component or concept diagram |
| Flow, event, request path | Sequence diagram |
| State transition, lifecycle | State machine |
| Rules, permissions, routing, classification | Decision table |
| Data structures and relations | ER diagram or object model |
| User or agent interaction | Scenario walkthrough |
| UI, dashboard, artifact shape | Wireframe or annotated mock |
| Goal progress, rounds, completion | Status table or timeline |
| Spec shape | Annotated outline or concept map |
| Boundaries and likely misunderstandings | `Assumption / Reality / Why it matters` table |

Do not force one diagram type onto every explanation. Use multiple compact
representations when the subject has both an architectural shape and a runtime
flow.

## Tone Rules

- Assume the reader understands standard CS, product, and workflow concepts. Do
  not over-explain terms such as `state machine`, `interface`, `adapter`,
  `invariant`, or `DTO`.
- Do not invent acronyms, cute metaphors, dramatic verbs, vague nouns, or
  private jargon.
- Do not use a pronoun, label, or project term as a substitute for saying what a
  thing is. Define opaque names by responsibility on first mention.
- Explain through concrete logic: what problem existed, what changed, what each
  part reads and writes, what it affects, and what remains unchanged.
- Prefer a plain responsibility label followed by the source term in
  parentheses when the original name is opaque.
- Use ordinary precise verbs: read, write, check, route, block, fall back,
  require approval, update state, and produce output.
- Do not overclaim exclusions. When evidence is incomplete, name the evidence
  boundary instead of presenting an assumption as fact.
- Keep the explanation neutral. Do not add recommendations, critique, or review
  findings unless the user asks for them.

## Non-Goals

`explain` does not:

- critique the artifact
- review correctness
- challenge the plan
- produce a quiz
- replace code review
- replace audit or blame investigation
- dump raw details

If the user asks for review, critique, audit, blame analysis, or implementation,
use the appropriate mode or skill instead.

## Calibration Example

Read `examples/progress-change-comprehension.md` when you need a model for
explaining a long technical session, phase progress, or a multi-round change.
