"""The run viewer `aqa report` writes (UX_SPEC §3.5 and §6; API.md §7; #249): one
HTML document holding the run's header, its step timeline, four tabs of the
selected step's evidence and the verdict panel, with no Screenshot tab yet.

Every text from the record, the page, the model or the spec is escaped as the
CLI's output is (`common._visible`), then by `html.escape`, line by line where it
spans lines, and is placed only in element content: never in the script, the
style or an attribute. The only attribute values the record decides are the
paths `report_record` validated, and integers. Page text and model text are
shown inside elements that say whose words they are. The style and the script
are fixed text, each allowed by its hash, and the page's CSP lets nothing else
load, so the file works offline and a hostile record stays text."""

import base64
import hashlib
import html
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from aqa_core.compiled import CompiledScript

from aqa_cli.common import _visible
from aqa_cli.report_record import (
    Cited,
    ConsoleEntry,
    Gap,
    Log,
    Record,
    Request,
    Verdict,
    compiled_action,
    console_log,
    heal_diff,
    network_log,
    repair_script,
    snapshot,
)

# Dark first; the light theme follows the OS (UX_SPEC §1). System fonts only, no
# `url(` or `@import`, no transition or animation (UX_SPEC §7).
STYLE: Final = """
:root {
  color-scheme: dark;
  --bg: #0f1115; --panel: #171b22; --line: #3a414d; --text: #e6e9ef;
  --muted: #a3acba; --link: #8cc4ff; --focus: #8cc4ff;
  --passed: #5bd17e; --violated: #ff8f85; --drift: #c8a6ff;
  --inconclusive: #efc25e; --errored: #dcaaaa;
}
@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #ffffff; --panel: #f4f6f8; --line: #b9c1cc; --text: #1c2128;
    --muted: #525b66; --link: #0a58c2; --focus: #0a58c2;
    --passed: #16702f; --violated: #b3261e; --drift: #6b3dc4;
    --inconclusive: #865100; --errored: #874444;
  }
}
* { box-sizing: border-box; }
html {
  background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
body { margin: 0 auto; max-width: 90rem; padding: 1rem; overflow-wrap: anywhere; }
code, pre, .badge {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 0.8125rem;
}
pre { margin: 0; white-space: pre-wrap; }
h1 { font-size: 1.375rem; line-height: 1.3; margin: 0.25rem 0 1rem; }
h2 { font-size: 1.0625rem; margin: 1.75rem 0 0.5rem; }
h3 { font-size: 0.9375rem; margin: 1.25rem 0 0.375rem; }
p, ul, ol, dl { margin: 0.375rem 0; }
ul, ol { padding-left: 1.25rem; }
a { color: var(--link); }
:focus { outline: 2px solid var(--focus); outline-offset: 2px; }
[hidden] { display: none !important; }
.eyebrow, .muted, .gap, figcaption, .facts dt { color: var(--muted); }
.eyebrow { margin: 0; font-size: 0.8125rem; }
.gap { font-style: italic; }
.facts {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
  gap: 0.75rem 1.5rem;
}
.facts div { min-width: 0; }
.facts dt { font-size: 0.8125rem; }
.facts dd { margin: 0.125rem 0 0; }
.chip {
  display: inline-flex; align-items: center; gap: 0.375rem;
  padding: 0 0.625rem; border: 1px solid currentColor; border-radius: 999px;
  font-weight: 600;
}
.badge { display: inline-block; padding: 0 0.375rem; border: 1px solid var(--line); border-radius: 4px; }
.s-passed { color: var(--passed); }
.s-violated { color: var(--violated); }
.s-drift { color: var(--drift); }
.s-inconclusive { color: var(--inconclusive); }
.s-errored { color: var(--errored); }
.notice {
  margin: 1rem 0 0; padding: 0.5rem 0.75rem; background: var(--panel);
  border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 4px;
}
.timeline { display: flex; gap: 0.5rem; overflow-x: auto; padding: 0.25rem; }
.step {
  flex: 0 0 20rem; min-width: 0; display: flex; flex-direction: column; gap: 0.25rem;
  padding: 0.625rem 0.75rem; background: var(--panel); cursor: pointer;
  border: 1px solid var(--line); border-radius: 6px;
}
.step[aria-selected="true"] { border-color: var(--focus); box-shadow: inset 0 0 0 1px var(--focus); }
.step .seq { font-weight: 600; }
.tabs {
  display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 0.25rem; margin-top: 0.5rem;
  border-bottom: 1px solid var(--line);
}
[role="tab"] {
  flex: none; min-height: 2.25rem; padding: 0.375rem 0.875rem; font: inherit; color: var(--text);
  background: transparent; border: 1px solid transparent; border-bottom: 0;
  border-radius: 6px 6px 0 0; cursor: pointer;
}
[role="tab"][aria-selected="true"] { background: var(--panel); border-color: var(--line); font-weight: 600; }
/* Inside the strip's scroll box, so the outline is drawn within the tab. */
[role="tab"]:focus { outline-offset: -2px; }
[role="tabpanel"] {
  padding: 0.75rem; background: var(--panel);
  border: 1px solid var(--line); border-top: 0; border-radius: 0 0 6px 6px;
}
.snapshot, .diff { padding: 0.5rem; background: var(--bg); border: 1px solid var(--line); border-radius: 4px; }
.snapshot { max-height: 28rem; overflow: auto; }
figure { margin: 0.5rem 0; }
figcaption { font-size: 0.8125rem; margin-bottom: 0.25rem; }
blockquote { margin: 0; padding: 0.25rem 0.75rem; border-left: 3px solid var(--line); }
.entries { list-style: none; padding: 0; }
.entries > li { padding: 0.375rem 0; border-bottom: 1px solid var(--line); }
.diff .add { color: var(--passed); }
.diff .del { color: var(--violated); }
.diff .head { color: var(--muted); }
.links a { display: inline-block; min-height: 1.5rem; line-height: 1.5rem; margin-right: 0.75rem; }
@media (max-width: 767.98px) {
  .timeline { flex-direction: column; overflow-x: visible; }
  .step { flex: none; }
  .tabs { gap: 0.125rem; }
  [role="tab"] { padding: 0.375rem 0.5rem; font-size: 0.875rem; }
}
"""

