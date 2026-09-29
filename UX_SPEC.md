# UX Spec — Agentic QA Platform

Last updated: 2026-09-27. Covers the dashboard (9 screens), GitHub surfaces, CLI output, and the local report viewer.

## 1. Design direction

- **Developer-tool aesthetic** in the family of Linear and Vercel: dense information, restrained color, monospace for code/selectors, generous keyboard support.
- **Dark theme first**, light theme supported; theme follows OS by default.
- **Status color system:** passed (green), expectation violated/bug (red), heal proposed — UI drift, expectations hold (violet), inconclusive (amber), non-resumable/errored (gray-red), running (blue), queued (gray). Never rely on color alone — every status has an icon and label.
- **Wording rule:** the UI never says a change was "intentional." It says what the evidence shows: "UI moved; all expectations still pass after updating the locator."
- **Evidence is the hero.** Screens exist to answer "what happened, and why does the agent think so?"
- **Polish budget:** Run viewer (5) and Triage inbox (6) get the highest polish — they are the public-demo path and what failing PR comments link to. Other screens are functional and clean.

## 2. Global layout

- Left sidebar: org switcher (Clerk), project switcher, nav (Runs, Triage, Specs, Usage, Settings).
- Top bar: breadcrumb, search (`/`), command palette (`⌘K`), user menu.
- Responsive: sidebar collapses to a bottom sheet under 768px; all screens usable at **390px**. Tables become stacked cards on narrow screens.

## 3. Screens

### 3.1 Onboarding (first run)
Stepper with persistent progress:
1. Create or join an organization (Clerk).
2. Install the GitHub App → choose repositories.
3. Verify a domain — show the DNS TXT value and the `/.well-known/agentic-qa.txt` alternative with copy buttons; "Check now" button; live status.
4. Add a model provider key — provider picker, paste field (write-only), capability check result per role.
5. Run your first spec — pick an example spec against the demo app or a verified domain; opens the Live run view.
Skippable steps show what is blocked until completed (e.g. "Hosted runs need a verified domain").

### 3.2 Projects
Cards/table: project name, repo, pass rate (7-day sparkline), last run status, open bugs, pending heals, LLM cost this month. Empty state explains CLI-only usage.

### 3.3 Runs list
Filterable table: status, spec, branch, PR, trigger (CI/hosted), duration, cost, started. Live status badges update via WebSocket. Row click → Run viewer (or Live run if running). Saved filters in URL query.

### 3.4 Live run
- Left: current screenshot (updates per step), with the target element outlined.
- Right: streaming step timeline + agent thoughts (collapsible), current model role, running cost.
- Footer: cancel button. (Interactive "agent needs input" interrupts are deferred to a later release — v1 runs never wait on a human mid-run.)

### 3.5 Run viewer (hero screen)
- **Header:** spec goal, verdict chip, commit/PR links, duration, cost, model config.
- **Scrubbable step timeline** (horizontal on desktop, vertical on mobile): each step shows action, locator used, outcome; keyboard `←/→` to move.
- **Main panel tabs** for the selected step:
  - *Screenshot* — before/after with a swipe comparison; password fields masked.
  - *Accessibility* — snapshot tree with diff highlighting vs the compiled expectation.
  - *Network* — requests with status; 5xx highlighted.
  - *Console* — errors/warnings.
  - *Agent reasoning* — the model's rationale for this step.
- **Verdict panel** (for heal/bug steps): "Why the agent decided this" — classification (`UI drift, expectations hold` / `expectation violated` / `inconclusive`), evidence list (each item links to the exact artifact), which assertions passed after the repair, confidence (labeled *model-reported, uncalibrated*), and for heals the proposed locator/step diff (assertions are never part of a heal diff).
- **Mode badge:** `strict` (no LLM) or `verified` (model-assisted visual checks, with their cost).
- Share link (org members) and "Open in GitHub" for the check run.

### 3.6 Triage inbox
- Two lists (tabs): **Bugs** and **Heal proposals**; counts in tabs.
- Keyboard-first: `j/k` move, `a` accept heal, `r` reject, `n` not a bug, `o` open run, `?` shortcuts help.
- Each item: spec, verdict summary, one-line rationale, thumbnail of the failing step, age.
- Actions record `triage_labels` (ground truth for evaluation); toast with undo for 5 s.
- Accepting a heal shows the resulting commit link when the GitHub App finishes.

### 3.7 Specs
Specs indexed from the repo per branch: path, goal, compiled status (`compiled` / `needs explore` / `stale`), last result. Detail view renders the spec and compiled steps side by side. **Edit** opens an editor with schema validation; **Save** opens a PR (never writes directly to the DB).

### 3.8 Usage & cost
- Stacked bar: daily LLM spend by model role; filters for project and model.
- KPI tiles: replay-vs-heal ratio, cost per heal (median, p90), runs this month, share of runs with $0 LLM cost.
- Table: top specs by cost.

### 3.9 Settings
Tabs: Members & roles (Clerk components), Domains, Provider keys (last4 only, rotate/delete), API keys (create shows secret once, prefix displayed afterward), GitHub installation, Data retention, Audit log.

## 4. GitHub surfaces

### Check runs
One check run per spec (`agentic-qa / <spec_id>`) plus a summary check. This keeps each heal within GitHub's limits (≤ 3 actions per check run; action label and identifier ≤ 20 characters).

**Summary check (Markdown, placeholder values):**
```
Agentic QA — 5 specs · 3 passed · 1 heal proposed · 1 expectation violated
| Spec                    | Result                              | Time | LLM cost |
|-------------------------|-------------------------------------|------|----------|
| checkout-happy-path     | ✅ passed (strict replay)           | 12s  | $0.00    |
| checkout-expired-card   | ❌ expectation violated: 500 on pay | 18s  | $0.02    |
| signup                  | 🟣 heal proposed                    | 21s  | $0.03    |
```

**Spec check with a heal** (`conclusion: action_required`): output explains *"Step 4: 'Create account' moved into a modal. After updating the locator, all 3 expectations and all invariants pass (evidence: 3 items)."* One action button — **Accept heal** (description "Commit the locator update"). "Open run" is a normal link (`details_url`), not an action. If the PR head changed since the proposal, clicking returns a *"Proposal is stale — re-running"* result. Fork PRs show the patch and `aqa heal apply <id>` instead of the button.

## 5. CLI output

- Human mode: one line per spec with status icon, time, cost; heal proposals printed as unified diffs; final summary; `aqa report` hint.
- `--json` mode for scripting; `--quiet` for CI logs.
- Errors are actionable ("Domain `staging.acme.dev` is not verified for hosted runs — run in CI or verify at <link>").

## 6. Local report viewer

`aqa report` opens a self-contained HTML file (no network needed) with the same layout as the run viewer (screen 3.5) — this is also what the public demo links to for runs without signing in.

## 7. Accessibility

WCAG 2.2 AA: full keyboard navigation, visible focus, ARIA-labelled timeline, reduced-motion support (no auto-playing transitions), color-independent status, alt text for screenshots derived from the step description.

## 8. States

Every data view defines: loading (skeletons), empty (explains next action), error (Problem Details `code` → human message + retry), and permission-denied (viewer role) states.

## 9. Acceptance for UI work

Per project rules: UI changes are accepted when they are **visibly different at a glance**, proven with **before/after screenshots at 390px and 1440px** attached to the PR. Interaction-only polish is not a standalone deliverable.
