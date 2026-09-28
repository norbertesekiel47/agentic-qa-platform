# ADR-0019: CI on GitHub Actions with a switchable runner (Blacksmith-ready)

- Status: Accepted
- Date: 2026-09-28

## Context
The repository went public on 2026-09-28. The local Claude Code hooks (`policy_guard.py`, the fallow gate) protect only Claude sessions on the maintainer's machine. People, other agents and outside contributors bypass them, and branch protection can only require checks that exist. The maintainer wants CI on [Blacksmith](https://docs.blacksmith.sh) runners, which are faster per job and cheaper per minute than GitHub-hosted runners. Blacksmith's quickstart states it "is limited to GitHub organizations and not available for personal repositories", and this repository belongs to a personal account.

## Options
1. **Blacksmith now**: create a GitHub organization, install the Blacksmith app, transfer the repository. The URL changes (GitHub redirects the old one).
2. **GitHub-hosted only**: free and unlimited for public repositories.
3. **GitHub-hosted now, runner label from a repository variable**: `runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}`, so moving to Blacksmith is a settings change with no workflow edit.

## Decision
Option 3. `.github/workflows/ci.yml` runs three jobs:
- **guardrails**: guard tests, `ruff check` / `ruff format --check`, and `mypy --strict` on `.claude/hooks` (tool versions from TECH_STACK.md), then `policy_guard.py --scan`.
- **secrets**: gitleaks 8.30.1 (checksum-verified release binary) over the full history.
- **fallow**: `fallow audit` on pull requests (ADR-0018).

`main` requires pull requests with these three checks passing and up to date, blocks force pushes and deletion, and applies the rules to admins. Actions are hardened for a public repository: read-only token, only GitHub-owned actions plus `fallow-rs/fallow` allowed, SHA pinning required, and approval required before workflows run for any outside contributor's pull request.

## Consequences
- To switch to Blacksmith: move the repository into an organization, install the Blacksmith app there, and set `CI_RUNNER` (e.g. `blacksmith-2vcpu-ubuntu-2404`). The label must be Linux x64 because the secrets job downloads a linux_x64 gitleaks build. Pricing as of this date: 3,000 free minutes a month, then $0.004/min for 2 vCPU. GitHub-hosted minutes are free on public repositories, so on Blacksmith, outside pull requests spend paid minutes; the approval requirement keeps that deliberate.
- Applying the rules to admins means neither the maintainer's token nor an agent using it can push to `main` or merge a red pull request. It can be turned off in Settings → Branches when genuinely needed.
- M1 adds the Python gates (pytest, mypy over `packages/`, …) and M7 the dashboard gates as further jobs. Each new job must also be added to the required checks.
- No CD yet: nothing is deployable before the M4 SaaS foundation. Deploys will use GitHub OIDC to AWS (SECURITY.md).