# The timeline is a listbox, its selection following focus, and the evidence a
# WAI-ARIA tabs widget with automatic activation. It reads nothing but the page's
# own roles and the fixed `data-step` integers.
SCRIPT: Final = """
"use strict";
(() => {
  const steps = [...document.querySelectorAll('[role="option"]')];
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (index) => {
    steps.forEach((step, at) => {
      step.setAttribute("aria-selected", at === index ? "true" : "false");
      step.tabIndex = at === index ? 0 : -1;
    });
    document.querySelectorAll("[data-step]").forEach((block) => {
      block.hidden = block.dataset.step !== String(index);
    });
  };
  const open = (index) => {
    tabs.forEach((tab, at) => {
      tab.setAttribute("aria-selected", at === index ? "true" : "false");
      tab.tabIndex = at === index ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = at !== index;
    });
  };
  const target = (key, index, count, moves) => {
    if (!Object.hasOwn(moves, key)) {
      return null;
    }
    return moves[key](index, count);
  };
  const along = {
    ArrowRight: (i, n) => Math.min(i + 1, n - 1),
    ArrowDown: (i, n) => Math.min(i + 1, n - 1),
    ArrowLeft: (i) => Math.max(i - 1, 0),
    ArrowUp: (i) => Math.max(i - 1, 0),
    Home: () => 0,
    End: (i, n) => n - 1,
  };
  const around = {
    ArrowRight: (i, n) => (i + 1) % n,
    ArrowLeft: (i, n) => (i + n - 1) % n,
    Home: () => 0,
    End: (i, n) => n - 1,
  };
  steps.forEach((step, index) => {
    step.addEventListener("click", () => select(index));
    step.addEventListener("keydown", (event) => {
      const next = target(event.key, index, steps.length, along);
      if (next !== null) {
        event.preventDefault();
        select(next);
        steps[next].focus();
      }
    });
  });
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => open(index));
    tab.addEventListener("keydown", (event) => {
      const next = target(event.key, index, tabs.length, around);
      if (next !== null) {
        event.preventDefault();
        open(next);
        tabs[next].focus();
      }
    });
  });
})();
"""


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


