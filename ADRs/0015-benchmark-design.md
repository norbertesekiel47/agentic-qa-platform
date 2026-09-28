# ADR-0015: Benchmark on real open-source apps with planted bugs and benign changes

- Status: Accepted
- Date: 2026-09-27

## Context
The README's numbers, résumé bullets, and interview stories all come from the benchmark. It must withstand the critique "you wrote the apps and the bugs, so of course it catches them," and it must make heal-classification accuracy measurable.

## Options
1. Custom demo apps with toggled bugs — fast, full control; vulnerable to the rigged-demo critique.
2. **Two established open-source apps + curated planted bugs + benign changes**, published as an open benchmark.
3. Automated mutation testing — cheap volume; most mutants are invisible or trivial in a UI.

## Decision
Option 2: 40–60 hand-written, flag-toggled bugs across six categories (functional, visual/layout, backend 5xx, JS error, broken flow, data display) and 15–20 benign intentional changes (moved/restyled/relabeled elements, reordered navigation). The benign set is what makes heal accuracy — and wrongly flagging intentional changes — measurable.

## Consequences
- M0 must select apps with compatible licenses and seedable data.
- Ground-truth manifests are versioned and reviewed; results cite commit SHA and model config.
- The benchmark becomes the Calibrated Eval Toolkit's first agentic dataset.
- Honest reporting: per-category results with confidence intervals, misses included.

## Amendment — 2026-09-27 (external design review)

- **Counts fixed:** 60 planted bugs and 20 benign UI changes.
- **Frozen splits before any tuning:** dev (20 bugs + 6 benign) for building and PR smoke tests; **test (40 bugs + 14 benign)** for release numbers only — never run in CI, never used for tuning. Related bugs share a split. Once test results influence a design decision, the test set is burned and a v2 test set is written for the next release.
- **Wording:** "benign intentional changes" are defined operationally as UI changes under which every `expect` assertion still holds; the heal metric measures `drift_consistent` classification on this set, not author intent.
- **Statistical conventions:** every interval states method and sidedness. The flake metric uses 30 specs × 20 repeats = 600 replays with a one-sided exact 95% upper bound (20 repeats of one spec could only bound flakiness at ~13.9%).
- **Pilot first (verification review):** M0 builds a development-only pilot (one app, ~5 bugs, 2 benign changes) to validate the compiler and heal contracts before the full benchmark is authored; the full 60 + 20 set is written and its split frozen at the start of M3, before any tuning on it. Pilot cases join the dev split.
- **Flake statistics corrected:** replays of one spec aren't independent, so the pooled replay rate is reported with its independence assumption stated, alongside per-spec flake incidence with a one-sided exact bound over 30 specs (the assumption-free claim).
