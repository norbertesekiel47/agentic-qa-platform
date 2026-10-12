"""Scripts the browser session runs inside a page's documents: whether a
frame's element is inside what a click lands in, or is transformed, and a
fill that never goes through the page's keyboard."""

# Whether `owner`, a frame's element, is inside what a click on `element`
# lands in, run in the element's own frame, where both are. That is the
# element Playwright's click targets, whose descendants its hit check
# accepts: the closest button or link, unless the element is a field (1.63's
# injected `retarget(node, "button-link")`). It climbs both trees from
# `owner`: the DOM's, through shadow roots to their hosts, and the rendered
# one through each slot a node is assigned to, which the hit check follows
# (1.63's `expectHitTarget`: `assignedSlot ?? parentElementOrShadowHost`).
CONTAINS = """(element, owner) => {
    const target =
        element.matches("input, textarea, select") || element.isContentEditable
            ? element
            : element.closest("button, [role=button], a, [role=link]") ?? element;
    const seen = new Set();
    const nodes = [owner];
    while (nodes.length > 0) {
        const node = nodes.pop();
        if (!node || seen.has(node)) continue;
        if (node === target) return true;
        seen.add(node);
        nodes.push(node.assignedSlot, node instanceof ShadowRoot ? node.host : node.parentNode);
    }
    return false;
}"""

# Whether a frame's element, or one around it, is transformed, which stops
# Playwright checking what a click in the frame hits (1.63's
# `describeIFrameStyle`, which climbs `parentElementOrShadowHost`).
TRANSFORMED = """(owner) => {
    for (let node = owner; node; ) {
        if (getComputedStyle(node).transform !== "none") return true;
        const parent = node.parentNode;
        node = parent instanceof ShadowRoot ? parent.host : node.parentElement;
    }
    return false;
}"""

# Fills a field with a value inside its own document, never through the
# page's keyboard, and says whether it then holds the value. Like
# Playwright's fill (1.63's injected script): text goes into the text kinds
# of <input>, <textarea> and contenteditable elements, replacing what they
# hold (execCommand's insertText, which fires `input`, though not
# `beforeinput` or key events); the date-like kinds of <input> take the
# value as set. A field whose page changes the value as it goes in (a mask)
# reads back otherwise, and counts as not filled.
FILL = """(element, value) => {
    const set = ["color", "date", "time", "datetime-local", "month", "range", "week"];
    const typed = ["", "email", "number", "password", "search", "tel", "text", "url"];
    if (element instanceof HTMLInputElement && set.includes(element.type)) {
        element.focus();
        element.value = value;
        element.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        return element.value === value;
    }
    if (element instanceof HTMLInputElement && typed.includes(element.type)) {
        element.focus();
        element.select();
    } else if (element instanceof HTMLTextAreaElement) {
        element.focus();
        element.select();
    } else if (element.isContentEditable) {
        element.focus();
        const all = document.createRange();
        all.selectNodeContents(element);
        getSelection().removeAllRanges();
        getSelection().addRange(all);
    } else {
        return false;
    }
    if (value === "") document.execCommand("delete");
    else document.execCommand("insertText", false, value);
    return (element.isContentEditable ? element.textContent : element.value) === value;
}"""
