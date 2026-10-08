"""Reading a project's config and specs strictly, before a browser or a model
is involved (DATA_MODEL §6, §9; ADR-0030). Every problem is reported at once,
each naming the file, the key and what is wrong."""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ValidationError

from aqa_core import strict_yaml
from aqa_core.browser import BrowserSettings
from aqa_core.compiled import (
    Click,
    CompiledScript,
    Fill,
    FillSecret,
    NotVisible,
    ProbeEqualsBaseline,
    Select,
    TextInTarget,
    VisibleUnoccluded,
)
from aqa_core.config import ProjectConfig, SecretField
from aqa_core.model_roles import RoleError, resolve_roles
from aqa_core.price_map import vendored
from aqa_core.schema import Contract, parse_origin
from aqa_core.spec import (
    Spec,
    SpecContext,
    SpecFrontmatter,
    canonical_hash,
    secret_references,
    spec_hash,
)


class SpecError(Exception):
    """A spec or the project config is invalid: ADR-0024's `spec_error`. Each
    problem reads `<file>: <key>: <problem>`, or `<file>: line N: <problem>`
    for YAML that doesn't parse."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = tuple(problems)


def _read_text(path: Path) -> str:
    """The text of `path`. A directory and text that isn't UTF-8 are spec
    errors; any other read error propagates as it is (DATA_MODEL §6)."""
    try:
        # utf-8-sig: a byte order mark an editor wrote is not part of the text.
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise SpecError([f"{path}: not UTF-8 text"]) from None
    except IsADirectoryError:
        raise SpecError([f"{path}: a directory, not a file"]) from None


def _read_yaml(text: str, path: Path, first_line: int) -> object:
    """`text` parsed; `first_line` is its first line's number in `path`."""
    try:
        return strict_yaml.parse(text)
    except strict_yaml.StrictYAMLError as error:
        line = "" if error.line is None else f"line {error.line + first_line}: "
        raise SpecError([f"{path}: {line}{error.problem}"]) from None


def _key(loc: tuple[int | str, ...]) -> str:
    key = ""
    for part in loc:
        if isinstance(part, int):
            key += f"[{part}]"
        elif part != "[key]":  # Pydantic's marker for an invalid mapping key
            shown = part if part.isprintable() else repr(part)
            key += f".{shown}" if key else shown
    return key


def _problems(error: ValidationError, path: Path) -> Iterator[str]:
    for detail in error.errors():
        match detail["type"]:
            case "extra_forbidden":
                problem = "unknown key"
            case "missing":
                problem = "missing key"
            case "value_error":
                problem = str(detail["ctx"]["error"])
            # Pydantic names Python's types; the file is YAML.
            case "model_type" | "dict_type":
                problem = "must be a mapping of keys"
            case "tuple_type":
                problem = "must be a list"
            case _:
                problem = detail["msg"]
        # A problem with the whole file, such as JSON pydantic can't parse,
        # has no key.
        key = _key(detail["loc"])
        yield f"{path}: {key}: {problem}" if key else f"{path}: {problem}"


def _validate[M: BaseModel](
    model: type[M], data: object, path: Path, context: SpecContext | None = None
) -> M:
    try:
        return model.model_validate(data, context=context)
    except ValidationError as error:
        raise SpecError(list(_problems(error, path))) from None


def _read_config(path: Path) -> ProjectConfig:
    """The project config at `path`, as the file's shape allows."""
    try:
        text = _read_text(path)
    except FileNotFoundError:
        raise SpecError(
            [
                (
                    f"{path}: no such file: a project's spec root is the directory "
                    "that holds its config.yaml"
                )
            ]
        ) from None
    data = _read_yaml(text, path, first_line=1)
    if data is None:  # an empty file, or comments only
        data = {}
    if not isinstance(data, dict):
        raise SpecError([f"{path}: the project config must be a mapping of keys"])
    return _validate(ProjectConfig, data, path)


def _role_problems(config: ProjectConfig, path: Path) -> list[str]:
    """Why a role in `config` can't be routed, if one can't (ADR-0007)."""
    try:
        resolve_roles(config, vendored())
    except RoleError as error:
        return [f"{path}: {key}: {problem}" for key, problem in error.problems]
    return []


def load_config(path: Path) -> ProjectConfig:
    """The project config at `path`. The directory that holds it is the
    project's spec root (DATA_MODEL §9). Every role must be routable: its model
    priced, with the capabilities the role needs."""
    config = _read_config(path)
    if problems := _role_problems(config, path):
        raise SpecError(problems)
    return config


