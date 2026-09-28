# ADR-0006: LangGraph for the run graph, with an API-backed checkpointer

- Status: Accepted
- Date: 2026-09-27

## Context
The agent needs typed tools, structured verdicts, multi-provider models, tracing, retries, and a clear run lifecycle (replay → heal → verify → report). Most runs are deterministic replays; the agent wakes only to explore, heal, or verify visually.

## Options
1. **LangGraph** — explicit graph, checkpointing, interrupts; strongest résumé keyword among agent frameworks (LangGraph appears as a differentiator in postings).
2. **Pydantic AI + hand-written state machine** — lighter, typed tools/outputs, built-in multi-provider and OTel.
3. **Custom loop on LiteLLM** — maximum control; rebuilds tool schemas, validation, tracing.

## Decision
Option 1, used where its features genuinely fit:
- The run lifecycle is a LangGraph graph with a heal subgraph.
- **Checkpointing** provides durability: a runner killed at Lambda's 15-minute limit resumes from its last completed step. Because runners must not access the database (ADR-0008), checkpoints persist through a custom `BaseCheckpointSaver` that calls our API with the run token.
- **Interrupts** are used only for live dashboard runs ("agent asks a human"); CI runs never block.

## Consequences
- Custom checkpointer must implement `put`, `put_writes`, `get_tuple`, `list` against the API and be covered by integration tests.
- Checkpoint payload size must stay under API Gateway's 10 MB limit (watch list in LAB_NOTES).
- The trade-off vs option 2 (less framework weight) is accepted for durability and market signal.

## Amendment — 2026-09-27 (external design review)

- **Payload ceiling corrected:** the binding limit is Lambda's **6 MB** synchronous invocation payload, not API Gateway's 10 MB. Checkpoints are capped at 1 MB compressed; large values are artifact references in S3.
- **Full saver contract:** the API-backed saver implements the complete `BaseCheckpointSaver` interface, sync and async (`get_tuple`, `list`, `put`, `put_writes`, `delete_thread` and their `a*` variants), with pending writes stored idempotently in `checkpoint_writes` keyed by `(task_id, idx)`. Conformance is proven with test cases ported from LangGraph's upstream Postgres saver plus HTTP failure injection.
- **A checkpoint is not a browser.** Continuation restores Playwright storage state and re-executes only `replay_safe` steps; it never re-executes a `side_effect` step automatically. Specs that can't meet this end `errored: non_resumable` unless they declare a `reset` hook. Each continuation gets a new run token and lease from the dispatcher; runners never renew their own tokens.
- **Step intents (verification review):** every action writes a lease-fenced intent row before dispatch and a completion after; an unresolved side-effect intent makes the run non-resumable (a checkpoint alone can't tell whether a submit happened). `reset` restarts as a new attempt from step 1.
- **Interrupts deferred:** interactive "agent needs input" interrupts are removed from v1; runs never wait on a human mid-run. They return later with persisted interrupt records, a response endpoint, expiry, and the same side-effect-safe continuation rules.
