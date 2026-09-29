# Benchmark

Planted bugs and benign UI changes in vendored open-source apps, used to measure the agent (TESTING.md §5, ADR-0015). The design is in ADR-0022.

| Path | What | Policed by our gates |
|---|---|---|
| `apps/<app>/` | Vendored third-party apps, with our flag plumbing, `/test-api/*` routes and planted changes (see each app's README) | No: gitleaks only (ADR-0021) |
| `apps/<app>/qa/` | That app's specs and their dry compile review (`REVIEW.md`) | No (inside `apps/`), so review them by hand |
| `harness/` | Our Python harness. Standard library only until M1, except `toggle_checks.py`, which runs in the checks image (`checks.Dockerfile`, `checks-requirements.txt`, `chromium-seccomp.json`) | Yes |
| `manifest.v1.json` | Ground truth, keyed by case ID (format: DATA_MODEL.md §8) | Yes |
| `manifest.v1.split.sha256` | The split freeze (TESTING.md §5) | Yes |

## Commands

```bash
python3 bench/harness/manifest.py                   # validate the manifest and the split freeze
python3 bench/harness/manifest.py --split-hash      # print the hash after a deliberate split change
python3 -m unittest discover -s bench/harness       # harness tests (CI runs these in guardrails)

python3 bench/harness/flags.py set <case-id>        # switch the case's app to that case (recreates, reseeds, verifies)
python3 bench/harness/flags.py clean <app>          # the clean app
python3 bench/harness/flags.py show <app>           # which case is on; fails if the tiers disagree
python3 bench/harness/flags.py selftest <app>       # alternate the reserved flag 0000 and clean, 10 cycles

python3 bench/harness/toggle.py <app> --cycles 5    # every case: change absent when clean, present with its flag
```

The flag commands need the app's stack built (`docker compose build` in `apps/<app>/`). The self-test proves flag delivery to both tiers. `toggle.py` proves each case's planted change (ADR-0023): it builds the checks image, runs `toggle_checks.py` as a non-root user with Chromium's sandbox on, and fails when a case has no check or a check sees anything other than the exact clean or planted state. A pull request that adds or changes cases includes its output.
