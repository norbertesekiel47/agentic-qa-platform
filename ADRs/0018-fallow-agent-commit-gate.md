# ADR-0018: fallow as the TypeScript codebase-intelligence gate, enforced at agent commit/push

- Status: Accepted
- Date: 2026-09-28

## Context
The dashboard (TypeScript) will be written largely by coding agents, which tend to leave unused exports and files, duplicate code, and grow complexity hotspots that `tsc` and ESLint do not report. The maintainer chose [fallow](https://github.com/fallow-rs/fallow) (MIT, Rust binary, npm `fallow`) for this: dead code, circular dependencies, duplication, complexity, and a `fallow audit` gate that fails only on findings a change introduces. knip plus jscpd is the obvious alternative; it was not evaluated separately. CI does not exist yet, so enforcement starts as a local Claude Code hook. As of this date the repository has no TypeScript and is not under git.

## Options
Trigger:
1. **fallow's official agent gate** — a PreToolUse hook on `git commit` / `git push` runs `fallow audit` and blocks on a `fail` verdict. Maintained upstream, low noise, needs git.
2. An end-of-turn (Stop) check after any turn that touched `.ts`/`.tsx` — earlier feedback and works without git, but noisy mid-refactor (a new export is "unused" until its caller exists) and a script we maintain.

Install:
1. **Manual, pinned** — copy upstream's `fallow-gate.sh` at a tagged release into `.claude/hooks/` and register it in `.claude/settings.json` by hand.
2. `fallow hooks install --target agent` — upgradable by re-running, but it also inserts a fallow-managed block into AGENTS.md (a second owner of agent rules) and rewrites settings in a way the policy guard's approval prompt cannot see.

## Decision
Trigger option 1 with install option 1: `.claude/hooks/fallow-gate.sh` is upstream's script at **v3.30.0** (sha256 of the upstream file `6a5d6067…d83ff`) minus the installer-version line, as fallow's manual-setup docs direct. The global `fallow` is 3.30.0; the gate runs the first `fallow` on `PATH`. fallow's escape routes are governed by `.claude/hooks/policy_guard.py`: `fallow-ignore*` comments and `@expected-unused` tags are refused unless the line cites an ADR, `.fallowrc*` / `fallow.toml` changes need the user's approval, and so do fallow's agent-config installers.

## Consequences
- The gate is inert until the repository is under git and contains TS/JS: `fallow audit` errors outside git and the gate fails open with a one-line notice. With no remote it compares against local `main`, so no `FALLOW_AUDIT_BASE` is needed.
- It gates only Claude Code commits and pushes. Other agents follow the AGENTS.md instruction; humans are covered once CI runs `fallow audit` (`fallow-rs/fallow@v3` Action, deferred).
- M0 vendors third-party benchmark apps with planted bugs under `bench/`, possibly the first TS/JS in the repo. Before committing them, add `bench/**` to fallow's `ignorePatterns` (a config change the user approves), or the gate will judge third-party code and the planted bugs.
- At the M7 dashboard scaffold: pin `fallow` as an exact devDependency (the Action installs the version pinned in `package.json`, keeping CI and local runs on one scanner), and generate `.fallowrc.json` with `fallow recommend` (a config change, so the user approves the initial bar). Keep the global install at the same version, because the gate prefers `PATH`.
- Upgrading fallow means re-copying the gate script from the new tag, re-verifying it, and updating this ADR's version and hash.

## Amendment (2026-09-28)
CI now runs `fallow audit` on every pull request as the required `fallow` check (`.github/workflows/ci.yml`, ADR-0019). This covers people and other agents, not only Claude Code. The Action is pinned by commit SHA (v3.30.0) and the CLI through its `version` input (3.30.0). When the dashboard pins `fallow` in `package.json` (M7), keep the two equal, or drop the `version` input so `package.json` decides. Upgrading fallow now also means updating both pins in the workflow.

The ignore for vendored benchmark apps is `bench/apps/**` rather than `bench/**`, set in `.fallowrc.json`. Only the vendored apps are third-party; the harness around them is ours (ADR-0021).
