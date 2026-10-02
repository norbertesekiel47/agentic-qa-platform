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

## Amendment (2026-10-01): how roles are routed, checked and costed

#40 builds the role table, the check at config load and the cost record in `aqa_core` (`model_roles.py`, `model_costs.py`), ahead of the router in `aqa_runner` that calls them. The router's own choices (the Anthropic adapter, refusals, tracing, cassettes) get their own amendment when it lands.

**Roles are checked in core, when the project config loads.** `load_config` resolves every role against the pinned price map and reports each role problem at once, in the `<file>: roles.<role>.<field>: <problem>` form the spec errors use. The file's own shape problems (an unknown key, a `models` entry without prices) come first, because roles are resolved from a valid file. `load_project` reports role problems beside the specs' own, and a role problem doesn't stop the specs' secret references being checked. *Alternative:* validate in the runner after `load_project` returns, which reports spec problems and role problems in two rounds, and lets an invalid role reach every caller that only reads the config.

**What each role needs** is TECH_STACK §3's table. `vision_fallback` needs tools rather than that table's earlier "coordinate actions", because a coordinate action is a tool call. A `fallback` model is held to the same needs and the same pricing rule as the role's own.

**Capabilities and prices come from one typed view of the map.** `PriceMap.models` maps a name to a read-only `ModelInfo`: capabilities from `supports_function_calling` (tools), `supports_response_schema` (structured output) and `supports_vision`, and exact per-million-token rates. A price that isn't a finite number of 0 or more, and a flag that isn't `true` or `false`, are errors that name the model and the field, because a quietly wrong price is the one failure here that nothing else would notice. Each `ModelInfo` also carries the map's `litellm_provider` and whether the map prices it in tiers.
- *Only priced models:* an entry without both token prices (an image, audio or embedding model) is not a model we could charge per token, so it is left out and counts as missing from the map.
- *The cached rate:* a map model's cached input tokens cost `cache_read_input_token_cost`, or its input rate if the entry has none. A model declared under `models` has no cache-read rate, so its cached input costs its input rate. *Alternative:* a `cached_input_usd_per_mtok` key in `models`. Left out: no one has asked for a negotiated cache rate, and the key can be added without breaking a config.
- *Tiers:* LiteLLM writes a higher rate above a token threshold as separate keys (`input_cost_per_token_above_200k_tokens`, and the same for output and cache reads, at 32k, 128k, 256k, 272k and 512k). Cost records don't apply them, any `*_above_*` key on a price field marks the model as tiered (so a new spelling fails closed), and 306 of the map's 3,663 priced models carry one (Sonnet 5.5 and Opus 5.5 don't), so a role can't use such a model *from the map*: a call over the threshold would be costed at the flat rate and understated (250,000 uncached input and 1,000 output tokens on `claude-sonnet-4-5` would record $0.765 against $1.5225 at its above-200k rates). The project declares the model under `models` with the flat rates it wants recorded, as it would a model the map lacks. *Alternative:* price the tiers, which needs thresholds in `ModelInfo`, a per-call selection of the rates, and the applied tier in `applied_prices`. Later, if a tiered model is wanted.
- *Cache creation:* M1 sends no `cache_control`, so no call writes to the cache, and cache-creation tokens have no rate here. The first change that sends one adds the rate and the field.

**A `models` entry wins over the map.** The ticket and DATA_MODEL §9 spoke only of models the map lacks. A model in both is served from the config, and its records say `price_source: config` with the rates applied, so a negotiated rate can be stated and audited. *Alternative:* reject the redundant entry, which would stop a project from correcting a price it knows the map has wrong until the next refresh.

**Only `anthropic` has an adapter in M1.** Another `provider` is a config error that says so, rather than a role that fails at its first call, and it is that role's only problem: an adapter that doesn't exist can't say whether a model suits it. Each further provider arrives with its adapter and its wire-level cassettes (ADR-0007; TESTING §4). A role's `effort` is a closed set (DATA_MODEL §9), the levels the Anthropic adapter takes.

**A map model must belong to the role's provider.** The map's `litellm_provider` for a model is checked against the role's `provider`, for the model and the fallback alike, so `model: gpt-4o` or a Bedrock model ID under `provider: anthropic` fails at config load rather than at the first call, or, for a fallback, only once the primary refuses. A map entry that names no provider fails too, so a refresh can't slip one past the check. A model declared under `models` has no such field, so the project's word stands, and its name is just a name: the adapter takes the provider from the role's `provider`, never from a prefix in the model name.

**A cost record is exact, and no billed response goes unrecorded.**
- *Exact:* tokens times per-million rates, as `Decimal`, so a record's `cost_usd` is the sum a person would write by hand (600 uncached input tokens at $2, 400 cached at $0.20 and 200 output at $10 cost $0.00328), and a rate of 2e-07 a token, the cache-read price of Sonnet 5.5, is $0.20 a million rather than 0.19999999999999998. The sum is computed at the decimal module's maximum precision, so it never rounds: Decimal's default 28 digits would round a call of about 10^15 tokens, and even a hundred digits would run out on a declared price of 1e-120 beside an ordinary one.
- *`input_tokens` includes the cached ones,* as LangChain's `usage_metadata` reports them, so a `Usage`, and the `CostRecord` that holds it, whose cached count exceeds its input count is rejected, as are a negative count, cost or rate.
- *Status:* a response that arrived and then failed parsing or validation is `invalid`, next to `ok` and `refusal` (DATA_MODEL §2 lists the values). Each was billed, so each gets a record. A call with no response, such as a transport error, has no usage and records nothing.
- *The map's version stays on a config-priced record:* it names the map in force when the call was made, and `price_source` says which rates applied.
- *Config prices are the project's word.* A `models` entry may price a model at $0 and a record then says so, with `price_source: config`. Records priced that way feed that run's own budget and report, and anything that enforces a platform limit or bills (M4 onward) must use the map's prices or the token counts, never a config-priced `cost_usd`. Where a pull request's config is trusted for CI runs is #27.

## Amendment (2026-10-01): the router, the Anthropic adapter, tracing and cassettes

#40 builds the router in `aqa_runner` (`model_router.py`, `chat_client.py`, `anthropic_client.py`, `tracing.py`) on the roles and cost records of the amendment above. Each choice had real alternatives.

**The router is provider-neutral, and async.** `ModelRouter.call(role, mode, messages, *, tools=(), schema=None)` returns a `Routed`: the last response and what was parsed from it, an outcome (`ok`, `refusal` or `invalid`) and a `CostRecord` for every response that arrived. It talks to a `ChatClient` (one `async call` returning a `Reply`: the message, its `Usage`, whether it refused, what parsed), built by a factory the first time a model is needed and kept per model and effort. So a router that makes no call builds no client, which is the router's half of the strict-mode guarantee (TESTING §1): `strict` is no call mode, and `call` refuses it before building anything. Async because the browser session is.
- *A refusal* (`stop_reason: refusal`) is recorded as one, and the role's fallback, if it has one, is called with the same effort. A fallback that refuses too ends the call as a `refusal` with both recorded.
- *An answer that doesn't parse* against the schema is recorded as `invalid` and returned, not retried and not raised. *Alternatives:* raising after recording loses the records, since an exception carries none to the caller; retrying hides what the first response cost. The caller decides, and a fallback is only for refusals.
- *A call that gets no response* (transport error) raises and records nothing: there is no usage. A fallback that gets none, after a refusal that was billed, raises `ModelCallError`, which carries the refusal's record (an exception otherwise loses it). Retries are the SDK's two. langchain-anthropic passes no request timeout (`timeout=None` is none) and sets `max_tokens` to its per-model default (4,096 for Sonnet 5.5, 128,000 for Opus 5.5), and the router leaves both as they are: tuning them is a decision of its own, not made here.

**The Anthropic adapter is the only module that imports langchain-anthropic.** `AnthropicClient` takes the provider from `RoutedModel.provider`, never from the model's name: a model a project declares under `models` can be called `openai:gpt-4o`, and an adapter that parsed the prefix would send the call, and the key, to another provider. It refuses any provider but `anthropic`.
- *Endpoint and key* come from Anthropic's own variables (`ANTHROPIC_API_URL`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`) and are passed explicitly. Left to langchain-anthropic, an ambient `LANGSMITH_GATEWAY` would send the call, prompts and all, through LangSmith's gateway, and a LangSmith key to Anthropic when there is no provider key: the ambient-tracing hazard below by another route (LAB_NOTES, 2026-10-02). The SDK's own settings still apply (a proxy through `HTTPS_PROXY` or `ANTHROPIC_PROXY`, TLS certificate files, `ANTHROPIC_CUSTOM_HEADERS`): they shape the call to the endpoint named and are the deployment's to set.
- *Tools:* `bind_tools(tools, tool_choice="auto", strict=True)`. *Structured output:* `with_structured_output(schema, method="json_schema", include_raw=True)`, which sends `output_config.format`. langchain-anthropic 1.7.4's default (`function_calling`) forces `tool_choice: {"type": "tool"}` for `claude-sonnet-5-5`, which the pinned price map marks `supports_forced_tool_use: false` (Opus 5.5 too) and which this ADR's first amendment records as a 400; the library only knows to skip a forced tool for Opus 5.5 and Fable 5.1. A control test shows what it would send (LAB_NOTES, 2026-10-01). `include_raw` leaves `parsed` empty on a refusal or a bad answer instead of raising, so the response can be priced.
- *A call has tools or a schema, not both.* Nothing asks for both yet, and the wire shape of the two together is untested.
- *Usage:* LangChain's `usage_metadata` already counts cached input tokens in `input_tokens` (Anthropic's own field leaves them out), so `Usage.input_tokens` is that figure and `cached_input_tokens` is its `cache_read`. Cache-creation tokens count as ordinary input (M1 sends no `cache_control`). A response whose own `usage` lacks `input_tokens` or `output_tokens` raises, since it can't be priced: langchain-anthropic would report zero tokens, so the adapter checks the response's `usage` itself.
- *A cut-off answer* (`stop_reason: max_tokens`, or any stop but `end_turn`, a stop sequence included) comes back unparsed: langchain-anthropic repairs half-written JSON into a verdict that validates, and half a reason is not a verdict. It is recorded as `invalid`; the caller decides (LAB_NOTES, 2026-10-02).

