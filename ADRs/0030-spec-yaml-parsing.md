# ADR-0030: Reading specs and the project config: PyYAML narrowed to YAML 1.2's core schema

- Status: Accepted
- Date: 2026-10-01

## Context
#39 reads every spec's frontmatter and the project config (`qa/config.yaml`) before a browser or a model is involved. Both are YAML (DATA_MODEL §6, §9), and both must reject unknown and duplicate keys. Until now no YAML parser was a dependency: the benchmark's manifest validator counted a spec's expectations line by line, and DATA_MODEL §6 carried a layout rule only for it.

What the parser has to get right:
- **Duplicate keys.** A second `id:` or `base_url:` must be an error, not the value that wins.
- **YAML 1.1's implicit types.** Measured with PyYAML 6.0.3's `safe_load` on 2026-10-01:
  - `locale: no` reads as `False`, though `no` is Norwegian's language tag;
  - `017` reads as 15, and `1:20` as 80;
  - `2026-01-04` reads as a `date`, and `.nan` as a float NaN;
  - `a: 1` then `a: 2` gives `{'a': 2}`.
- **`spec_hash`** is the sha256 of the frontmatter's canonical JSON (DATA_MODEL §7), so every parsed value must be a JSON value: no dates, sets or NaN.
- **Aliases** let a few lines expand into a large document once the hash serializes them.
- **Flow style.** Every pilot spec writes `invariants: { inherit: true }` and `tags: [auth]`.

## Options
1. **PyYAML**, already in `uv.lock` through langchain-core and vcrpy. Its loaders follow YAML 1.1 and keep the last of two equal keys, so the narrowing would be ours. Typeshed publishes its stubs (`types-pyyaml`).
2. **ruamel.yaml**: YAML 1.2, and duplicate keys are errors by default. It would be a new dependency outside the lock.
3. **strictyaml**: a restricted YAML that refuses flow style by default, which every pilot spec uses.

## Decision
PyYAML 6.0.3 (option 1), with a loader narrowed to YAML 1.2's core schema (`aqa_core.strict_yaml`), then Pydantic for the shape:
- **Types.** Plain scalars resolve to null (`~`, `null`, empty), `true`/`false`, decimal integers and floats, and everything else is a string. There's no `.inf` or `.nan`, and no dates.
- **Tags.** Every node's tag must be the one it would have with none written, so `!!set`, `!!timestamp`, `!custom`, a scalar tag on a collection (`!!null {a: 1}`) and a tag that changes a scalar's type (`!!int '3'`) are refused. Replacing SafeLoader's constructors isn't enough on its own: PyYAML reads a collection whose tag has no constructor as a plain mapping or list (LAB_NOTES, 2026-10-01).
- **Keys** must be strings, each once in its mapping. A problem is reported at its line in the file.
- **Aliases** are refused, reported at the anchor's line.
- **Shape.** Pydantic models with `extra="forbid"`, strict types and frozen instances check the result. Each problem reads `<file>: <key>: <problem>`, and every problem in the files read is reported together, as a `SpecError` (ADR-0024's `spec_error`).
- **Stubs.** `types-pyyaml` joins the dev group for mypy `--strict`.

Option 2 would trade a few dozen lines of narrowing for a new dependency, and it would still need our own refusal of dates and aliases for the hash. The pure-Python loader is fast enough: a spec is a few hundred bytes.

## Consequences
- **What a spec can't write:** aliases, tags that change a value's type, `yes`/`no`/`on`/`off` as booleans, octal or sexagesimal numbers. A date is a string, quoted or not. An anchor with no alias, and a tag that names the type a value already has (`!!str no`, `!!map {…}`), change nothing and are accepted.
- **Text YAML can't read is a spec error too:** a control character, a file that isn't UTF-8, nesting too deep for the parser, and a number too long for Python.
- **Hashes are stable** across machines and runs: the canonical JSON holds only JSON values, and the hash covers the YAML as parsed, not a model that could gain defaults.
- **The manifest validator reads specs through the parser**, with each app's `qa/config.yaml`, replacing the layout-based reading in ADR-0022's 2026-09-28 amendment ("invariant ground truth and spec-file checks"). The harness therefore runs in the workspace (`uv run python bench/harness/…`). That ends ADR-0022's "standard library only until M1 brings uv and Pydantic", and ADR-0023's `toggle.py` is no longer standard library only. `toggle_checks.py`, which runs in the checks image, imports neither.
- **DATA_MODEL §6 drops its layout rule.**
- **PyYAML is a direct dependency of `aqa-core`**, at the version the lock already held.
