# Benchmark

Planted bugs and benign UI changes in vendored open-source apps, used to measure the agent (TESTING.md §5, ADR-0015). The design is in ADR-0022.

| Path | What | Policed by our gates |
|---|---|---|
| `apps/<app>/` | Vendored third-party apps, with our flag plumbing, `/test-api/*` routes and planted changes (see each app's README) | No: gitleaks only (ADR-0021) |
| `apps/<app>/qa/` | That app's specs and their dry compile review (`REVIEW.md`) | No (inside `apps/`), so review them by hand |
| `harness/` | Our Python harness, a uv workspace member (ADR-0027). `toggle_checks.py` runs in the checks image (`checks.Dockerfile`, `checks-requirements.txt`, `chromium-seccomp.json`) | Yes |
| `manifest.v1.json` | Ground truth, keyed by case ID (format: DATA_MODEL.md §8) | Yes |
| `manifest.v1.split.sha256` | The split freeze (TESTING.md §5) | Yes |

## Commands

```bash
uv run python bench/harness/manifest.py                # validate the manifest and the split freeze
uv run python bench/harness/manifest.py --split-hash   # print the hash after a deliberate split change
uv run pytest bench/harness                            # harness tests (AGENTS.md §4's test gate runs them too)

uv run python bench/harness/flags.py set <case-id>     # switch the case's app to that case (recreates, reseeds, verifies)
uv run python bench/harness/flags.py clean <app>       # the clean app
uv run python bench/harness/flags.py show <app>        # which case is on; fails if the tiers disagree
uv run python bench/harness/flags.py selftest <app>    # alternate the reserved flag 0000 and clean, 10 cycles

uv run python bench/harness/toggle.py <app> --cycles 5 # every check on the clean app and under each case's flag

uv run python bench/harness/pilot.py <app> --out .scratch/<new dir> [--spec <id> ...] [--repeat 3] [--compiled-dir DIR] [--patches DIR]
```

The flag commands need the app's images built (`docker compose build` in `apps/<app>/`). The self-test proves flag delivery to both tiers. `toggle.py` proves each case's planted change, and that the case's flag switches nothing else (ADR-0023). It builds the app's images from the working tree and the checks image, then runs every case's check (`toggle_checks.py`, as a non-root user with Chromium's sandbox on) on the clean app and under each case's flag. It fails when a case has no check, or when a check sees anything other than its exact expected state: planted under its own flag, clean otherwise. A pull request that adds or changes cases includes its output.

## Pilot acceptance

`pilot.py` replays compiled pilot scripts through the strict executor, with no model client, and scores them against the manifest (ADR-0023). It admits every input before its first Docker call: a clean source tree, a new `--out` directory, the manifest, each selected spec's script (`<compiled-dir>/<id>.json`, by default the app's `qa/.compiled/`) and each patch. Replay is confined to the app's stack origin (`host_origin` in `flags.py`, `http://127.0.0.1:4100` for Conduit): it resets, navigates and acts there alone. The QA project's `base_url` must be that origin, compared as `aqa_core` reads origins, so a trailing slash doesn't matter but `localhost` isn't `127.0.0.1`. A spec's `allowed_origins` and the config's `egress.private_origins` must be empty. Admission refuses any other or extra origin, so no other local instance receives the reset POST or the replay.

- **Selection.** `--spec ID`, repeatable, picks specs in the order given; without it every spec runs, by ID. The report's `selected` lists them, and `omitted` lists every other spec in the app's QA project, sorted. An omitted spec needs no compiled script and gets no pair. A selected spec with no manifest row under a case still gets one, reported `unscored`.
- **Patches.** `--patches DIR` holds `<case-id>.<spec-id>.json` files, one for each `drift_consistent` row of a selected spec under a dev case. Each one replaces target locators only (ADR-0023's rebinding format). Any other file refuses the run.
- **Order.** It builds the images, switches to the clean app and replays each spec `--repeat` times. Then, in case-ID order, it switches to each dev case's flag and replays each spec once. A `drift_consistent` row with a patch also gets a fresh, patched attempt, but only when its original passed or failed only to find a binding. Test-split cases are never switched. The run stops at the first fatal pair.
- **Ending.** It switches back to the clean app only when nothing stopped short and every attempt's resources are known closed. Otherwise the report says `reservation_release: forbidden`, and whoever holds the stack's reservation reconciles it before releasing it.
- **Outputs.** All under `--out`: `report.json`, one receipt per attempt in `attempts/`, and the private run records in `.aqa/runs/`. The report and each receipt are written once. They hold outcomes, counts, IDs and hashes, never a URL or error text. The run records are the executor's own, may hold page text, and stay private (#50).
- **Limits.** A benign pair accepts at most `accepted_pending_C`, because the assertions' locator provenance is still to come. A repeat other than 3 is marked `diagnostic`. Exit 0 here is not the M1 exit on its own. A report is citable only from a clean commit with `source_unchanged` true.

| Exit | Meaning |
|---|---|
| 0 | Every pair passed, matched, was accepted pending C, or is unscored |
| 1 | A scored pair disagrees with the manifest (`mismatch`), or a benign pair has no patch (`patch_missing`) |
| 2 | Invalid input, found before any Docker call. stderr shows our own message, or only the type of a manifest, Docker, git or file error, since its text can quote input (git may print its own message, naming paths, first); run `manifest.py` or `flags.py show` for details |
| 3 | A fatal attempt, one the replay couldn't complete (a reset, timeout or cleanup failure) or one that ran with infrastructure events, an egress block, an errored run, an unsettled step or a check timeout. Also a stop short of the end (an unexpected error, a process exit, a failed switch), a source that changed during the run or can't be read at its end, or evidence that couldn't be written |
| 12 | A test secret the spec references can't be used, before the run or at an attempt |
| 130 | Interrupted |

When several apply, the highest-ranked wins: 130, then 12, then 3, then 1.