**Ambient tracing is switched off by constructing a router.** `ignore_ambient_tracing()` runs `langsmith.configure(enabled=False)`, which outranks every spelling of the tracing switch, and drops the version 1 variables from the environment (why: LAB_NOTES, 2026-10-01). It is process-wide, so it covers LangGraph runs too once a router exists: a LangChain or LangGraph run must start after its router is built, and a process entry point that runs LangChain without one must call the function first (SECURITY §10). Nothing in M1 turns export on. *Alternative:* `langsmith.tracing_context(enabled=False)` around each call covers only the router's calls, and still raises on the version 1 variables. The runner now pins `langsmith` itself (`==0.14.1`, tracked in TECH_STACK §1), since it imports it.

**Cassettes are VCR.py's, matched by prompt hash.** The matcher compares a sha256 of each request's canonical JSON body, with the method and URL, so a changed prompt, schema, model or tool choice matches nothing and the test fails with the re-record command. A test also fails when a recorded response is never played. What a recording keeps, and the re-record command, are TESTING §4's.
- *The cassettes that landed with #40 are hand-written* (no provider key was available), and each file's header says so. They show what the adapter sends (no forced tool choice, strict tools, `output_config.format`, an effort, a changed prompt failing) and how it reads a response of that shape. They do not show that the API accepts the request, or that its real responses read the same way: that waits for a re-record with a key. Which cassettes a re-record replaces, and which are hand-written by design, is TESTING §4's.
- *Alternative:* a home-made replay layer over `httpx2.MockTransport`. VCR was already the choice (TECH_STACK §1, TESTING §1).

**Two quality-gate decisions** (CONSTRAINTS.md, Exceptions):
- *E-001, expires 2026-12-29:* a `[[tool.mypy.overrides]]` with `ignore_missing_imports` for `vcr` and `vcr.*`, because vcrpy ships no types and no stub package exists, and mypy `strict` refuses an untyped import. Nothing else is excused.
- *No `filterwarnings` entry.* One was approved for the `DeprecationWarning` that `import langchain_anthropic` raises under `python -W error`. It turned out to be unnecessary, and its module would not match: LAB_NOTES (2026-10-01) has the cause and the exact entry to add if a langsmith upgrade makes CI show the warning.