CSP: Final = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; "
    f"style-src 'sha256-{_sha256(STYLE)}'; script-src 'sha256-{_sha256(SCRIPT)}'"
)

# A status's class, icon and label (UX_SPEC §1): never colour alone.
type Look = tuple[str, str, str]
_OUTCOMES: Final[dict[str, Look]] = {
    "passed": ("passed", "✓", "passed"),
    "drift_consistent": ("drift", "⇄", "heal proposed: UI drift, expectations hold"),
    "expectation_violated": ("violated", "✗", "expectation violated"),
    "inconclusive": ("inconclusive", "?", "inconclusive"),
}
_VERDICT_LOOKS: Final[dict[str, Look]] = {
    "pass": ("passed", "✓", "passed"),
    "drift_consistent": ("drift", "⇄", "UI drift, expectations hold"),
    "expectation_violated": ("violated", "✗", "expectation violated"),
    "inconclusive": ("inconclusive", "?", "inconclusive"),
}
_ERRORED: Final[Look] = ("errored", "⊘", "errored")
_UNKNOWN_VERDICT: Final[Look] = ("errored", "⊘", "verdict couldn't be read")
# What each mode does, in one line (UX_SPEC §3.5's mode badge).
_MODES: Final = {
    "strict": "replays the committed script with no model call and no heal",
    "run": (
        "replays with every locator; a drift goes to the healer, whose rebinding "
        "is a proposal for you to accept"
    ),
}
_PRIVATE: Final = (
    "<strong>Private.</strong> This report is part of the private run record "
    "under <code>.aqa/runs/</code> and may hold page text: keep it out of "
    "commits, issues and chats."
)
_TABS: Final = (
    ("a11y", "Accessibility"),
    ("network", "Network"),
    ("console", "Console"),
    ("reasoning", "Agent reasoning"),
)
_UNCALIBRATED: Final = "model-reported, uncalibrated"


def _e(text: str) -> str:
    """`text` as element content: escaped as the CLI escapes what it prints,
    then for HTML."""
    return html.escape(_visible(text), quote=True)


def _lines(text: str) -> str:
    """`text`, escaped line by line, its line breaks kept."""
    return "\n".join(_e(line) for line in text.split("\n"))


def _chip(look: Look, *more: str) -> str:
    kind, icon, label = look
    said = " ".join((_e(label), *map(_e, more)))
    return (
        f'<span class="chip s-{kind}"><span aria-hidden="true">{icon}</span> '
        f"{said}</span>"
    )


def _gap(words: str) -> str:
    return f'<span class="gap">{_e(words)}</span>'


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _script(record: Record) -> CompiledScript | Gap:
    return record.kept if isinstance(record.kept, Gap) else record.kept.script


@dataclass(frozen=True)
class _Step:
    """One timeline entry: its verdict, its action as JSON, the target it acted
    on and that target's meaning, and the locator used, as JSON; or the state
    each shows instead."""

    verdict: Verdict
    action: str | Gap
    target: str | None
    meaning: str | None
    locator: str | Gap | None


def _ran_with(record: Record, verdict: Verdict) -> CompiledScript | Gap:
    """The script step `verdict` ran with: the one its heal's repair offered,
    or else `run.json`'s."""
    heal = verdict.heal
    if heal is None or heal.repair is None:
        return _script(record)
    if heal.repair_path is None:
        return Gap.BROKEN
    offered = repair_script(record.run, heal.repair_path)
    return _script(record) if offered is None else offered


