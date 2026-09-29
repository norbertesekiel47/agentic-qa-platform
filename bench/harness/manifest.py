"""The benchmark's ground-truth manifest, ``bench/manifest.v1.json`` (ADR-0022).

Validates the manifest against the format in DATA_MODEL.md §8, checks the
split-freeze hash (TESTING.md §5) and loads typed cases for the harness.
Standard library only until M1 brings Pydantic.

Run: python3 bench/harness/manifest.py [--root DIR] [--split-hash]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path("bench/manifest.v1.json")
SPLIT_HASH = Path("bench/manifest.v1.split.sha256")
APPS = Path("bench/apps")

SCHEMA_VERSION = 1
KINDS = ("bug", "benign")
# The six finding categories (DATA_MODEL.md §2, findings.category).
CATEGORIES = (
    "functional",
    "visual",
    "backend_5xx",
    "js_error",
    "broken_flow",
    "data_display",
)
SPLITS = ("dev", "test")
VIOLATED = "expectation_violated"
DRIFT = "drift_consistent"
VERDICTS = (VIOLATED, DRIFT)
# What a run checks beyond a spec's expect items (DATA_MODEL.md §6).
INVARIANTS = ("console_errors", "js_exceptions", "http_5xx", "broken_images")

CASE_ID = re.compile(r"(?P<app>[a-z][a-z0-9]*)-(?P<kind>bug|benign)-\d{3}")
FLAG = re.compile(r"[a-z0-9]{4}")
# The harness self-test switches this flag, and no app code checks it (flags.py).
SELFTEST_FLAG = "0000"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

CASE_REQUIRED = ("app", "kind", "split", "family", "flag", "summary", "expected")
CASE_OPTIONAL = ("category",)
ENTRY_REQUIRED = ("spec", "verdict")
ENTRY_OPTIONAL = ("expect", "invariants")

# Spec files (DATA_MODEL.md §6) start with YAML front matter. With no YAML parser
# before M1, spec_front_matter relies on that section's layout: top-level keys at
# column 0, and each expect item a "  - " line two spaces in.
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
TOP_LEVEL_KEY = re.compile(r"[A-Za-z_][\w-]*:")


@dataclass(frozen=True)
class Expected:
    """One spec's expected outcome while a case's flag is on."""

    spec: str
    verdict: str
    expect: tuple[int, ...]
    invariants: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    id: str
    app: str
    kind: str
    category: str | None
    split: str
    family: str
    flag: str
    summary: str
    expected: tuple[Expected, ...]


@dataclass(frozen=True)
class Manifest:
    cases: Mapping[str, Case]

    def case_for_flag(self, flag: str) -> Case | None:
        return next((c for c in self.cases.values() if c.flag == flag), None)


class ManifestError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json.loads keeps the last of two equal keys, so a copy-pasted case would
    # silently replace the first.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _read(root: Path) -> tuple[Any, list[str]]:
    path = root / MANIFEST
    if not path.is_file():
        return None, [f"missing {MANIFEST}"]
    try:
        return json.loads(path.read_text(), object_pairs_hook=_reject_duplicates), []
    except _DuplicateKeyError as exc:
        return None, [f"{MANIFEST}: duplicate key '{exc}'"]
    except json.JSONDecodeError as exc:
        return None, [f"{MANIFEST} is not valid JSON: {exc}"]


def _keys(
    obj: dict[str, Any],
    required: tuple[str, ...],
    optional: tuple[str, ...],
    where: str,
    errors: list[str],
) -> None:
    errors.extend(f"{where}: missing key '{k}'" for k in required if k not in obj)
    allowed = set(required) | set(optional)
    errors.extend(f"{where}: unknown key '{k}'" for k in obj if k not in allowed)


def _string(obj: dict[str, Any], key: str, where: str, errors: list[str]) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{where}: {key} must be a string")
        return None
    return value


def _one_of(
    value: str | None, key: str, allowed: tuple[str, ...], where: str
) -> list[str]:
    if value is None or value in allowed:
        return []
    return [f"{where}: {key} '{value}' is not one of {', '.join(allowed)}"]


def spec_front_matter(text: str) -> tuple[str | None, int | None]:
    """A spec file's id and its number of expect items (None where absent)."""
    match = FRONT_MATTER.match(text)
    if match is None:
        return None, None
    spec_id: str | None = None
    has_expect, count, in_expect = False, 0, False
    for line in match[1].splitlines():
        if TOP_LEVEL_KEY.match(line):
            key, _, value = line.partition(":")
            in_expect = key == "expect"
            has_expect = has_expect or in_expect
            if key == "id":
                spec_id = value.strip()
        elif in_expect and line.startswith("  - "):
            count += 1
    return spec_id, (count if has_expect else None)


