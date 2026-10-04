"""Finite, reviewed establishing checks for the five Conduit pilot plans."""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from aqa_core.coverage_plan import CoveragePlan, PlannedCheck
from aqa_core.spec import SpecFrontmatter, canonical_hash


@dataclass(frozen=True)
class Row:
    required: tuple[PlannedCheck, ...]
    forbidden: tuple[str, ...] = ()


def scoped(target: str, literal: str) -> PlannedCheck:
    return PlannedCheck(check="text_in_target", target_meaning=target, text=literal)


ROOT_PATTERN = r"^https?://[^/?#]+/$"
PUBLISH_PATTERN = r"(?i)^https?://[^/?#]+/article/benchmarks-we-trust-"
RELOAD = "Reload the article after favoriting and before checking the button and count"
HEADER_LINKS = {
    "new article": "header new-article link",
    "settings": "header settings link",
    "reader": "header current-user profile link",
}


def row(*checks: PlannedCheck, forbidden: tuple[str, ...] = ()) -> Row:
    return Row(checks, forbidden)


ROWS = {
    "login": (
        row(
            PlannedCheck(check="url_matches", pattern=ROOT_PATTERN),
            forbidden=("Global Feed heading",),
        ),
        row(PlannedCheck(check="text_visible", text="Your Feed")),
        row(
            *tuple(
                scoped("header nav", label)
                for label in ("New Article", "Settings", "reader")
            ),
            forbidden=("global header-label text",),
        ),
        row(
            *tuple(
                PlannedCheck(check="not_visible", target_meaning=role)
                for role in ("header sign-in link", "header sign-up link")
            )
        ),
        row(
            *tuple(
                PlannedCheck(check="visible_unoccluded", target_meaning=role)
                for role in HEADER_LINKS.values()
            ),
            forbidden=("header text without hit tests",),
        ),
    ),
    "post-comment": (
        row(scoped("article comment list", "Thanks for the warm welcome!")),
        row(
            scoped("new comment card author link", "reader"),
            forbidden=("global reader text",),
        ),
        row(PlannedCheck(check="text_visible", text="Glad to be here.")),
        row(
            PlannedCheck(check="probe_equals", probe="comment_count", value=2),
            forbidden=("optimistic comment UI",),
        ),
    ),
    "favorite-article": (
        row(
            scoped("article banner favorite button", "Unfavorite Article"),
            forbidden=("button without reload",),
        ),
        row(
            scoped("article banner favorites count", "1"),
            forbidden=("count without reload",),
        ),
    ),
    "publish-article": (
        row(
            PlannedCheck(check="url_matches", pattern=PUBLISH_PATTERN),
            scoped("article banner title heading", "Benchmarks we trust"),
            forbidden=("case-sensitive slug", "exact generated slug"),
        ),
        row(scoped("article body", "Every number cites the commit that produced it.")),
        row(
            scoped("article tag list", "testing"),
            scoped("article tag list", "benchmarks"),
        ),
        row(
            scoped("article banner author link", "jake"),
            forbidden=("global jake text",),
        ),
        row(
            PlannedCheck(check="probe_equals", probe="jake_article_count", value=6),
            forbidden=("one POST request",),
        ),
    ),
    "read-article": (
        row(scoped("article banner title heading", "Testing without flakes")),
        row(scoped("article banner author link", "anna")),
        row(
            scoped("article banner publication date", "January 4, 2026"),
            forbidden=("global publication date",),
        ),
        row(scoped("article body", "Most flaky tests are really flaky data.")),
        row(scoped("article tag list", "testing"), forbidden=("global tag text",)),
        row(scoped("article comment list", "Deterministic data helped us most.")),
        row(
            scoped(
                "signed-out comment prompt",
                "Sign in or sign up to add comments on this article",
            ),
            forbidden=("partial comment prompt", "global Sign in text"),
        ),
    ),
}


