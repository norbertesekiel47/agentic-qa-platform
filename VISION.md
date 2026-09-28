# Vision — Agentic QA Platform

> Natural-language E2E tests that run as deterministic replays and self-heal on UI drift. CLI, GitHub App, and multi-tenant SaaS.

## The problem

End-to-end (E2E) tests are the most valuable and most hated tests in a web codebase.

- **They break for the wrong reasons.** A renamed CSS class, a moved button, or a reworded label fails the suite even though nothing is broken. Teams learn to ignore red builds, and then real regressions ship.
- **They are expensive to write and maintain.** Selector-based scripts encode *how* the UI is built instead of *what* the user is trying to do.
- **Pure-LLM "AI testers" trade one problem for another.** Driving every run with a model is slow, costs money on every PR, and is non-deterministic — a test that can pass or fail on the same code is not a test.

## The insight

Use the model only where judgment is needed, and nowhere else.

1. **Explore once.** An agent reads a plain-English spec, drives a real browser, and finds the path through the app.
2. **Compile.** A successful path is compiled into a deterministic script with robust locators and explicit assertions.
3. **Replay for free.** Every subsequent run replays the script in strict mode with **zero LLM calls** — including deterministic visual checks.
4. **Heal on drift.** When a step breaks, the agent resumes from that step and answers the one hard question it *can* answer from evidence: *did the UI merely move — with every expectation still holding — or is an expectation actually violated?* UI drift → a locator-only script update for human review. Violated expectation → a bug report with evidence. The agent never claims to know what the author *intended*; if the intended behavior changed, the author edits the spec's expectations, and heals can never change them.

That classification is the product's core AI problem, and it is measured, not asserted.

## Who it is for

- **Primary:** product engineers on web teams who own their own E2E coverage and run CI on every PR.
- **Secondary:** QA engineers and engineering managers who triage failures and track coverage across repos.

## What success looks like

A developer opens a PR that redesigns checkout. Within minutes the checks report: *"4 flows passed. `signup`: 'Pay' moved into a drawer; after updating the locator, every expectation still holds — [Accept heal]. `checkout-expired-card`: expectation violated — an expired card returns HTTP 500; replay here."* Replays cost nothing; the agent spent tokens only where the UI actually changed.

## Differentiators

| | Selector scripts (Playwright/Cypress) | LLM-every-run agents | **Agentic QA Platform** |
|---|---|---|---|
| Authoring | Code + selectors | Plain English | Structured plain-English specs |
| Deterministic runs | Yes | No | **Yes (compiled replay)** |
| LLM cost per unchanged run | $0 | Every run | **$0** |
| Survives UI refactors | No | Yes | **Yes (heal on drift, reviewed)** |
| Separates "UI moved" from "behavior broke" | No | Implicitly, unmeasured | **Explicitly, benchmarked on a frozen test split** |
| Tests versioned with code | Yes | Usually no | **Yes (specs + compiled scripts in repo)** |

## Non-goals (v1)

- Native mobile apps (web only).
- Load/performance testing.
- Replacing unit or integration tests.
- A general-purpose web agent for arbitrary live sites (hosted runs only target verified domains).
- Billing and paid plans (BYOK only; billing is a later phase).

## Principles

1. **Deterministic by default, agentic by exception.**
2. **Evidence over assertion.** Every verdict links to screenshots, the accessibility snapshot, network/console logs, and the agent's reasoning.
3. **Humans approve changes to tests.** Heals are locator-only proposals, never silent rewrites, and never change what "correct" means.
4. **Tenant isolation is enforced by the database**, not promised by application code.
5. **Every number is reproducible.** No metric appears anywhere without the command and commit that produced it.

## Role in the portfolio

This is the flagship of a three-project portfolio:

- **Agentic QA Platform** (this repo) — builds agents and generates trajectories.
- **Calibrated Eval Toolkit** — measures those trajectories with statistical rigor (drift-vs-violation classification is its first agentic case study).
- **MCP Defense Gateway** — can front this platform's browser tools over MCP, and is itself measured on a published attack benchmark.

The projects are loosely coupled through open standards (MCP, OpenTelemetry). Each runs and demos on its own.