def _valid_invariants(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    names = [n for n in value if isinstance(n, str) and n in INVARIANTS]
    return len(names) == len(value) and len(set(names)) == len(names)


def _check_spec_file(
    root: Path, app: str, spec: str, expect: object, where: str
) -> list[str]:
    spec_file = APPS / app / "qa" / f"{spec}.spec.md"
    path = root / spec_file
    if not path.is_file():
        return [f"{where}: no spec file {spec_file}"]
    spec_id, count = spec_front_matter(path.read_text())
    errors: list[str] = []
    if spec_id != spec:
        errors.append(
            f"{where}: {spec_file}: id '{spec_id}' does not match the file name"
        )
    if count is None:
        return [*errors, f"{where}: {spec_file}: no expect list in its front matter"]
    if isinstance(expect, list) and _valid_indexes(expect):
        errors.extend(
            f"{where}: expect index {i} is out of range: spec '{spec}' has {count} expect items"
            for i in expect
            if i >= count
        )
    return errors


def _valid_indexes(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    ints = [i for i in value if type(i) is int and i >= 0]
    return len(ints) == len(value) and len(set(ints)) == len(ints)


def _validate_entry(
    entry: object, where: str, app: str | None, kind: str | None, root: Path
) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{where}: must be an object"]
    errors: list[str] = []
    _keys(entry, ENTRY_REQUIRED, ENTRY_OPTIONAL, where, errors)
    spec = _string(entry, "spec", where, errors)
    verdict = _string(entry, "verdict", where, errors)
    errors += _one_of(verdict, "verdict", VERDICTS, where)
    if verdict == VIOLATED and kind == "benign":
        errors.append(f"{where}: a benign case is {DRIFT} in every spec")
    if verdict == VIOLATED and not any(k in entry for k in ENTRY_OPTIONAL):
        errors.append(
            f"{where}: an {VIOLATED} entry names expect indexes, invariants or both"
        )
    if verdict == DRIFT:
        errors.extend(
            f"{where}: {k} is only for {VIOLATED}" for k in ENTRY_OPTIONAL if k in entry
        )
    if "expect" in entry and not _valid_indexes(entry["expect"]):
        errors.append(
            f"{where}: expect must be a non-empty list of distinct non-negative integers"
        )
    if "invariants" in entry and not _valid_invariants(entry["invariants"]):
        errors.append(
            f"{where}: invariants must be a non-empty list of distinct names from "
            + ", ".join(INVARIANTS)
        )
    if spec is not None and not NAME.fullmatch(spec):
        errors.append(f"{where}: spec '{spec}' must be a kebab-case spec id")
    elif spec is not None and app is not None:
        errors += _check_spec_file(root, app, spec, entry.get("expect"), where)
    return errors


def _validate_expected(
    value: object, case_id: str, app: str | None, kind: str | None, root: Path
) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{case_id}: expected must be a non-empty list"]
    errors: list[str] = []
    specs: list[object] = []
    for index, entry in enumerate(value):
        where = f"{case_id}: expected[{index}]"
        errors += _validate_entry(entry, where, app, kind, root)
        if isinstance(entry, dict):
            specs.append(entry.get("spec"))
    for spec in sorted({s for s in specs if isinstance(s, str) and specs.count(s) > 1}):
        errors.append(f"{case_id}: spec '{spec}' is listed twice")
    verdicts = [e.get("verdict") for e in value if isinstance(e, dict)]
    if kind == "bug" and VIOLATED not in verdicts:
        errors.append(f"{case_id}: a bug needs at least one {VIOLATED} entry")
    return errors


def _validate_identity(
    case_id: str, app: str | None, kind: str | None, root: Path
) -> list[str]:
    match = CASE_ID.fullmatch(case_id)
    if match is None:
        return [f"case id '{case_id}' must look like <app>-<bug|benign>-NNN"]
    errors: list[str] = []
    if app is not None and app != match["app"]:
        errors.append(f"{case_id}: app '{app}' does not match the case id")
    if kind is not None and kind != match["kind"]:
        errors.append(f"{case_id}: kind '{kind}' does not match the case id")
    if app is not None and not (root / APPS / app).is_dir():
        errors.append(f"{case_id}: no app directory {APPS / app}")
    return errors


def _validate_case(case_id: str, case: object, root: Path) -> list[str]:
    if not isinstance(case, dict):
        return [f"{case_id}: must be an object"]
    errors: list[str] = []
    _keys(case, CASE_REQUIRED, CASE_OPTIONAL, case_id, errors)
    app = _string(case, "app", case_id, errors)
    kind = _string(case, "kind", case_id, errors)
    errors += _validate_identity(case_id, app, kind, root)
    errors += _one_of(kind, "kind", KINDS, case_id)

    category = _string(case, "category", case_id, errors)
    if kind == "bug" and "category" not in case:
        errors.append(f"{case_id}: a bug needs a category")
    if kind == "benign" and "category" in case:
        errors.append(f"{case_id}: a benign case has no category")
    errors += _one_of(category, "category", CATEGORIES, case_id)

    errors += _one_of(_string(case, "split", case_id, errors), "split", SPLITS, case_id)
    family = _string(case, "family", case_id, errors)
    if family is not None and app is not None:
        rest = family.removeprefix(f"{app}-")
        if rest == family or not NAME.fullmatch(rest):
            errors.append(f"{case_id}: family '{family}' must look like {app}-<name>")
    flag = _string(case, "flag", case_id, errors)
    if flag is not None and not FLAG.fullmatch(flag):
        errors.append(f"{case_id}: flag '{flag}' must be 4 lowercase letters or digits")
    if flag == SELFTEST_FLAG:
        errors.append(f"{case_id}: flag '{flag}' is reserved for the harness self-test")
    summary = _string(case, "summary", case_id, errors)
    if summary is not None and (not summary.strip() or "\n" in summary):
        errors.append(f"{case_id}: summary must be one non-empty line")
    if "expected" in case:
        errors += _validate_expected(case["expected"], case_id, app, kind, root)
    return errors


def _grouped(cases: dict[str, Any], field: str) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for case_id, case in cases.items():
        value = case.get(field) if isinstance(case, dict) else None
        if isinstance(value, str):
            groups.setdefault(value, set()).add(case_id)
    return groups


def _validate_across_cases(cases: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for flag, ids in sorted(_grouped(cases, "flag").items()):
        if len(ids) > 1:
            errors.append(f"flag '{flag}' is used by {', '.join(sorted(ids))}")
    for family, ids in sorted(_grouped(cases, "family").items()):
        splits = {s for i in ids if isinstance(s := cases[i].get("split"), str)}
        if len(splits) > 1:
            errors.append(
                f"family '{family}' spans splits: {', '.join(sorted(splits))}"
            )
    return errors


def validate(data: object, root: Path) -> list[str]:
    """Every problem with the manifest ``data``, or an empty list."""
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]
    errors: list[str] = []
    _keys(data, ("schema_version", "cases"), (), "manifest", errors)
    version = data.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        errors.append(f"manifest: schema_version must be {SCHEMA_VERSION}")
    cases = data.get("cases", {})
    if not isinstance(cases, dict):
        return [*errors, "manifest: cases must be an object keyed by case id"]
    for case_id, case in cases.items():
        errors += _validate_case(case_id, case, root)
    return errors + _validate_across_cases(cases)


def split_hash(data: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of ``{case_id: [split, family]}``.

    It changes only when a case moves between splits or families, so fixing a
    summary is not an unfreeze (ADR-0022).
    """
    assignment = {cid: [c["split"], c["family"]] for cid, c in data["cases"].items()}
    canonical = json.dumps(assignment, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_valid(root: Path) -> tuple[Any, list[str]]:
    data, errors = _read(root)
    return data, errors or validate(data, root)


def check(root: Path) -> list[str]:
    """Validate the committed manifest and its split-freeze hash."""
    data, errors = _read_valid(root)
    if errors:
        return errors
    path = root / SPLIT_HASH
    if not path.is_file():
        return [f"missing {SPLIT_HASH}; write it from --split-hash"]
    committed, actual = path.read_text().strip(), split_hash(data)
    if committed == actual:
        return []
    message = (
        f"split assignment changed: {SPLIT_HASH} has {committed[:12]}, "
        f"the manifest hashes to {actual[:12]}. Moving a case between splits "
        "or families is a deliberate, reviewed change (TESTING.md §5): "
        "update the hash file from --split-hash in the same pull request."
    )
    return [message]


def load(root: Path = REPO_ROOT) -> Manifest:
    """The validated manifest as typed cases; raises ManifestError otherwise."""
    data, errors = _read_valid(root)
    if errors:
        raise ManifestError(errors)
    cases = {
        case_id: Case(
            id=case_id,
            app=c["app"],
            kind=c["kind"],
            category=c.get("category"),
            split=c["split"],
            family=c["family"],
            flag=c["flag"],
            summary=c["summary"],
            expected=tuple(
                Expected(
                    e["spec"],
                    e["verdict"],
                    tuple(e.get("expect", ())),
                    tuple(e.get("invariants", ())),
                )
                for e in c["expected"]
            ),
        )
        for case_id, c in data["cases"].items()
    }
    return Manifest(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument(
        "--split-hash",
        action="store_true",
        help=f"print the split-assignment hash to commit in {SPLIT_HASH}",
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    data, errors = _read_valid(root)
    if not errors and not args.split_hash:
        errors = check(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    digest = split_hash(data)
    if args.split_hash:
        print(digest)
    else:
        print(f"{MANIFEST}: OK, {len(data['cases'])} cases, split hash {digest[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