def _locator(
    script: CompiledScript | Gap, target: str | None, index: int | None
) -> str | Gap | None:
    if target is None or index is None:
        return None
    if isinstance(script, Gap):
        return script
    found = script.targets.get(target)
    if found is None or not 0 <= index < len(found.locators):
        return None
    return json.dumps(
        found.locators[index].model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
    )


def _step(record: Record, verdict: Verdict) -> _Step:
    seq = verdict.seq if verdict.seq is not None else -1
    script = _script(record)
    intent = record.intents.get(seq)
    compiled = None if isinstance(script, Gap) else compiled_action(script, seq)
    action: str | Gap = Gap.ABSENT
    target = None
    if intent is not None:
        action, target = _json(intent.action), intent.target
    elif compiled is not None:
        given = compiled.get("target")
        action, target = _json(compiled), given if isinstance(given, str) else None
    meaning = None
    if target is not None and not isinstance(script, Gap) and target in script.targets:
        meaning = script.targets[target].semantic
    # With no `run.json`, the locator used is not recorded, as for a record made
    # before #249, whatever a repair's file holds.
    locator = (
        Gap.ABSENT
        if isinstance(script, Gap)
        else _locator(_ran_with(record, verdict), target, verdict.locator_index)
    )
    return _Step(verdict, action, target, meaning, locator)


def _verdict_look(verdict: Verdict) -> Look:
    return _VERDICT_LOOKS.get(verdict.verdict or "", _UNKNOWN_VERDICT)


def _subject(verdict: Verdict) -> str:
    if verdict.kind is None or verdict.name is None:
        return "An entry that couldn't be read"
    return f"{verdict.kind.capitalize()} {verdict.name}"


def _why(verdict: Verdict) -> str:
    """A verdict's reason and misses, as list items' content."""
    parts = []
    if verdict.reason is not None:
        parts.append(f'<span class="reason">{_e(verdict.reason)}</span>')
    if verdict.misses:
        parts.append(
            f'<span class="muted">Misses: {_e(", ".join(verdict.misses))}</span>'
        )
    return " ".join(parts)


def _code_or_gap(value: str | Gap | None, none: str) -> str:
    if isinstance(value, Gap):
        return _gap(value)
    if value is None:
        return _gap(none)
    return f"<code>{_e(value)}</code>"


def _entry(index: int, step: _Step, selected: bool) -> str:
    verdict = step.verdict
    target = _gap("none")
    if step.target is not None:
        meaning = "" if step.meaning is None else f", {_e(step.meaning)}"
        target = f"<code>{_e(step.target)}</code>{meaning}"
    why = _why(verdict)
    return (
        f'<div class="step" role="option" id="step-{index}" '
        f'aria-selected="{"true" if selected else "false"}" '
        f'tabindex="{0 if selected else -1}">'
        f'<div class="seq">Step {verdict.seq}</div>'
        f"<div>{_chip(_verdict_look(verdict))}</div>"
        f"<div>Action: {_code_or_gap(step.action, 'not recorded')}</div>"
        f"<div>Target: {target}</div>"
        f"<div>Locator: {_code_or_gap(step.locator, 'none')}</div>"
        f"{f'<div>{why}</div>' if why else ''}"
        "</div>"
    )


def _timeline(steps: Sequence[_Step], chosen: int) -> str:
    if not steps:
        return (
            '<section aria-labelledby="steps-title"><h2 id="steps-title">Steps</h2>'
            f"<p>{_gap('No step has a verdict in this record.')}</p></section>"
        )
    entries = "".join(_entry(i, step, i == chosen) for i, step in enumerate(steps))
    return (
        '<section aria-labelledby="steps-title"><h2 id="steps-title">Steps</h2>'
        '<p class="muted" id="steps-hint">Select a step to see its evidence. '
        "&larr; and &rarr; move between steps.</p>"
        '<div class="timeline" role="listbox" aria-labelledby="steps-title" '
        f'aria-describedby="steps-hint">{entries}</div></section>'
    )


