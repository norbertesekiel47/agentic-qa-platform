# ADR-0007: Model-agnostic, role-based model routing

- Status: Accepted
- Date: 2026-09-27

## Context
Users want to use any provider — OpenRouter, DeepSeek, OpenAI (including Codex-family models), Anthropic — and cost depends on using expensive models only where needed. Most agent steps are navigation (frequent, simple); heal classification and visual verification are rare and hard.

## Options
1. **Single provider (Claude)** — simplest; vendor lock-in.
2. **Model-agnostic with per-role routing** — thin routing layer; any provider per role via config.
3. **Self-hosted open-weight models** — no token cost; GPU ops; weaker on agentic browser tasks.

## Decision
Option 2. Roles: `navigator`, `verifier`, `healer`, `vision_fallback`. The chat-model layer is LangChain's provider packages (native to LangGraph for tool binding and structured output); OpenRouter is reached through its OpenAI-compatible API. Per-call cost is computed from LiteLLM's model price map. Defaults ship configured for Claude (Haiku 4.5 navigator; Sonnet 5 verifier/healer/vision fallback), all overridable.

Each role declares required capabilities (tool calling, structured output, vision); config validation rejects models lacking them at load time.

## Consequences
- The benchmark publishes a per-role model comparison — evidence that routing saves cost without losing accuracy.
- Provider quirks (tool-call formats, structured-output support) must be covered by cassette tests per supported provider.
- Prices are never hard-coded in logic; published cost figures are re-measured per release.

## Amendment (2026-09-29): a pinned price map, Sonnet 5.5 defaults and no forced tool choice

**Prices come from a vendored copy of LiteLLM's `model_prices_and_context_window.json`, not the `litellm` package.**
- *How it's kept:* the copy is pinned to an upstream commit, with its sha256, and refreshed by a script in a reviewed pull request. Each cost record cites the version (`llm_calls.price_map_version`, DATA_MODEL §2), so a published cost figure names the prices it used.
- *Why not the package:* LiteLLM's PyPI releases 1.82.7 and 1.82.8 were malicious (2026-03-24); 1.82.8 ran at interpreter start through a `.pth` file. The runner holds customers' provider keys, and we need one data file, not the package.

**Capability validation reads the same file** (`supports_function_calling`, `supports_vision`, `supports_response_schema`). A model missing from it needs explicit capabilities and prices in the project config (`models`, DATA_MODEL §9), or it is rejected.

**Every role defaults to Claude Sonnet 5.5** (`claude-sonnet-5-5`, $2 input / $10 output per million tokens). This covers the navigator, verifier, healer and vision fallback.
- *Navigator:* moves up from Haiku 4.5. Exploring runs once per spec version, and the compiled script's quality decides every later result.
- *Other roles:* move from Sonnet 5, at the same price.
- *Benchmark:* M3's per-role ablation tests Haiku 4.5 and Opus 5.5 on the dev split.

**Requests never force a tool choice.** Sonnet 5.5 and Opus 5.5 reject `tool_choice` set to `any` or `tool` with a 400.
- Tools use `auto` with strict schemas.
- The coverage plan (ADR-0024) uses structured output.
- A structured-output helper can force a tool behind the scenes, so cassette tests check the pinned adapter's actual wire requests.

**Refusals** (`stop_reason: "refusal"`) are recorded as their own outcome. The role's configured fallback model is used if there is one.

"The live price map" in AGENTS.md §6 and TECH_STACK now means this pinned copy.

## Amendment (2026-10-01): where the pinned price map lives, how it is checked and how it is refreshed

#40 builds the map's home in `aqa_core` (`price_map.py`, `price_map_refresh.py` and `price_map_data/`), ahead of the role validation and the router that read it. Each choice had real alternatives.

**The copy is verbatim, and in `aqa_core`.**
- *Verbatim, not trimmed to the providers we support:* a trimmed file could not be compared with upstream. As it stands, `git show <commit>:model_prices_and_context_window.json | sha256sum` in LiteLLM's repository, or `curl -s https://raw.githubusercontent.com/BerriAI/litellm/<commit>/model_prices_and_context_window.json | shasum -a 256`, must print the sha256 in `pin.json`. The cost is a 3.0 MB file (79,607 lines) in git. It compresses to about 150 KB (`gzip -c … | wc -c`), and git stores each refresh as a delta.
- *Byte for byte:* `.gitattributes` marks the map `-text`. Without it, a checkout with `core.autocrlf=true` converts all 79,607 line endings to CRLF, the file's sha256 changes, and the load check calls an untouched map hand-edited (measured with `git -c core.autocrlf=true cat-file --filters HEAD:<path>`).
- *In core, not the runner:* the map is data with no LangChain dependency, and config load (`aqa_core`, DATA_MODEL §9) has to read it to validate roles. The API and the dashboard can price a call later without importing the runner.

**The pin is `pin.json`: the upstream repository, a commit and the sha256 of the copy.** The file's name is a constant in the code, so the pin names no path.

**`load_price_map` checks the sha256 each time the map loads.** A map whose hash differs from the pin's, such as one with a hand-edited price, is rejected with an error that names both hashes and the pinned commit. A pin that is malformed (a short or padded commit or hash, another repository, an unknown key, or bytes that aren't UTF-8), a missing file, and a file that isn't a JSON object of objects, are rejected with errors of their own. Every one is a `PriceMapError`. Prices load as exact `Decimal`s, so cost math is exact (LiteLLM writes them as floats such as `2e-06`), and the file's `sample_spec` entry, which documents the keys, is not a model.
- *What this does not prove:* the commit's content, or a map and a pin edited together with a recomputed hash, which load fine. The check catches an edit made without the script. The reviewed pull request that changes the pair is where a person checks the diff and the commit.

**The refresh is `uv run python -m aqa_core.price_map_refresh [ref]`.**
- It resolves `ref` (default `main`; a commit, branch or tag without a slash) to a commit through GitHub's API, downloads that commit's file over HTTPS from `raw.githubusercontent.com`, and pins the commit with the download's sha256. The commit is the one `ref` named at that moment, not the last commit that touched the file; the sha256 is what ties the content.
- Nothing is written unless the download is a JSON object of objects (`parse_models`, the parse the load uses), so a bad download leaves the old pair in place.
- *What it does not check:* that the commit is on upstream's default branch. GitHub's API resolves a commit ID from a fork of the repository under the upstream's name, so the reviewer of the pull request checks the pinned commit, as they check the diff. A failed write between the two files leaves a map that doesn't match its pin, which the next load rejects; running the refresh again repairs it.
- *Considered and left for later:* a scheduled CI job that opens refresh pull requests. It needs a workflow and a token, and a refresh is a deliberate event today.
