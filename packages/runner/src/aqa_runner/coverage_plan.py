"""Writing a spec's coverage plan (ADR-0024; #41): one structured call on the
navigator role, whose request holds the static instructions and the spec's
own text, and nothing from a page, a run or the machine."""

import json
import re
from dataclasses import dataclass, replace
from functools import partial
from typing import cast

from aqa_core.coverage_plan import CoveragePlan, PlannedCheck, misfits
from aqa_core.model_costs import CostRecord
from aqa_core.spec import UNHASHED_KEYS, Spec
from aqa_core.text import has_text
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from aqa_runner.model_router import ModelCallError, ModelRouter, Routed, Spend
from aqa_runner.text_search import SearchTimeoutError, pattern_matches

# The plan's system prompt. Static: a change to it changes every plan request,
# so the plan's cassettes are re-recorded or edited with it (TESTING §4).
INSTRUCTIONS = """\
You write the coverage plan for one spec of an end-to-end test of a web app. \
For each of the spec's expectations, you say which checks establish \
everything it claims, or why no check can. You see only the spec, never the \
app, so you plan from the spec's words alone.

The spec is the JSON in the user's message. Its goal is what the user does; \
steps are optional hints; preconditions.probes are read-only endpoints that \
report the app's state, by name; expect lists the expectations, numbered \
from 0 in order. Invariants and browser settings are checked apart from the \
plan: they are not expectations.

For each expectation, in order, write one entry:
- expect_index: its position in expect, from 0.
- subject: what it is about, such as "the line items in the cart summary".
- claim: everything it says about its subject: its text, state, destination \
or count.
- checks: the checks that together establish all of the claim. When no \
check can, leave checks out and write unsupported instead: a reason, and in \
needs the check that would establish it: pixel_diff (how a region looks), \
contrast_min (colour contrast) or model_verify (a judgement only a model can \
make). Leave needs out when none would.

Every check runs once, after the last step, on the final page:
- text_visible: text anywhere on the page.
- text_in_target: text inside one element, the one target_meaning names.
- not_visible: the element target_meaning names is not shown.
- url_matches: a pattern found in the page's URL.
- network_none, network_seen: no request, or a request, from the browser \
itself, by method, url_pattern and status_class (1xx to 5xx).
- probe_equals: the probe the spec declares under that name reads value, an \
integer or a string.
- probe_equals_baseline: the probe reads what it read before the run's \
actions.
- visible_unoccluded: the element target_meaning names is in the viewport, \
not tiny, and not covered by another element.

Rules:
- Never a weaker proxy. A check must establish the claim itself, not \
something that usually goes with it. Use text_visible only when the text \
could appear nowhere else on the final page; otherwise use text_in_target \
on the element the claim is about.
- A claim about a count, such as "exactly 2" or "now has 6", needs \
probe_equals on a probe the spec declares: seeing an item on the page proves \
neither how many exist nor that it was saved.
- A claim that something persisted needs the final state to show it: a \
reload condition in requires, or a probe.
- target_meaning says what the element is for and where it sits, such as \
"the payment step's submit button", never its current label or the text the \
check looks for. Use the subject when the check reads the subject itself. \
When the claim names several elements, write one check per element, each \
naming its own. When a page may show the same thing in more than one place, \
such as an item's details repeated above and below it, name the one the \
claim is about by where it sits, such as "the price in the product summary \
under the product name".
- A target_meaning never quotes or contains the text its check asserts: it \
names what the element is and where it sits, not what it says. A target \
that names a tag "news", or a tab called "Home", gives the check away.
- A claim that a particular page is shown, such as an item's page or a \
profile page, includes a url_matches check on the page's address, in \
addition to any check of what the page shows.
- A claim about what a collection holds, such as a list of comments, tags \
or links, names the collection as the target, and the text is searched for \
inside it. A claim about a property of one item, such as who wrote the item \
just added, targets that item, not its whole collection.
- text is a literal, matched as whole words and ignoring case: prefer it. \
Use pattern, a Python regular expression searched with re.search, only when \
a literal can't say it, and write its flags inline, such as (?i). A URL is \
matched as it is, so write (?i) when its case may vary. A pattern must still \
require every part of the claim the expectation states, in order, not only \
some of its words.
- Assert nothing the spec can't tell you, such as a generated part of a URL \
or the order of items.
- requires lists the conditions the goal or the expectations need before \
the checks run, each with an id ("c1", "c2", ...), such as "checked after \
reloading the order page". Leave it empty when there are none.
"""


# What the request leaves out of the frontmatter: what spec_hash leaves out,
# and the account and the reset hook, which a plan doesn't need and which may
# hold a credential the spec writes out (AGENTS.md §6).
_LEFT_OUT: dict[str, bool | dict[str, bool]] = {
    **dict.fromkeys(UNHASHED_KEYS, True),
    "preconditions": {"account": True, "reset": True},
}


# A URL's query and fragment: the path says what a page or a probe is, and a
# query can carry a token the spec writes out.
_QUERY = re.compile(r"[?#].*", re.DOTALL)