def _snapshot_block(record: Record, verdict: Verdict) -> str:
    found = snapshot(record.run, verdict.evidence_path)
    if found is Gap.ABSENT:
        return f"<p>{_gap('Not recorded for this step.')}</p>"
    if isinstance(found, Gap):
        return f"<p>{_gap(f'The snapshot {found}.')}</p>"
    return (
        "<figure><figcaption>The page's accessibility tree after this step, in "
        "the page's own words</figcaption>"
        '<pre class="snapshot" tabindex="0" role="region" '
        f'aria-label="Accessibility snapshot">{_lines(found)}</pre></figure>'
    )


_WITHHELD_NETWORK: Final = (
    "The URLs and methods were withheld, because the run bound a test secret."
)
_WITHHELD_CONSOLE: Final = (
    "The console text was withheld, because the run bound a test secret."
)


def _kept_of[T](log: Log[T], noun: str) -> str:
    kept = len(log.entries)
    if log.total is None or log.total <= kept:
        return ""
    return f"<p>{_e(f'Showing the first {kept} of {log.total} {noun}: the log keeps no more.')}</p>"


def _status(status: int | None) -> str:
    if status is None:
        return _gap("no response")
    said = f"<code>{status}</code>"
    if 500 <= status <= 599:
        said += (
            ' <strong class="s-violated"><span aria-hidden="true">!</span> '
            "server error (5xx)</strong>"
        )
    return said


def _request(entry: Request | None, withheld: bool) -> str:
    if entry is None:
        return f"<li>{_gap("This entry couldn't be read.")}</li>"
    if withheld:
        return f"<li>Status {_status(entry.status)}</li>"
    method = _code_or_gap(entry.method, "no method")
    url = _code_or_gap(entry.url, "no URL")
    return f"<li>{method} {url} &middot; status {_status(entry.status)}</li>"


def _network_block(record: Record, verdict: Verdict) -> str:
    log = network_log(record.run, verdict.evidence_path)
    if isinstance(log, Gap) and log is not Gap.ABSENT:
        return f"<p>{_gap(f'The network log {log}.')}</p>"
    withheld = f"<p>{_e(_WITHHELD_NETWORK)}</p>" if record.withheld else ""
    if isinstance(log, Gap) or not log.entries:
        return f"{withheld}<p>{_gap('No requests.')}</p>"
    rows = "".join(_request(entry, record.withheld) for entry in log.entries)
    return (
        f"{withheld}{_kept_of(log, 'requests')}"
        "<figure><figcaption>The requests the page made during this step, in "
        f'the page\'s own words</figcaption><ul class="entries">{rows}</ul></figure>'
    )


def _message(entry: ConsoleEntry | None, withheld: bool) -> str:
    if entry is None:
        return f"<li>{_gap("This entry couldn't be read.")}</li>"
    kind = f"<code>{_e(entry.type)}</code>"
    if withheld or entry.text is None:
        return f"<li>{kind}</li>"
    return f"<li>{kind} {_e(entry.text)}</li>"


def _console_block(record: Record, verdict: Verdict) -> str:
    log = console_log(record.run, verdict.evidence_path)
    if isinstance(log, Gap) and log is not Gap.ABSENT:
        return f"<p>{_gap(f'The console log {log}.')}</p>"
    withheld = f"<p>{_e(_WITHHELD_CONSOLE)}</p>" if record.withheld else ""
    if isinstance(log, Gap) or not log.entries:
        return f"{withheld}<p>{_gap('No console messages.')}</p>"
    rows = "".join(_message(entry, record.withheld) for entry in log.entries)
    return (
        f"{withheld}{_kept_of(log, 'messages')}"
        "<figure><figcaption>The page's console messages during this step, in "
        f'its own words</figcaption><ul class="entries">{rows}</ul></figure>'
    )