REVIEW_FINGERPRINTS = {
    "login": (
        "05c6f2c3800610f7b89decaed65eee7a1045455b453a338acc0ba3334367c44c",
        "6647a731af9ed3d9784249a47074fb8226f248aa7abb5ad7346c912e1a46fb42",
        "bb3b5b2c075b77d56bb7263c601f267518909079fc9b5eb7ef0ea34a50adba5e",
        "ac4e1220d11437fd4278de3cd68b67bad5dae72d24f0fde2207a8e653ef9bc62",
        "7796683e1c8b4b890c0495b4067cfe174e3fdbc14c465a89310aa4faed032a2d",
    ),
    "read-article": (
        "c27624b271108def2c2918c39b38b3de3b2078b02983d176b2dc27dfa3b24889",
        "576e8cde6c6d17d81a3db42c6792a9d606987bd4808ee73859c152417aec7305",
        "ca3574399af691f0b4290184c46d4cc42dc6870c0ed65dbaf1477442abe7879a",
        "a4a14822f15776e8622cf31eeda74269a490e5df4d8c986d2fc2b4e2690001a4",
        "5a0c5bdff56028b4197d284449024676cc1c99615242417afa5177660205f8c8",
        "9fcf071d4ef1dbdb1d703cdd13da7f399750b3388b214b489d9ea7598553a0c2",
        "ef88c1a35f9d46b76d9a277b78986cd122621aa948b861382d8b3c1f4b8a38d6",
    ),
    "post-comment": (
        "1f2c20843acb78d6d70daae3d62d412e3c2526c162961cafcb9d168ae00a0bbf",
        "3f0c2fa8622cab0919f8a4f92640e7d330b3376fa72bd9b0e12feb478b9adf14",
        "47ab556e1bab3786f19227e5d9d415f5460a9bfaa92124bb5f7ea35776ce145c",
        "287bf479ac3931b0acd21414b935463a11f118fbdeac1dae689d10675ef503d8",
    ),
    "favorite-article": (
        "020a816a335fbce5974717250a81f3a8b2ecd8c1cfe89dd1e03adff95685c465",
        "ed40b0eb994fb200ec0a012cb2d50ba1a1d4639238de78c53e7b196cf8c2eb71",
    ),
    "publish-article": (
        "d73f934c50600c418380ef4dee747fec10c8e1deb88412eefc1f40ff49455b5e",
        "cda486b91b7aff06245eafe32334cea773f50e731a2e1599539ed3387e9ad46c",
        "2f3e33a72b9358decaeefff5362533104731d4520fecc903c96b1c149610b2eb",
        "e7a286ead54892e791f0980c25a92b4f9da78d8d8eb4d0d653d9b64143b61c64",
        "df2e33577b02c1c03b464de84408d00480dbebc1d5e978fa6f101fcbb84e942f",
    ),
}
SPEC_FINGERPRINTS = {
    "favorite-article": "sha256:d12b0347122cd823a80a4c92d0e6ac41ae60d5ec6cb4c45bf006cae21294d02d",
    "login": "sha256:44a1d01eab43baa1e05a5ec788c8d85cfad7c0e4fb5cf49579d788183401cfbf",
    "post-comment": "sha256:a9c45a1cfdcf5f2aad72388253214f3a83e4e27efed11befcb9fd74108e13867",
    "publish-article": "sha256:0723762bc1b6a3724691a445bb154498a41f5929c2dfb53a3dfcec0552b1ce3d",
    "read-article": "sha256:2ea2ac88b1eb1bcccf0250dc3494e980cca32383ed0465e8237331195b206b3a",
}


def phrase(value: str) -> str:
    return " ".join(value.split()).casefold()


def check_key(check: PlannedCheck) -> str:
    fields = check.model_dump(mode="json")
    if check.target_meaning is not None:
        fields["target_meaning"] = phrase(check.target_meaning)
    if check.text is not None:
        fields["text"] = check.text.casefold()
    if check.check == "text_in_target" and fields["target_meaning"] == HEADER_LINKS.get(
        fields["text"]
    ):
        fields["target_meaning"] = "header nav"
    return json.dumps(fields, sort_keys=True)


def plan_problems(spec_id: str, plan: CoveragePlan) -> tuple[str, ...]:
    if spec_id not in ROWS:
        return (f"{spec_id}: unknown pilot",)
    rows = ROWS[spec_id]
    if tuple(item.expect_index for item in plan.expectations) != tuple(
        range(len(rows))
    ):
        return (f"{spec_id}: expectation indexes differ",)
    problems = [
        f"{spec_id}/{index}: establishing checks differ"
        for index, (row, expectation) in enumerate(
            zip(rows, plan.expectations, strict=True)
        )
        if expectation.unsupported is not None
        or Counter(map(check_key, row.required))
        != Counter(map(check_key, expectation.checks))
    ]
    conditions = tuple(phrase(condition.condition) for condition in plan.requires)
    if spec_id == "favorite-article" and conditions != (phrase(RELOAD),):
        problems.extend(
            f"{spec_id}/{index}: reload condition differs" for index in range(len(rows))
        )
    elif spec_id != "favorite-article" and conditions:
        problems.append(f"{spec_id}: unreviewed conditions")
    return tuple(problems)


def review_rows(review: str) -> dict[str, list[tuple[int, str]]] | None:
    sections: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for line in review.splitlines():
        if line.startswith("#"):
            current = None
        if heading := re.fullmatch(r"### `([^`]+)`", line):
            current = heading[1]
            if current in sections:
                return None
            sections[current] = []
        elif indexed := re.match(r"\| (\d+) \|", line):
            if current is None:
                return None
            sections[current].append(
                (int(indexed[1]), hashlib.sha256(line.encode()).hexdigest())
            )
    return sections


def source_problems(review: str, specs: Sequence[SpecFrontmatter]) -> tuple[str, ...]:
    if review_rows(review) != {
        name: list(enumerate(hashes)) for name, hashes in REVIEW_FINGERPRINTS.items()
    }:
        return ("REVIEW rows differ",)
    actual = Counter(
        (
            spec.id,
            len(spec.expect),
            canonical_hash([e.model_dump(mode="json") for e in spec.expect]),
        )
        for spec in specs
    )
    expected = Counter(
        (name, len(ROWS[name]), fingerprint)
        for name, fingerprint in SPEC_FINGERPRINTS.items()
    )
    if actual != expected:
        return ("spec expectations differ",)
    return ()
