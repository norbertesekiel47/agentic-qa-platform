"""Where in the page a test secret may go (ADR-0026, Test secrets; SECURITY
§5): whether an element is the field a secret's binding names, and the
refusal `BrowserSession.fill_secret` raises when it isn't, or when the page
or the field's frame isn't where the binding allows."""

import re

from aqa_core.config import SecretField
from aqa_core.text import normalize
from playwright.async_api import ElementHandle, Frame

from aqa_runner.locators import name_pattern

# The fields a `password` binding names: <input type="password">, whose type
# HTML reads case-insensitively, as the css engine's `i` flag does.
CONCEALED_INPUTS = 'css=input[type="password" i]'

# Whether `element` is one of `found`, compared as the same node
# (`field_matches` says where this runs, and what it guards).
AMONG = """(element, found) => {
    for (let i = 0; i < found.length; i++) if (found[i] === element) return true;
    return false;
}"""


class SecretRefusedError(Exception):
    """fill_secret refused to fill a test secret where its binding doesn't
    allow it: not a policy event, since the page is on an allowed origin, but
    the wrong page or field for this secret. Nothing was filled, and the
    message names the secret, never its value."""


def describe_field(field: SecretField) -> str:
    """The field a binding names, as a refusal says it."""
    if field == "password":
        return 'an <input type="password">'
    return f'the {field.role} named "{field.name}"'


async def field_matches(
    frame: Frame, element: ElementHandle, field: SecretField
) -> bool:
    """Whether `element`, in `frame`, is the field a binding names: an
    `<input type="password">` (`CONCEALED_INPUTS`), or an element whose role
    and accessible name Playwright computes as the binding's, the name
    compared normalized, as a role locator's is (DATA_MODEL §7).

    The fields are found in Playwright's utility world, which the page's
    scripts can't reach: `element_handles` resolves its selector there,
    unlike `evaluate_all`, which resolves it in the page's own world
    (Playwright 1.63's `FrameSelectors._callOnSelectorInternal` and
    `queryArrayInMainWorld`). Whether `element` is one of them is decided in
    the page's own world, where nothing can redefine `===` but the list
    arrives through the page's scripts: so this keeps a secret from a field
    the agent or a script chose wrongly, not from the page, which is on one
    of the secret's destinations."""
    if field == "password":
        candidates = frame.locator(CONCEALED_INPUTS)
    else:
        name = normalize(field.name)
        if not name:
            return False  # a name that compares as empty names no field
        # https://playwright.dev/python/docs/api/class-frame#frame-get-by-role
        candidates = frame.get_by_role(field.role, name=re.compile(name_pattern(name)))
    # https://playwright.dev/python/docs/api/class-locator#locator-element-handles
    found = await candidates.element_handles()
    try:
        return bool(await element.evaluate(AMONG, found))
    finally:
        for handle in found:
            await handle.dispose()