def _confidence(confidence: float | None) -> str:
    if confidence is None:
        return _gap("no confidence reported")
    return f"{_e(f'{confidence:g}')} ({_UNCALIBRATED})"


def _reasoning_block(record: Record, verdict: Verdict) -> str:
    heal = verdict.heal
    if heal is None:
        said = (
            "Strict replay calls no model."
            if record.mode == "strict"
            else "No model ran for this step."
        )
        return f"<p>{_e(said)}</p>"
    bound = (
        _gap("The heal bound no element for this step.")
        if heal.bound is None
        else f"Bound to <q>{_e(heal.bound)}</q>, the page's own role and name."
    )
    rationale = (
        f"<p>{_gap('No rationale reported.')}</p>"
        if heal.rationale is None
        else "<figure><figcaption>The model's rationale, in its own words "
        f"({_UNCALIBRATED})</figcaption><blockquote>{_lines(heal.rationale)}"
        "</blockquote></figure>"
    )
    return f"<p>{bound}</p><p>Confidence: {_confidence(heal.confidence)}</p>{rationale}"


def _tabs(record: Record, steps: Sequence[_Step], chosen: int) -> str:
    if not steps:
        return ""
    blocks = (
        ("a11y", _snapshot_block),
        ("network", _network_block),
        ("console", _console_block),
        ("reasoning", _reasoning_block),
    )
    tabs = "".join(
        f'<button type="button" role="tab" id="tab-{key}" aria-controls="panel-{key}" '
        f'aria-selected="{"true" if i == 0 else "false"}" tabindex="{0 if i == 0 else -1}">'
        f"{label}</button>"
        for i, (key, label) in enumerate(_TABS)
    )
    panels = "".join(
        f'<div role="tabpanel" id="panel-{key}" aria-labelledby="tab-{key}" '
        f'tabindex="0"{"" if i == 0 else " hidden"}>'
        + "".join(
            f'<div data-step="{n}"{"" if n == chosen else " hidden"}>'
            f"{block(record, step.verdict)}</div>"
            for n, step in enumerate(steps)
        )
        + "</div>"
        for i, (key, block) in enumerate(blocks)
    )
    # Which step the panels show, one label per step, shown as the script
    # shows that step's blocks; a live region, so the change is announced.
    labels = "".join(
        f'<p class="for-step" data-step="{n}"{"" if n == chosen else " hidden"}>'
        f"Evidence of step {step.verdict.seq} {_chip(_verdict_look(step.verdict))}</p>"
        for n, step in enumerate(steps)
    )
    return (
        '<section aria-labelledby="evidence-title">'
        '<h2 id="evidence-title">Evidence for the selected step</h2>'
        f'<div aria-live="polite">{labels}</div>'
        f'<div class="tabs" role="tablist" aria-labelledby="evidence-title">{tabs}</div>'
        f"{panels}</section>"
    )


def _links(cited: Cited) -> str:
    if cited is None:
        return ""
    if not cited:
        return _gap("evidence not found")
    return " ".join(
        f'<a href="{html.escape(path)}">{html.escape(path)}</a>' for path in cited
    )


def _evidence_links(verdict: Verdict) -> str:
    heal = verdict.heal
    links = [
        _links(verdict.evidence),
        *(() if heal is None else (_links(heal.repair), _links(heal.record))),
    ]
    shown = " ".join(link for link in links if link)
    return f'<span class="links">Evidence: {shown}</span>' if shown else ""


def _unpassed(verdict: Verdict) -> str:
    finding = ""
    if verdict.finding is not None:
        category = _code_or_gap(verdict.finding.category, "no category")
        title = _e(verdict.finding.title) if verdict.finding.title else _gap("no title")
        finding = f"<p>Finding: {category} {title}</p>"
    why = _why(verdict)
    return (
        f"<li><p><strong>{_e(_subject(verdict))}</strong> "
        f"{_chip(_verdict_look(verdict))}</p>"
        f"{f'<p>{why}</p>' if why else ''}{finding}"
        f"<p>{_evidence_links(verdict)}</p></li>"
    )


