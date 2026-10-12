# Pilot fixtures

Compiled scripts written by hand for the pilot acceptance proof (#51, ADR-0023). `pilot.py --compiled-dir bench/harness/fixtures` replays them as it would compiled output, and `test_read_article.py` holds them to the current spec and config.

| File | What |
|---|---|
| `read-article.json` | The read-article spec's script: open the seeded article from the global feed, then seven `text_in_target` checks, `a1` to `a7` for expectations 0 to 6 |
| `read-article.plan.json` | Its coverage plan, one check per expectation |

## Metadata

No model, compiler or confirmation replay produced these files, and the metadata says so.
- **`compiled_by.mode` is `explore`** only because schema version 1 allows no other value (DATA_MODEL §7). `models` is empty and `price_map` reads `none: handwritten fixture`.
- **`confirmed` is `false`** because no confirmation replay ran. Explore writes `false` only for a spec with no reset hook; this spec has one.
- **`compiled_at`** is when the file was written.
- **The click is a side effect**, as its `side_effect_basis` says: no inference recorded otherwise, and the spec's reset hook makes a repeat safe.
- **No origin beyond the stack's.** The spec and config declare no `allowed_origins`, `egress.private_origins` or `egress.subresource_hosts`, which admission refuses (bench/README.md).

## Hashes

Each pinned hash comes from the function that owns it: `spec_hash` from the parsed spec (`load_project`), `coverage.plan_hash` from `aqa_core.coverage_plan.plan_hash` on the plan file, and `compiled_by.subject_contracts` from `aqa_core.project.contracts_fingerprint(config, "read-article")`. The banner's author and date targets carry the config's rows 1 and 2 as contracts, and no other target has one. A change to the spec, the plan or those rows fails `test_read_article.py`. Recompute with the same function, never by hand, and review the checks again.

## Comment bindings

The comment list (`a6`) binds `div.comment-list`, a plain wrapper around Conduit's comment loop (change 6 in `bench/apps/conduit/README.md`). It holds only the comments, so a quote in the prompt, an error or the form can't satisfy it, and an empty list fails rather than going unresolved. The prompt (`a7`) is the column's other direct `div`. A missing or doubled wrapper leaves `a6` `binding_unresolved`, never a pass found elsewhere.
