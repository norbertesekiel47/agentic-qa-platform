# ADR-0002: Hybrid perception — accessibility tree for actions, vision for verification

- Status: Accepted
- Date: 2026-09-27

## Context
The agent must perceive and act on web pages. Many real QA bugs are visual (a Pay button hidden behind a cookie banner, overlapping elements), while actions must be precise and replayable. 2026 comparisons report DOM/accessibility-driven stacks as roughly 12–17 points more reliable on common tasks, while vision-driven stacks reach canvas-only and image-driven UIs.

## Options
1. **Accessibility tree only** — fast, cheap, precise, replayable; blind to visual bugs, canvas, and unlabeled custom widgets.
2. **Vision only (computer use)** — universal, catches visual bugs; slower, costlier per step, less precise clicks, weaker determinism.
3. **Hybrid** — tree for actions (refs compile into locators), screenshots for verification and visual assertions, vision fallback when the tree has no usable element.

## Decision
Option 3. A tree-only QA agent would pass pages that are visibly broken; a vision-only agent makes every step slow and expensive. Hybrid matches where the field has converged and fits the explore–compile–heal design (tree refs compile to robust locators).

## Consequences
- Two perception code paths to maintain and test.
- The benchmark publishes an ablation (tree-only vs vision-only vs hybrid) — evidence for this ADR.
- `vision_fallback` role requires a vision-capable model; enforced by capability validation (ADR-0007).