def _expectation(record: Record, assertion: str) -> str:
    if isinstance(record.kept, Gap):
        return _gap(record.kept)
    for check in record.kept.script.assertions:
        if check.id == assertion and check.expect_index < len(record.kept.expectations):
            return _e(record.kept.expectations[check.expect_index])
    return _gap("not recorded")


def _passing(record: Record, kinds: Collection[str | None]) -> str:
    rows = [
        f"<li><code>{_e(v.name or '')}</code>: "
        + (_expectation(record, v.name or "") if v.kind == "assertion" else "held")
        + "</li>"
        for v in record.verdicts
        if v.kind in kinds and v.verdict in ("pass", "drift_consistent")
    ]
    return f"<ul>{''.join(rows)}</ul>" if rows else f"<p>{_gap('none')}</p>"


def _bindings(record: Record) -> str:
    rows = [
        f"<li><strong>{_e(_subject(v))}</strong>: bound to <q>{_e(v.heal.bound)}</q>, "
        f"the page's own role and name; confidence {_confidence(v.heal.confidence)}</li>"
        for v in record.verdicts
        if v.heal is not None and v.heal.bound is not None
    ]
    return f"<ul>{''.join(rows)}</ul>" if rows else f"<p>{_gap('none')}</p>"


def _diff(found: tuple[str, ...] | Gap) -> str:
    if isinstance(found, Gap):
        words = (
            "The heal diff: not recorded."
            if found is Gap.ABSENT
            else f"{found.capitalize()}, so no diff is shown."
        )
        return f"<p>{_gap(words)}</p>"
    rows = []
    for line in found:
        kind = (
            "head"
            if line.startswith(("---", "+++", "@@"))
            else {"+": "add", "-": "del"}.get(line[:1], "")
        )
        rows.append(f'<span class="{kind}">{_e(line)}</span>' if kind else _e(line))
    return (
        "<figure><figcaption>The proposal as a diff of the compiled script</figcaption>"
        f'<pre class="diff">{chr(10).join(rows)}</pre></figure>'
    )


def _accept(held: str) -> str:
    """The command that writes the proposal `held` names (API.md §7), for one
    `heal apply` would take."""
    return (
        f"<p>To accept it, run <code>aqa heal apply {_e(held)}</code> in this "
        "project: it writes the proposal to the compiled script and commits "
        "nothing, so review <code>git diff</code>, then commit it.</p>"
    )


def _healed(record: Record) -> str:
    """What a run that healed adds to the panel: the assertions that passed
    after its repair, what it bound, and its proposal: the diff and the command
    that accepts it, or why neither is shown. `heal apply` reads the proposal,
    not `run.json`, so a record without one still names the command."""
    proposal = record.proposal
    held = proposal.get("id") if isinstance(proposal, dict) else None
    named = f" <code>{_e(held)}</code>" if isinstance(held, str) else ""
    found = heal_diff(record)
    shown = "" if found is None else _diff(found)
    if isinstance(held, str) and found is not None and found is not Gap.NOT_ONE:
        shown += _accept(held)
    return (
        "<h3>Assertions that passed after the repair</h3>"
        f"{_passing(record, ('assertion',))}"
        "<h3>What the healer bound</h3>"
        f"{_bindings(record)}"
        f"<h3>Heal proposal{named}</h3>{shown}"
    )


def _classification(record: Record) -> str:
    look = _OUTCOMES.get(record.outcome)
    if look is not None:
        kind, icon, label = look
        words = label.removeprefix("heal proposed: ")
        return f"<p>Classification: {_chip((kind, icon, words))}</p>"
    reasons = "".join(
        f"<li>{_e(reason) if reason is not None else _gap("a reason that couldn't be read")}</li>"
        for reason in record.reasons
    )
    return (
        f"<p>Classification: {_chip(_ERRORED, record.outcome)}</p>"
        f"<p>The run ended <code>{_e(record.outcome)}</code>.</p>"
        f"{f'<ul>{reasons}</ul>' if reasons else ''}"
    )


