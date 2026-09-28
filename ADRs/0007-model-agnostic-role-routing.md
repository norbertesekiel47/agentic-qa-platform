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
