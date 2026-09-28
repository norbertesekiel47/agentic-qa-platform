# ADR-0021: Vendored benchmark apps (`bench/apps/`): a narrow gate carve-out

- Status: Accepted
- Date: 2026-09-28

## Context
ADR-0020 vendors third-party apps under `bench/apps/`, and M0 plants bugs in them on purpose. Our gates are written for our own code.

Measured against the untouched upstream Conduit code, they report:
- **`policy_guard.py`:** 6 findings. There are 4 tautological assertions, 1 `@ts-expect-error`-style suppression, and 1 test JWT in the frontend's `jwt.service.spec.ts`.
- **gitleaks:** 5 findings. There are 3 generic-key matches in the backend `Makefile`, plus the same test JWT flagged twice.

fallow would also judge the planted bugs, which include deliberately dead code. Vendoring as-is turns CI red, and the end-of-turn scan would send agents to "fix" upstream code.

## Options
1. **Exempt `bench/apps/` from every gate.** Simple, but it loses secret scanning where a real key could be pasted by accident.
2. **Allowlist each finding in every gate.** Precise, but it fights the benchmark: planted bugs *are* findings, and each one would need an entry.
3. **Split by purpose.** policy_guard and fallow police the quality of *our* code, so they skip `bench/apps/`. gitleaks guards against *leaks*, which matter in any code, so it keeps scanning `bench/apps/`. Each reviewed upstream finding gets one commit-free entry in `.gitleaksignore`.

## Decision
Option 3:
- `policy_guard.py` adds `bench/apps` to `EXEMPT_DIRS`. That covers per-edit checks, the end-of-turn scan and CI's `--scan`.
- `.fallowrc.json` sets `ignorePatterns: ["bench/apps/**"]`.
- gitleaks entries use the `file:rule:line` form, without the commit. They are added in the pull request that vendors the files, one entry per finding reviewed as a non-secret.
- Only `bench/apps/` is exempt. The benchmark harness, manifests and results under `bench/` are ours and fully policed.

## Evidence (gitleaks 8.30.1, fallow 3.30.0; 2026-09-28)
- **A commit-free `.gitleaksignore` entry suppresses exactly its finding in a `gitleaks git` history scan, and still does after every commit SHA is rewritten.** This matters because commit-scoped entries would break on each rebase merge.
- **A new, realistic GitHub token elsewhere in `bench/apps/` is still reported.** gitleaks deliberately skips low-entropy strings, so a fake such as a repeated `A1b2` is *not* reported.
- **fallow ignores `bench/apps/**`.** With the config, fallow no longer reports a planted unused export there, while a control unused export in `src/` is still reported.

## Consequences
- Secrets in vendored code are caught only by gitleaks (CI's `secrets` job, and local runs). policy_guard no longer looks there.
- **An upstream update that moves a reviewed finding changes its line number, so CI fails until someone re-reviews it.** That is intended.
- policy_guard's per-edit shell check matches content, not paths. So writing suppression markers into `bench/apps/` through a shell heredoc is still refused; use the Edit/Write tools there.
- ADR-0018's "`bench/**`" becomes `bench/apps/**` (amended).
