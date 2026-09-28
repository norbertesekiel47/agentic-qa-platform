# ADR-0001: Build an agentic QA tester, not a general web agent

- Status: Accepted
- Date: 2026-09-27

## Context
The portfolio needs a flagship computer-use/browser agent project. General-purpose browser agents (Browser Use, OpenAI Operator, Claude for Chrome) are a crowded field backed by well-funded teams; a solo project cannot win on generality. It can win on depth in one domain with measurable outcomes.

## Options
1. **Agentic QA tester for web apps** — measurable (planted bugs), safe (runs against apps the user controls), maps to a real product category (QA Wolf, Momentic), and produces trajectories the eval toolkit can score.
2. **General web agent on a public benchmark (WebArena/WebVoyager)** — comparable numbers, but likely below frontier labs; "me too" story.
3. **Task agent on live real-world sites** — relatable demo, but sites change constantly, CAPTCHAs/ToS interfere, evals are unreproducible.
4. **Accessibility auditor agent** — novel and tied to ADA-litigation research, but narrower appeal.

## Decision
Option 1. It yields hard, reproducible numbers (detection, false positives, heal accuracy, cost), is safe by construction, and addresses a pain every engineering org recognizes (flaky E2E tests). Option 4 is preserved as a future "accessibility mode."

## Consequences
- Requires building a benchmark (ADR-0015).
- Hosted runs need abuse controls since the agent can target URLs (ADR-0012).
- Competitive framing against Momentic/QA Wolf must be honest in docs.
