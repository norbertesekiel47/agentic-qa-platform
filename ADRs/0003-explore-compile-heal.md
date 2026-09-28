# ADR-0003: Explore once, compile, replay without LLM, heal on drift

- Status: Accepted
- Date: 2026-09-27

## Context
A test must be deterministic and cheap to run on every PR. LLM-driven runs are neither. Pure selector scripts are deterministic but break on harmless UI changes.

## Options
1. **Agent drives every run** — adaptive but costly per PR and non-deterministic (flaky by design).
2. **Explore → compile → replay → heal on drift** — first run agentic; the successful path compiles to a deterministic script; replays make no LLM calls; on a broken step the agent resumes and classifies *intentional change vs bug*.
3. **Generate Playwright code once** — portable, but loses runtime judgment; every UI change requires regeneration.

## Decision
Option 2. It delivers $0 LLM cost on unchanged runs, determinism, and concentrates model usage on the single hard judgment — which becomes a measurable classification problem and the flagship case study for the Calibrated Eval Toolkit. Option 3's portability is kept as a later "export to Playwright" feature.

## Consequences
- Compiled scripts are artifacts that need a stable, diffable format (DATA_MODEL §7) and multi-strategy locators.
- A "drift" definition is required (no unique actionable match within the wait budget).
- Heal classification accuracy is the core product risk; it is measured before SaaS work begins (ADR-0016).
- Replay must be provably LLM-free (test asserts no model client in replay mode).

## Amendment — 2026-09-27 (external design review)

Two refinements; the decision itself stands.

1. **Replay modes.** "Replays make no LLM calls" applies to **`strict` mode** (the default and the CI mode). Every assertion must compile to a deterministic check — including deterministic visual checks (in viewport, not occluded via center hit-test, minimum size/contrast, optional baseline pixel-diff). Model-assisted visual verification exists only in an opt-in **`verified` mode**, with its own cost line and benchmark denominators. This resolves a contradiction between "zero-LLM replay" and "verifier model for visual assertions."
2. **What the healer classifies.** Evidence can show that behavior changed, not what the author intended. The heal verdicts are therefore `drift_consistent` (UI changed; after locator/step repair every expectation and invariant still passes), `expectation_violated`, and `inconclusive`. Heal patches may modify only `/steps/…`; assertions and invariants are immutable in heals *(refined in the addendum below: target locators are also repairable; side-effect steps are not)*. Intended behavior changes are expressed by editing the spec. PR context may be provided as hints, never as a claim of intent.

### Amendment addendum — 2026-09-27 (verification review)

3. **Expectation coverage.** Every `expect` item compiles to ≥ 1 check that actually establishes it (UI, target content, URL, the browser's network traffic, read-only state probes, deterministic visual checks); unsupported clauses fail compilation by name. A weaker proxy (e.g., "no confirmation heading" for "no order created") is never substituted.
4. **Targets vs assertions.** The compiled format separates *targets* (an element's meaning + locators) from *assertions*. Heals may change target locators and non-side-effect steps only — never assertions, target meanings, side-effect steps, or replay-safety flags. An assertion whose target won't resolve is drift (binding repair); a resolved check that evaluates false is `expectation_violated`.
