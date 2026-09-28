# ADR-0005: Python backend + Next.js dashboard with a generated typed client

- Status: Accepted
- Date: 2026-09-27

## Context
This project is the flagship of a résumé that will list only the three new portfolio projects. Research (2026): Python appears in roughly 71–89% of AI-engineer postings; TypeScript overtook Python as the most-used language on GitHub (Octoverse 2025) and dominates web tooling; the closest ecosystem (Stagehand, Vercel AI SDK, Momentic) is TypeScript-first. The dashboard is TypeScript regardless.

## Options
1. **Python backend (FastAPI, LangGraph, Playwright for Python) + Next.js dashboard** — matches the language most postings require and the author's fluency; TypeScript still shown via the dashboard.
2. **TypeScript end-to-end** — best ecosystem fit for web testing; Python would appear only in the eval toolkit.
3. **Split** (Python agent, TS API) — two toolchains and duplicated schemas across the boundary.

## Decision
Option 1. The most-discussed project should be in the language most postings request and the author writes best. A single source of truth for types flows Pydantic → OpenAPI → generated TypeScript client (`@hey-api/openapi-ts`). The GitHub Action is Docker-based, so distribution language doesn't matter.

## Consequences
- Portfolio language balance: Python (this backend, Calibrated Eval Toolkit), TypeScript (this dashboard, MCP Defense Gateway).
- Client regeneration is part of the API change workflow; a CI check fails if the generated client is stale.
- Playwright for Python is used (feature parity is sufficient; trace viewer works with Python traces).
