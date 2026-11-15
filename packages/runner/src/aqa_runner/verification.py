"""A verified replay's verifier (ADR-0024's #256 amendment): each `model_verify`
check whose target resolved comes here (`evaluation.Verify`), in assertion
order.

Unless the run was interrupted, an earlier call got no response or the run's
`verified_model_usd` is spent, the verifier looks at the page once: an
accessibility snapshot, in which it finds the ref of the very element the
target resolved to, then the check's evidence in `verified/assertion-<index>/`,
the snapshot and the page's masked screenshot among it (`evidence`). Only when
both the ref and the screenshot are there does it send the verifier role the
expectation's text, the two references and that screenshot
(`verifier.verify_request`), never the snapshot, and read what the reply
decides (`verifier.judged`). Each priced response is on disk in `costs.jsonl`
before anything else runs (`Spending`). A call that got no response, or whose
cost line isn't on disk, is a stop of the whole run, and no later call is
made."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from aqa_core.compiled import CompiledScript, ModelVerify
from aqa_core.spec import Spec
from langchain_core.messages import BaseMessage
from playwright.async_api import ElementHandle

from aqa_runner.anthropic_client import ProviderError
from aqa_runner.binding import same_element
from aqa_runner.browser_session import BrowserSession, RefError
from aqa_runner.evaluation import Verified, bounded, looked
from aqa_runner.evidence import Asked, Shot, capture_evidence
from aqa_runner.model_router import ModelCallError, ModelRouter
from aqa_runner.model_turns import (
    UNRECORDED,
    UNSENT,
    Spending,
    UnsentRequestError,
    no_response,
    unspelled,
)
from aqa_runner.redaction import Redacted, Redactor
from aqa_runner.replay_result import Stop, Stoppage
from aqa_runner.run_record import UnrecordedCostError
from aqa_runner.verifier import VerifierVerdict, judged, verify_request

# Why the verifier wasn't called, in fixed words.
INTERRUPTED: Final = "the run was interrupted, so the verifier wasn't called"
SILENCED: Final = (
    "an earlier verifier call got no response, so the verifier wasn't called"
)
INCOMPLETE: Final = (
    "an earlier verifier call's cost line isn't on disk, so the verifier wasn't called"
)
SPENT: Final = "the verified budget ran out, so the verifier wasn't called"
NO_REF: Final = (
    "the target's element has no ref in the page's snapshot, so the verifier "
    "wasn't called"
)
WITHHELD: Final = "the screenshot was withheld, so the verifier wasn't called"

# What a check's evidence saves of the page: its screenshot after the look,
# the one sent, and no "before".
_AFTER: Final = Asked(after=True)

# An element's ref in a session's snapshot: page text that imitates one reads
# `(ref=` (`snapshot_refs.as_text`).
_REF: Final = re.compile(r"\[ref=(e[0-9]+)\]")


@dataclass
class Verifying:
    """One verified run's verifier: its router and spend, the spec and the
    script it replays, whether the run is interrupted yet, and the bound on
    each look at the page. `verify` is what the run's evaluation hands each
    `model_verify` check. `stops` are the stops of the whole run its calls hit,
    and once one is hit no call is made."""

    router: ModelRouter
    spending: Spending
    spec: Spec
    script: CompiledScript
    interrupted: Callable[[], bool]
    seconds: float
    stops: list[Stop] = field(default_factory=list)
    _silenced: str | None = field(default=None, init=False)

    @property
    def _indexes(self) -> Mapping[str, int]:
        # An assertion's id may hold `/`, so its folder is named by its index.
        return {check.id: index for index, check in enumerate(self.script.assertions)}

    async def verify(
        self, session: BrowserSession, check: ModelVerify, element: ElementHandle
    ) -> Verified:
        """What `check` comes to, its target resolved to `element` (module
        docstring). A record that can't take its evidence raises, as a step's
        evidence does; a look at the page that raises leaves the check not
        evaluated (`evaluation.evaluate`)."""
        if (unasked := self._unasked()) is not None:
            return Verified("inconclusive", unasked)
        folder = f"verified/assertion-{self._indexes[check.id]}"
        seconds = self.seconds
        snapshot = await bounded(looked(session.snapshot, seconds), seconds)
        ref = await bounded(_ref_of(session, snapshot, element), seconds)
        saved = await capture_evidence(
            self.spending.record, session, folder, None, snapshot, asked=_AFTER
        )
        shot = saved.after
        verdict = await self._asked(session, check, ref, shot)
        return replace(verdict, folder=folder, shot=shot)

    async def _asked(
        self,
        session: BrowserSession,
        check: ModelVerify,
        ref: str | None,
        shot: Shot | None,
    ) -> Verified:
        """What the verifier decides of `check`, shown the screenshot `shot`
        saved and citing `ref`, or why it wasn't asked: no ref, no screenshot,
        a request that still spells a bound value, or a run interrupted by
        now."""
        if ref is None:
            return Verified("inconclusive", NO_REF)
        if shot is None or shot.image is None or shot.path is None:
            return Verified("inconclusive", WITHHELD)
        expectation = self.spec.frontmatter.expect[check.expect_index].text
        messages = verify_request(
            expectation, shot.path, ref, shot.image, redactor=session.redactor
        )
        try:
            unspelled(session.redactor)(messages)
        except UnsentRequestError:
            return Verified("inconclusive", UNSENT)
        if self.interrupted():
            return Verified("inconclusive", INTERRUPTED)
        cited = frozenset({shot.path, ref})
        return await self._called(messages, cited, session.redactor)

    async def _called(
        self, messages: list[BaseMessage], cited: frozenset[str], redactor: Redactor
    ) -> Verified:
        """What the reply to `messages` decides (`verifier.judged`), or the stop
        of the whole run its call hit: no response, or a cost line not on
        disk."""
        try:
            routed = await self.router.call(
                "verifier",
                "verified",
                messages,
                schema=VerifierVerdict,
                spend=self.spending.spend(),
            )
        except UnrecordedCostError:
            return self._stopped("record_error", UNRECORDED, INCOMPLETE)
        except (ModelCallError, ProviderError) as error:
            return self._stopped("no_response", no_response(error), SILENCED)
        return judged(routed, cited, redactor)

    def _unasked(self) -> str | None:
        """Why no capture or call is made for the next check, if any."""
        if self.interrupted():
            return INTERRUPTED
        if self._silenced is not None:
            return self._silenced
        if self.spending.left <= 0:
            return SPENT
        return None

    def _stopped(self, outcome: Stoppage, reason: str, later: str) -> Verified:
        """Stop the run on `outcome`, for `reason`, which is also the check's,
        and give `later` as why no later check calls the verifier."""
        scanned: Redacted = self.spending.record.redactor.redact(reason)
        self.stops.append(Stop(outcome, scanned))
        self._silenced = later
        return Verified("inconclusive", scanned)


async def _ref_of(
    session: BrowserSession, snapshot: str, element: ElementHandle
) -> str | None:
    """The ref `snapshot`, the session's current one, gives `element` itself,
    by identity, not by a role or name it shares with another; None when it
    gives that element none, as for one Playwright folds into its parent's
    line (LAB_NOTES, 2026-10-02). A ref whose element has left the page names
    nothing."""
    refs: list[str] = _REF.findall(snapshot)
    for ref in dict.fromkeys(refs):
        try:
            found = await session.locate(ref)
        except RefError:
            continue
        try:
            if await same_element(element, found):
                return ref
        finally:
            await found.dispose()
    return None
