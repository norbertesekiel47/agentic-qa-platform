# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo departs from the skills' defaults in one place: **ADRs live in `ADRs/`, not `docs/adr/`.** Wherever a skill says `docs/adr/`, use `ADRs/`.

## Before exploring, read these

- **`AGENTS.md` §2**: the single-owner table. Each fact has one owning doc (PRD, ARCHITECTURE, DATA_MODEL, API, …). Read the owner for the area you're touching.
- **`CONTEXT.md`** at the repo root: the domain glossary.
- **`ADRs/`**: read ADRs that touch the area you're about to work in. `ADRs/README.md` indexes them with their status. Read any Amendment sections too.

If `CONTEXT.md` doesn't exist, **proceed silently**. Don't flag its absence; don't suggest creating it upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates it lazily when terms actually get resolved.

## File structure

```
/
├── AGENTS.md        ← rules + single-owner doc table
├── CONTEXT.md       ← glossary
├── ADRs/
│   ├── README.md    ← index, template, amendment rules
│   └── NNNN-slug.md
└── PRD.md, ARCHITECTURE.md, DATA_MODEL.md, API.md, …
```

## Writing ADRs

This overrides `domain-modeling`'s ADR-FORMAT:

- Write new ADRs in `ADRs/`, numbered after the highest existing `ADRs/NNNN-*.md`. Never create `docs/adr/`.
- Follow `ADRs/README.md`: its template, its index row, and its supersede/amend rules.

## CONTEXT.md owns vocabulary, not definitions

`CONTEXT.md` names concepts and the synonyms to avoid. Formats, schemas, endpoints and rules keep their owner from `AGENTS.md` §2 (e.g. DATA_MODEL.md owns the compiled-script format). Link to the owning doc; don't restate its content.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0003 (explore, compile, heal), but worth reopening because…_