# A spec file opens with its frontmatter, between two lines of three dashes.
_FRONTMATTER = re.compile(r"---\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)


def load_spec(path: Path, config: ProjectConfig) -> Spec:
    """The spec at `path`, carrying the bindings in `config` of exactly the
    test secrets it references. Its secret references must name secrets
    `config` declares, and its id must be its file name without `.spec.md`."""
    frontmatter, hashed = _read_spec(path, frozenset(config.secrets))
    return _spec_with_bindings(path, frontmatter, hashed, config)


def _read_spec(
    path: Path, declared_secrets: frozenset[str] | None
) -> tuple[SpecFrontmatter, str]:
    """The frontmatter of the spec at `path`, and its spec_hash."""
    match = _FRONTMATTER.match(_read_text(path))
    if match is None:
        raise SpecError(
            [f"{path}: a spec starts with its frontmatter between two --- lines"]
        )
    data = _read_yaml(match[1], path, first_line=2)
    if not isinstance(data, dict):
        raise SpecError([f"{path}: the frontmatter must be a mapping of keys"])
    context = SpecContext(
        file_id=path.name.removesuffix(".spec.md"), declared_secrets=declared_secrets
    )
    return _validate(SpecFrontmatter, data, path, context), spec_hash(data)


def _spec_with_bindings(
    path: Path, frontmatter: SpecFrontmatter, hashed: str, config: ProjectConfig
) -> Spec:
    """The spec, with the bindings in `config` of the secrets `frontmatter`
    references, which _read_spec checked `config` declares."""
    referenced = {name for _, name in secret_references(frontmatter)}
    bindings = {name: b for name, b in config.secrets.items() if name in referenced}
    return Spec(path, frontmatter, hashed, bindings)


@dataclass(frozen=True)
class Project:
    """A spec root: its project config, and every spec below it by id."""

    root: Path
    config: ProjectConfig
    specs: Mapping[str, Spec]


def _subject_problems(
    config: ProjectConfig, specs: Mapping[str, Spec], path: Path
) -> list[str]:
    """Rows whose spec or expectation is absent from this project."""
    problems: list[str] = []
    for index, row in enumerate(config.subjects):
        prefix = f"{path}: subjects[{index}]"
        if row.spec not in specs:
            problems.append(f"{prefix}: no spec named {row.spec}")
        elif row.expect >= len(specs[row.spec].frontmatter.expect):
            count = len(specs[row.spec].frontmatter.expect)
            problems.append(
                f"{prefix}: {row.spec} has {count} expectations, "
                f"so it has no expect {row.expect}"
            )
    return problems


def load_project(spec_root: Path) -> Project:
    """The project whose config is `spec_root/config.yaml`, with every
    `*.spec.md` below it, in subdirectories too. Spec ids are unique in a
    project (DATA_MODEL §6). An invalid config doesn't stop the specs being
    read: every problem in every file is reported together. Each spec carries
    the config's bindings of exactly the test secrets it references."""
    problems: list[str] = []
    config_path = spec_root / "config.yaml"
    config: ProjectConfig | None
    try:
        config = _read_config(config_path)
    except SpecError as error:
        config = None
        problems.extend(error.problems)
    if config is not None:
        # A role that can't be routed leaves the secrets readable, so the specs'
        # references are still checked.
        problems.extend(_role_problems(config, config_path))
    # Unknown when the config is invalid, so references aren't checked then.
    declared = None if config is None else frozenset(config.secrets)
    paths: dict[str, Path] = {}
    specs: dict[str, Spec] = {}
    for path in sorted(p for p in spec_root.rglob("*.spec.md") if p.is_file()):
        try:
            frontmatter, hashed = _read_spec(path, declared)
        except SpecError as error:
            problems.extend(error.problems)
            continue
        first = paths.setdefault(frontmatter.id, path)
        if first != path:
            problems.append(
                f"{path}: id: '{frontmatter.id}' is already the id of {first}: "
                "spec ids are unique in a project"
            )
        elif config is not None:  # an invalid config has no bindings to give
            specs[frontmatter.id] = _spec_with_bindings(
                path, frontmatter, hashed, config
            )
    if config is not None:
        problems.extend(_subject_problems(config, specs, config_path))
    if config is None or problems:
        raise SpecError(problems)
    return Project(spec_root, config, specs)


def subject_contracts(config: ProjectConfig, spec_id: str) -> Mapping[int, Contract]:
    """The reviewed subjects of exactly one spec, by expectation index."""
    return {row.expect: row.contract for row in config.subjects if row.spec == spec_id}


def contracts_fingerprint(config: ProjectConfig, spec_id: str) -> str:
    """The canonical hash of this spec's sorted, normalized rows (ADR-0025)."""
    return canonical_hash(
        [
            {"expect": expect, **contract.model_dump()}
            for expect, contract in sorted(subject_contracts(config, spec_id).items())
        ]
    )


def contract_problems(script: CompiledScript, config: ProjectConfig) -> list[str]:
    """Why replay lacks agreement with reviewed subjects (ADR-0025).

    This format cannot carry a target contract, so every listed spec fails
    closed. A shared semantic meaning is governed wherever it is targeted.
    """
    problems: list[str] = []
    if script.compiled_by.subject_contracts != contracts_fingerprint(
        config, script.spec_id
    ):
        problems.append("compiled under other subject contracts: explore it again")
    for expect in subject_contracts(config, script.spec_id):
        names = {
            assertion.target
            for assertion in script.assertions
            if assertion.expect_index == expect
            and isinstance(assertion, TextInTarget | NotVisible | VisibleUnoccluded)
        }
        if not names:
            problems.append(
                f"subjects {script.spec_id} expect {expect}: no target carries its subject contract"
            )
            continue
        meanings = {
            script.targets[name].semantic for name in names if name in script.targets
        }
        governed = names | {
            name
            for name, target in script.targets.items()
            if target.semantic in meanings
        }
        problems.extend(
            f"targets.{name}: lacks subject contract for {script.spec_id} expect {expect}"
            for name in sorted(governed)
        )
    return problems


class _JsonObject(dict[str, object]):
    """A JSON object as `json.loads` builds it, keeping its last value for a
    key, and the keys the text wrote more than once, in order."""

    repeated: tuple[str, ...] = ()

    @classmethod
    def of(cls, members: list[tuple[str, object]]) -> Self:
        """`json.loads`'s `object_pairs_hook`: the object from its members,
        as the text wrote them."""
        found = cls(members)
        seen: set[str] = set()
        repeated: dict[str, None] = {}
        for key, _ in members:
            if key in seen:
                repeated[key] = None
            seen.add(key)
        found.repeated = tuple(repeated)
        return found


# Where a JSON value is: its parent's place and its own key or index, or None
# at the top. Linked, so walking deeper copies nothing.
type _Place = tuple[_Place, int | str] | None

# The most objects and arrays the walk below reads nested in one another:
# more than any compiled script needs, and than pydantic reads (about 200), so
# a deeper file is refused before walking it costs more.
_DEEPEST_JSON = 256


def _spelled(place: _Place) -> tuple[int | str, ...]:
    """The keys and indexes that lead from the top to `place`."""
    keys: list[int | str] = []
    while place is not None:
        place, key = place
        keys.append(key)
    return tuple(reversed(keys))


def _repeated_keys(text: str, path: Path) -> list[tuple[int | str, ...]]:
    """Where `text`, which must be JSON, writes a key more than once: each
    object's repeats, objects in the order the text opens them. A reader keeps
    the last value, so a repeat could hide a lowered `side_effect` (ADR-0025's
    2026-10-02 amendment)."""
    too_deep = SpecError([f"{path}: nested more than {_DEEPEST_JSON} levels deep"])
    try:
        parsed = json.loads(text, object_pairs_hook=_JsonObject.of)
    except json.JSONDecodeError as error:
        raise SpecError(
            [f"{path}: line {error.lineno}: not JSON: {error.msg}"]
        ) from None
    except ValueError as error:
        # Well-formed JSON with a value json won't convert: an integer longer
        # than Python reads (sys.int_info.default_max_str_digits).
        raise SpecError([f"{path}: not JSON: {error}"]) from None
    except RecursionError:
        raise too_deep from None
    found: list[tuple[int | str, ...]] = []
    # Walked with a stack, not recursion, which deep JSON would exhaust.
    # Children go on reversed, so they come off in the text's order.
    stack: list[tuple[object, int, _Place]] = [(parsed, 0, None)]
    while stack:
        value, depth, place = stack.pop()
        if isinstance(value, _JsonObject):
            if value.repeated:
                where = _spelled(place)
                found.extend((*where, key) for key in value.repeated)
            children: list[tuple[int | str, object]] = list(value.items())
        elif isinstance(value, list):
            children = list(enumerate(value))
        else:
            continue
        if depth == _DEEPEST_JSON:  # the top value is level 1
            raise too_deep
        stack.extend(
            (child, depth + 1, (place, key)) for key, child in reversed(children)
        )
    return found


def _defined_twice(where: str, field: str, values: Sequence[object]) -> Iterator[str]:
    """A problem for each of `values`, the `field` of each entry in `where`,
    that an earlier entry already defines."""
    first: dict[object, int] = {}
    for index, value in enumerate(values):
        earlier = first.setdefault(value, index)
        if earlier != index:
            yield (
                f"{where}[{index}].{field}: {value!r} is already the {field} of "
                f"{where}[{earlier}]"
            )


def _step_names(script: CompiledScript, config: ProjectConfig) -> Iterator[str]:
    """Names the steps use that nothing defines: a target, a secret the
    project config doesn't declare, or a required condition."""
    conditions = {condition.id for condition in script.coverage.requires}
    for index, step in enumerate(script.steps):
        where = f"steps[{index}]"
        match step:
            case (
                Click(target=target)
                | Fill(target=target)
                | FillSecret(target=target)
                | Select(target=target)
            ) if target not in script.targets:
                yield f"{where}.target: {target!r} names no target in targets"
        if isinstance(step, FillSecret) and step.secret not in config.secrets:
            yield (
                f"{where}.secret: secret {step.secret} is not declared in the "
                "project config's secrets"
            )
        for position, condition in enumerate(step.satisfies):
            if condition not in conditions:
                yield (
                    f"{where}.satisfies[{position}]: {condition!r} names no "
                    "condition in coverage.requires"
                )


def _assertion_names(script: CompiledScript) -> Iterator[str]:
    """Names the assertions use that nothing defines: a target, a probe or an
    expectation."""
    expectations = {each.expect_index for each in script.coverage.expectations}
    for index, assertion in enumerate(script.assertions):
        where = f"assertions[{index}]"
        match assertion:
            case (
                TextInTarget(target=target)
                | NotVisible(target=target)
                | VisibleUnoccluded(target=target)
            ) if target not in script.targets:
                yield f"{where}.target: {target!r} names no target in targets"
            case ProbeEqualsBaseline(probe=probe) if (
                probe not in script.probe_baselines
            ):
                yield f"{where}.probe: {probe!r} names no probe in probe_baselines"
        if assertion.expect_index not in expectations:
            yield (
                f"{where}.expect_index: {assertion.expect_index} names no "
                "expectation in coverage.expectations"
            )


def _unscoped_negative_checks(script: CompiledScript) -> Iterator[str]:
    """A locator with no scope on a not_visible check's target. Unscoped, a
    negative check finds its element absent on any page without a match,
    such as a wrong page or an app's 404, so it would pass vacuously: a
    broken script, not drift (#46, #52)."""
    for index, assertion in enumerate(script.assertions):
        if not isinstance(assertion, NotVisible):
            continue
        target = script.targets.get(assertion.target)
        for position, locator in enumerate(target.locators if target else ()):
            if locator.scope is None:
                yield (
                    f"targets.{assertion.target}.locators[{position}]: has no "
                    f"scope, but assertions[{index}] ({assertion.id}) checks that "
                    "this target is not visible: without a scope, its absence "
                    "passes on any page that lacks a match, such as a wrong page "
                    "or a 404"
                )


def _name_problems(script: CompiledScript, config: ProjectConfig) -> Iterator[str]:
    """How the parts of `script`, which the format accepts, fail to name each
    other (DATA_MODEL §7, "Checked by the loader"), each `<key>: <problem>`:
    names defined twice, then names used that nothing defines."""
    coverage = script.coverage
    yield from _defined_twice("assertions", "id", [a.id for a in script.assertions])
    yield from _defined_twice(
        "coverage.expectations",
        "expect_index",
        [each.expect_index for each in coverage.expectations],
    )
    yield from _defined_twice(
        "coverage.requires", "id", [c.id for c in coverage.requires]
    )
    yield from _defined_twice("steps", "seq", [step.seq for step in script.steps])
    yield from _step_names(script, config)
    yield from _assertion_names(script)
    ids = {assertion.id for assertion in script.assertions}
    for index, expectation in enumerate(coverage.expectations):
        for position, name in enumerate(expectation.assertions):
            if name not in ids:
                yield (
                    f"coverage.expectations[{index}].assertions[{position}]: "
                    f"{name!r} names no assertion in assertions"
                )
    seqs = {step.seq for step in script.steps}
    for name, baseline in script.probe_baselines.items():
        if baseline.capture_before_seq not in seqs:
            yield (
                f"probe_baselines.{name}.capture_before_seq: "
                f"{baseline.capture_before_seq} names no step's seq"
            )
    yield from _unscoped_negative_checks(script)


def load_compiled(path: Path, config: ProjectConfig) -> CompiledScript:
    """The compiled script at `path`, read strictly (DATA_MODEL §7, "Reading a
    compiled script"): JSON that repeats no key, then the same text validated
    in JSON mode, where `compiled_at` may be a string, then its parts checked
    against each other and the secrets `config` declares. Every problem is
    reported at once, as a SpecError."""
    return parse_compiled(_read_text(path), config, source=path)


def parse_compiled(text: str, config: ProjectConfig, *, source: Path) -> CompiledScript:
    """Validate compiled JSON and its cross-field rules without file I/O.
    `source` identifies every SpecError problem, as in `load_compiled`."""
    problems = [
        f"{source}: {_key(where)}: the key appears more than once, and a reader "
        "keeps only its last value, so write it once"
        for where in _repeated_keys(text, source)
    ]
    try:
        script = CompiledScript.model_validate_json(text)
    except ValidationError as error:
        raise SpecError([*problems, *_problems(error, source)]) from None
    problems.extend(
        f"{source}: {problem}" for problem in _name_problems(script, config)
    )
    if problems:
        raise SpecError(problems)
    return script


def effective_browser(spec: Spec, config: ProjectConfig) -> BrowserSettings:
    """The pinned settings, overridden by the project's, then by the spec's
    (ADR-0025)."""
    settings = BrowserSettings().model_dump()
    for overrides in (config.browser, spec.frontmatter.browser):
        settings |= overrides.model_dump(exclude_none=True)
    return BrowserSettings.model_validate(settings)


def start_origin(url: str | None, config: ProjectConfig) -> str:
    """The run's start origin: the invocation's `--url` when it gives one,
    otherwise the project's base_url (DATA_MODEL §9). Never the spec's."""
    if url is None:
        if config.base_url is None:
            raise SpecError(
                ["no start origin: pass --url, or set base_url in the project config"]
            )
        return config.base_url
    try:
        return parse_origin(url)
    except ValueError as error:
        raise SpecError([f"--url: {error}"]) from None


def allowed_origins(spec: Spec, start: str) -> tuple[str, ...]:
    """The origins the run may navigate to and act on: `start`, the run's
    start origin, then the spec's (ADR-0026)."""
    return tuple(dict.fromkeys((start, *spec.frontmatter.allowed_origins)))


def path_on_origin(start: str, path: str) -> str:
    """`path`, a start_url or a compiled navigate's path, on `start`, the run's
    start origin as start_origin gives it: joined as text, never
    percent-decoded or resolved (ADR-0026's start URL amendment; DATA_MODEL
    §9)."""
    return start + path


def probe_url(spec: Spec, name: str, start: str) -> str:
    """The declared probe's validated GET path on this run's start origin."""
    path = spec.frontmatter.preconditions.probes[name].removeprefix("GET ")
    return path_on_origin(start, path)


def start_url(spec: Spec, start: str) -> str:
    """Where the run's first navigation goes: `start`, the run's start origin
    as start_origin gives it, followed by the spec's start_url as written
    (`path_on_origin`)."""
    return path_on_origin(start, spec.frontmatter.preconditions.start_url)


@dataclass(frozen=True)
class SecretDestination:
    """Where the browser may fill one test secret in this run."""

    origins: tuple[str, ...]
    field: SecretField


def secret_destinations(spec: Spec, start: str) -> dict[str, SecretDestination]:
    """For each test secret `spec` references, its binding in
    `spec.secret_bindings` intersected with the run's allowed origins
    (ADR-0026), where `start` is the run's start origin. A bound origin the
    run doesn't allow is an error, never dropped: the secret could otherwise
    be left with nowhere to go. fill_secret takes each secret's origins and
    field from here, never from `spec.secret_bindings`."""
    allowed = allowed_origins(spec, start)
    destinations: dict[str, SecretDestination] = {}
    problems: list[str] = []
    for key, name in secret_references(spec.frontmatter):
        if name in destinations:
            continue
        binding = spec.secret_bindings[name]
        origins = tuple(
            dict.fromkeys(start if o == "start" else o for o in binding.origins)
        )
        problems.extend(
            f"{spec.path}: {key}: secret {name} is bound to {origin}, which is not one of "
            f"this run's allowed origins: {', '.join(allowed)}"
            for origin in origins
            if origin not in allowed
        )
        destinations[name] = SecretDestination(origins, binding.field)
    if problems:
        raise SpecError(problems)
    return destinations