def _panel(record: Record) -> str:
    unpassed = [v for v in record.verdicts if v.verdict != "pass"]
    listed = (
        f"<h3>Verdicts that didn't pass</h3><ul>{''.join(map(_unpassed, unpassed))}</ul>"
        if unpassed
        else ""
    )
    passed = (
        "<h3>What passed</h3>" + _passing(record, ("assertion", "invariant"))
        if record.outcome == "passed"
        else ""
    )
    healed = _healed(record) if record.proposal is not None else ""
    return (
        '<section aria-labelledby="verdict-title">'
        '<h2 id="verdict-title">Why aqa decided this</h2>'
        f"{_classification(record)}{listed}{healed}{passed}</section>"
    )


def _cost(cost: Decimal | Gap) -> str:
    return _gap(cost) if isinstance(cost, Gap) else _e(f"${cost.normalize():f}")


def _models(record: Record) -> str:
    if isinstance(record.kept, Gap):
        return _gap(record.kept)
    healer = record.kept.models.healer
    if healer is not None:
        fallback = healer.fallback or "none"
        effort = healer.effort or "the provider's default"
        return _e(
            f"healer: {healer.provider} {healer.model}, effort {effort}, "
            f"fallback {fallback}"
        )
    if record.mode == "strict":
        return _e("none: strict replay calls no model")
    return _gap("no model recorded")


def _header(record: Record) -> str:
    kept = record.kept
    goal = _gap("Goal: not recorded") if isinstance(kept, Gap) else _e(kept.goal)
    duration = (
        _gap(kept) if isinstance(kept, Gap) else _e(f"{kept.duration_seconds:.1f}s")
    )
    expectations = (
        f"<p>{_gap(kept)}</p>"
        if isinstance(kept, Gap)
        else "<ol>"
        + "".join(f"<li>{_e(text)}</li>" for text in kept.expectations)
        + "</ol>"
    )
    look = _OUTCOMES.get(record.outcome)
    chip = _chip(look) if look is not None else _chip(_ERRORED, record.outcome)
    spec = _code_or_gap(record.spec_id, "not recorded")
    facts = (
        ("Verdict", chip),
        (
            "Mode",
            f'<span class="badge">{_e(record.mode)}</span> {_e(_MODES.get(record.mode, ""))}',
        ),
        ("Duration", duration),
        ("Cost", _cost(record.cost)),
        ("Model config", _models(record)),
    )
    listed = "".join(
        f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts
    )
    return (
        "<header>"
        f'<p class="eyebrow">aqa report &middot; spec {spec} &middot; run <code>{_e(record.run_id)}</code></p>'
        f"<h1>{goal}</h1>"
        f'<dl class="facts">{listed}</dl>'
        f"<h2>Expectations</h2>{expectations}"
        f'<p class="notice" role="note">{_PRIVATE}</p>'
        "</header>"
    )


def _chosen(steps: Sequence[_Step]) -> int:
    """The step selected as the page opens: the first that didn't pass."""
    return next((i for i, s in enumerate(steps) if s.verdict.verdict != "pass"), 0)


def page(record: Record) -> str:
    """The whole report of `record`, one HTML document."""
    steps = [_step(record, v) for v in record.verdicts if v.kind == "step"]
    chosen = _chosen(steps)
    title = _e(f"aqa report: {record.spec_id or 'a spec'}, {record.outcome}")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>{STYLE}</style></head><body>"
        f"{_header(record)}<main>{_timeline(steps, chosen)}{_tabs(record, steps, chosen)}"
        f"{_panel(record)}</main><script>{SCRIPT}</script></body></html>\n"
    )
