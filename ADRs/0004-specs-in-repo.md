# ADR-0004: Structured natural-language specs; the repo is the source of truth

- Status: Accepted
- Date: 2026-09-27

## Context
Two linked questions: how users express tests (the test-oracle problem — vague specs make the agent guess, producing false positives), and where specs and compiled scripts live (different branches have different UIs).

## Options
Format:
1. Free-form English — easy, but vague success criteria.
2. **Structured Markdown + YAML frontmatter** — `goal`, `preconditions`, optional `steps`, explicit `expect` assertions, `invariants`.
3. Gherkin — familiar but rigid; adds syntax without adding what the agent needs.

Location:
1. **Repo is the source of truth** — specs and compiled scripts committed; heals arrive as PR commits; dashboard edits open PRs.
2. Dashboard DB is the source of truth — easier for non-developers; compiled scripts need branch awareness; tests drift from code.
3. Split — specs in repo, compiled scripts in SaaS keyed by branch.

## Decision
Structured specs (format option 2) stored in the repo (location option 1). Explicit expectations keep the false-positive rate low; universal invariants (no 5xx, no console errors, no uncaught exceptions) catch bugs nobody wrote a test for. Repo storage gives branch-correctness for free: a PR that redesigns checkout carries its own healed checkout script, reviewed and merged together.

## Consequences
- Requires a GitHub App with Contents write (heal commits, spec PRs) — ADR scope in API.md §5.
- GitHub's suggested-changes feature can't target files outside the PR diff, so heal acceptance uses Checks API requested actions.
- Spec indexing on push keeps the dashboard's view current; the DB stores indexed copies, never the canonical version.
- Spec-less exploratory mode is a later feature.
