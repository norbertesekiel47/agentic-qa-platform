import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from aqa_core.schema import Contract
from playwright.async_api import ElementHandle, Error, Page, Playwright
from playwright.async_api import Locator as PlaywrightLocator

type BindingMiss = Literal["no region", "outside region", "not the part", "not a leaf"]

ENGINE = "aqa-binding"
SCRIPT = """(() => {
    const held = new Map();
    // A form's named controls override DOM properties on the form itself, in
    // every world, so page nodes are read only through prototype natives. Each
    // is looked up on first use, which is in the utility world: the page's main
    // world evaluates this source too, and its globals are the page's to poison.
    const native = (type, name) => {
        let found;
        return (node, ...args) => {
            found ??= Object.getOwnPropertyDescriptor(globalThis[type].prototype, name);
            return found.get ? found.get.call(node) : found.value.apply(node, args);
        };
    };
    const ownerDocument = native("Node", "ownerDocument");
    const isConnected = native("Node", "isConnected"), contains = native("Node", "contains");
    const childNodes = native("Node", "childNodes"), nodeType = native("Node", "nodeType");
    const parentElement = native("Node", "parentElement");
    const shadowRoot = native("Element", "shadowRoot");
    const localName = native("Element", "localName");
    const children = native("Element", "children");
    const classList = native("Element", "classList");
    const childElementCount = native("Element", "childElementCount");
    const inElement = native("Element", "querySelectorAll");
    const checkVisibility = native("Element", "checkVisibility");
    const getBoundingClientRect = native("Element", "getBoundingClientRect");
    const inDocument = native("Document", "querySelectorAll");
    const inShadow = native("DocumentFragment", "querySelectorAll");
    const createRange = native("Document", "createRange");
    const count = (scope, css) => {
        const query = scope === document ? inDocument : inShadow;
        let n = query(scope, css).length;
        for (const e of query(scope, "*")) {
            const tree = shadowRoot(e); if (tree) n += count(tree, css);
        }
        return n;
    };
    const valid = (r, region, root) => r && isConnected(r) && ownerDocument(r) === document &&
        ownerDocument(root) === document && ![r, ...inElement(r, "*")].some(shadowRoot) &&
        count(document, region) === 1 && inDocument(document, region)[0] === r;
    const visible = (e) => {
        const style = getComputedStyle(e);
        if (style.display === "contents") return [...childNodes(e)].some((c) => {
            if (nodeType(c) === 1) return visible(c);
            if (nodeType(c) !== 3) return false;
            const range = createRange(document); range.selectNode(c);
            const box = range.getBoundingClientRect(); return box.width > 0 && box.height > 0;
        });
        if (!checkVisibility(e) || style.visibility !== "visible") return false;
        const box = getBoundingClientRect(e); return box.width > 0 && box.height > 0;
    };
    const stable = (s) => /^[A-Za-z][A-Za-z0-9_-]*$/.test(s) &&
        !/^(css|sc|jsx|emotion|svelte|ng)-/.test(s) &&
        !["active", "checked", "collapsed", "disabled", "expanded", "focus", "hidden",
          "hover", "open", "selected", "show"].includes(s) &&
        !s.split(/[-_]/).some((p) => p.length >= 5 && /[0-9]/.test(p) && /[A-Za-z]/.test(p));
    const classes = (e) => [...classList(e)].filter(stable).sort().join(" ");
    const copies = (e) => {
        const found = new Set([e]); const path = [localName(e)];
        let child = e;
        for (let n = 0, a = parentElement(e); a && localName(a) !== "body" &&
             localName(a) !== "html"; n++, child = a, a = parentElement(a)) {
            if (n === 16) return [e];
            if (path.some((tag) => tag.includes("-"))) {
                if (children(a).length > 64) return [e];
                for (const other of children(a)) {
                    if (other === child || localName(other) !== localName(child) ||
                        classes(other) === classes(child)) continue;
                    let matches = [other];
                    for (const tag of path.slice(1)) matches = matches.flatMap(
                        (node) => [...children(node)].filter((c) => localName(c) === tag));
                    for (const match of matches) found.add(match);
                    if (found.size > 8) return [e];
                }
            }
            path.unshift(localName(a));
        }
        return found.size > 1 ? [...found] : [];
    };
    const queryAll = (root, body) => {
        if (body === "copies") return copies(root);
        const colon = body.indexOf(":");
        const verb = body.slice(0, colon);
        const [token, region, part, leaf] = body.slice(colon + 1).split("|");
        if (verb === "hold") { held.set(token, root); return []; }
        if (verb === "drop") { held.delete(token); return []; }
        if (verb === "regions") return [...inDocument(document, token)];
        const r = held.get(token);
        if (!valid(r, region, root)) return [];
        if (verb === "in") return contains(r, root) ? [root] : [];
        const parts = [...inElement(r, part)];
        if (verb === "parts") return parts;
        if (verb === "absent") return parts.some(visible) ? [] : [r];
        return verb === "bound" && parts.length === 1 && parts[0] === root &&
            (leaf !== "leaf" || childElementCount(root) === 0) ? [root] : [];
    };
    return { query: (root, body) => queryAll(root, body)[0] || null, queryAll };
})()"""