def plan_request(spec: Spec) -> list[BaseMessage]:
    """The plan's request: the instructions, then the spec's frontmatter as
    validated, as JSON, without `tags`, the account or the reset hook, and
    with the start URL and each probe cut at its query. All of it comes from
    what `spec_hash` covers, so a change to the request means a change to
    the hash. Only what the spec sets is written, so a field the spec
    format gains later leaves existing requests, and their cassettes, as they
    were. The path, the Markdown body, the start origin and the environment
    never enter it."""
    frontmatter = spec.frontmatter.model_dump(
        mode="json", exclude_unset=True, exclude=_LEFT_OUT
    )
    preconditions = frontmatter["preconditions"]
    preconditions["start_url"] = _QUERY.sub("", preconditions["start_url"])
    if "probes" in preconditions:
        preconditions["probes"] = {
            name: _QUERY.sub("", endpoint)
            for name, endpoint in preconditions["probes"].items()
        }
    return [
        SystemMessage(content=INSTRUCTIONS),
        HumanMessage(
            content=json.dumps(
                frontmatter, indent=2, sort_keys=True, ensure_ascii=False
            )
        ),
    ]


@dataclass(frozen=True)
class Planned:
    """What the plan call gave, after its one retry if it needed one: the plan
    the model wrote, if it parsed; why it can't be used, if it can't (it
    doesn't fit its spec, or a check's target holds what the check asserts);
    and the routed call, with a cost record for every response that arrived.
    No plan means the model refused or its answer didn't parse, and
    `routed.outcome` says which."""

    plan: CoveragePlan | None
    problems: tuple[str, ...]
    routed: Routed


# Why a target may not hold what its check asserts, as each such line ends.
_TARGET_RULE = (
    "a target says what the element is for and where it sits, never what it says"
)


async def _why_circular(index: int, check: PlannedCheck) -> str | None:
    """Why `check`'s target holds what the check asserts, if it does: finding
    the element by the text it must verify would turn a wrong text into a
    missing element, drift instead of a failure (ADR-0025). A literal is
    found as the check finds it, whole words ignoring case; a pattern is the
    model's regex, so it is searched in a child bounded in time, as every
    pattern is (ADR-0024's bounded text searches)."""
    meaning = check.target_meaning
    if meaning is None:
        return None
    if check.text is not None:
        found = has_text(meaning, check.text)
        what = f'holds "{check.text}", the text'
    elif check.pattern is not None:
        try:
            found = await pattern_matches(check.pattern, meaning)
        except SearchTimeoutError as error:
            return (
                f'expect[{index}]: the pattern "{check.pattern}" could not be '
                f"searched in its own target: {error}"
            )
        what = f'matches "{check.pattern}", the pattern'
    else:
        return None
    if not found:
        return None
    return (
        f'expect[{index}]: the target "{meaning}" {what} its check asserts: '
        f"{_TARGET_RULE}"
    )


async def _planned(routed: Routed, spec: Spec) -> Planned:
    """The plan `routed` gave, with every reason it can't be used: `misfits`,
    then each check whose target holds what it asserts. A failure to judge it,
    such as a search process that couldn't run, raises `ModelCallError`
    holding `routed`'s records."""
    # The router parses against the schema it was given.
    plan = cast(CoveragePlan | None, routed.parsed)
    if plan is None:
        return Planned(None, (), routed)
    try:
        circular = [
            await _why_circular(planned.expect_index, check)
            for planned in plan.expectations
            for check in planned.checks
        ]
    except Exception as error:
        # The answers were billed: their records must outlive the failure.
        raise ModelCallError(routed.calls) from error
    found = (*misfits(plan, spec.frontmatter), *filter(None, circular))
    return Planned(plan, found, routed)


# What the one retry says after the plan that can't be used.
_CORRECTION = (
    "That plan can't be used:\n{reasons}\n"
    "Write the whole plan again: fix each of these, and keep the rest as it is."
)


def _keeping(spend: Spend, raised: list[Exception]) -> Spend:
    """`spend`, whose `on_priced` also keeps in `raised` what it raises."""

    def on_priced(record: CostRecord) -> None:
        try:
            spend.on_priced(record)
        except Exception as error:
            raised.append(error)
            raise

    return replace(spend, on_priced=on_priced)


async def make_plan(
    router: ModelRouter, spec: Spec, *, spend: Spend | None = None
) -> Planned:
    """Ask the navigator role's model for `spec`'s coverage plan: a call in
    explore mode, with the plan as its response format and no tools, so no
    tool choice at all (ADR-0007 amendment). A plan that can't be used is
    asked for once more, with the plan as the model's answer and every reason
    after it, and the second answer is final (ADR-0024's #161 amendment). A
    refusal falls back as the router does; a call that gets no response raises
    as the router does, and a retry's failure raises `ModelCallError` holding
    the first answer's record too. The retry gets what the first answer left
    of `spend`'s ceiling, and none once it is reached (ADR-0007's #53 P3)."""
    request = plan_request(spec)
    ask = partial(router.call, "navigator", "explore", schema=CoveragePlan)
    first = await _planned(await ask(request, spend=spend), spec)
    if first.plan is None or not first.problems:
        return first
    if spend is not None and spend.reached(first.routed.calls):
        return first
    retry = [
        *request,
        AIMessage(content=first.plan.model_dump_json(exclude_none=True)),
        HumanMessage(
            content=_CORRECTION.format(
                reasons="\n".join(f"- {reason}" for reason in first.problems)
            )
        ),
    ]
    billed = first.routed.calls
    raised: list[Exception] = []
    retry_spend = None if spend is None else _keeping(spend.after(billed), raised)
    try:
        routed = await ask(retry, spend=retry_spend)
    except Exception as error:
        if raised and error is raised[0]:
            raise  # the caller's own on_priced failed: it leaves as it came
        if isinstance(error, ModelCallError):
            raise ModelCallError((*billed, *error.records)) from error.__cause__
        # The first answer was billed: its record must outlive the failure.
        raise ModelCallError(billed) from error
    return await _planned(replace(routed, calls=(*billed, *routed.calls)), spec)