async def register_binding_engine(playwright: Playwright) -> None:
    await playwright.selectors.register(ENGINE, script=SCRIPT, content_script=True)


async def _query(element: ElementHandle, body: str) -> list[ElementHandle]:
    try:
        return await element.query_selector_all(f"{ENGINE}={body}")
    except Error as error:
        if f'Unknown engine "{ENGINE}"' in str(error):
            raise RuntimeError(
                "the binding engine isn't registered: call register_identity_engine "
                "before the browser session opens"
            ) from None
        raise


async def _passes(element: ElementHandle, body: str) -> bool:
    found = await _query(element, body)
    for handle in found:
        await handle.dispose()
    return bool(found)


@dataclass(frozen=True)
class HeldRegion:
    """The original region and token; all predicates run in the utility world."""

    element: ElementHandle
    root: PlaywrightLocator
    contract: Contract
    token: str

    async def contains(self, element: ElementHandle) -> bool:
        return await _passes(element, f"in:{self.token}|{self.contract.region}")

    async def bound(self, element: ElementHandle, *, leaf: bool | None = None) -> bool:
        required = self.contract.leaf if leaf is None else leaf
        return await _passes(
            element, _body(self, "bound") + ("|leaf" if required else "|any")
        )

    async def absent(self) -> bool:
        return await _passes(self.element, _body(self, "absent"))

    async def miss(self, element: ElementHandle) -> BindingMiss:
        if not await self.contains(self.element):
            return "no region"
        if not await self.contains(element):
            return "outside region"
        if self.contract.leaf and await self.bound(element, leaf=False):
            return "not a leaf"
        return "not the part"


def _body(held: HeldRegion, verb: str) -> str:
    return f"{verb}:{held.token}|{held.contract.region}|{held.contract.part}"


@asynccontextmanager
async def held_region(
    page: Page, contract: Contract
) -> AsyncIterator[HeldRegion | None]:
    root = page.locator(f"css={contract.region}")
    regions = await root.element_handles()
    if len(regions) != 1:
        for element in regions:
            await element.dispose()
        yield None
        return
    [element] = regions
    token = secrets.token_hex(16)
    try:
        await _query(element, f"hold:{token}")
        yield HeldRegion(element, root, contract, token)
    except Exception as error:
        await _release(element, token, error)
        raise
    except BaseException as error:
        # A cancellation stays the raised exception, so asyncio still sees one.
        try:
            await _release(element, token, None)
        except BaseException as failure:
            raise error from failure
        raise
    await _release(element, token, None)


async def _release(element: ElementHandle, token: str, cause: Exception | None) -> None:
    try:
        await _query(element, f"drop:{token}")
    except BaseException as error:
        error.__cause__ = cause
        await _dispose(element, error)
        raise
    await _dispose(element, cause)


async def _dispose(element: ElementHandle, earlier: BaseException | None) -> None:
    try:
        await element.dispose()
    except BaseException as error:
        if isinstance(earlier, Exception | None):
            raise error from earlier
        # A cancelled drop stays raised with its own cause; this failure is its context.
        raise earlier from earlier.__cause__


@dataclass(frozen=True)
class Bind:
    """Admit the offered handle, without selecting or returning another."""


@dataclass(frozen=True)
class Refused:
    """A fixed refusal, independent of page text."""

    reason: Literal[
        "region_absent",
        "region_ambiguous",
        "outside_region",
        "part_absent",
        "part_ambiguous",
        "not_the_part",
        "not_leaf",
        "unlisted_copies",
        "too_many",
    ]


async def binding_verdict(
    page: Page, element: ElementHandle, contract: Contract | None
) -> Bind | Refused:
    """Admit exactly this offered element through trusted predicates."""
    if contract is None:
        copies = await _query(element, "copies")
        try:
            return (
                Refused("too_many" if len(copies) == 1 else "unlisted_copies")
                if copies
                else Bind()
            )
        finally:
            for copy in copies:
                await copy.dispose()
    async with held_region(page, contract) as held:
        if held is None:
            regions = await _query(element, f"regions:{contract.region}")
            try:
                return Refused("region_absent" if not regions else "region_ambiguous")
            finally:
                for region in regions:
                    await region.dispose()
        if await held.bound(element):
            return Bind()
        return await _refusal(held, element)


async def _refusal(held: HeldRegion, element: ElementHandle) -> Refused:
    if not await held.contains(held.element):
        return Refused("region_ambiguous")
    if not await held.contains(element):
        return Refused("outside_region")
    parts = await _query(held.element, _body(held, "parts"))
    try:
        if len(parts) != 1:
            return Refused("part_absent" if not parts else "part_ambiguous")
        return Refused(
            "not_leaf" if await held.bound(element, leaf=False) else "not_the_part"
        )
    finally:
        for part in parts:
            await part.dispose()
