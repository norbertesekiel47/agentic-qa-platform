"""Pages the screenshot tests load (#171, ADR-0034): the hostile pages the
reviewers and the prototype aimed at a capture, and the owner's tricks,
each drawing the field that gets the bound value in pure red, which
nothing else on the page is; and the oracle that counts red pixels in a
saved PNG. Imported by its path, as pytest names the runner's test modules
(TESTING.md §1, Shared egress fixtures)."""

import base64
import json
from dataclasses import dataclass
from typing import Literal

from aqa_core.config import RoleField
from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret
from playwright.async_api import Page, Route
from pydantic import SecretStr

# The values the tests bind, each saying it is fake (AGENTS.md rule 9).
RED_VALUE = "Zq7fakeSECx"
SPECIALS_VALUE = 'Zq7&<"fake'
TRIGGER_VALUE = "trigger-fake-1"
# What a control types where the bound value goes: no run of four of its
# characters is in a bound value.
UNBOUND = "Plain Unbound"


def bound(origin: str) -> list[BoundSecret]:
    """The secrets a test binds on `origin`: the red value for the field
    named Secret, and for password fields, the same value with HTML's
    special characters, and a trigger whose fill starts the render."""

    def at(
        name: str, value: str, field: RoleField | Literal["password"]
    ) -> BoundSecret:
        return BoundSecret(name, SecretStr(value), SecretDestination((origin,), field))

    secret = RoleField(role="textbox", name="Secret")
    return [
        at("RED", RED_VALUE, secret),
        at("RED_PASSWORD", RED_VALUE, "password"),
        at("SPECIALS", SPECIALS_VALUE, secret),
        at("TRIGGER", TRIGGER_VALUE, RoleField(role="textbox", name="Trigger")),
    ]


RED = (
    "font:bold 48px sans-serif;color:#f00;width:500px;height:70px;border:0;"
    "padding:0;outline:none;caret-color:transparent;background:#fff;"
    "-webkit-text-security:none"
)
STYLE = f"<style>body{{margin:0;background:#fff}}input,textarea{{{RED}}}</style>"
FIELD = "<input id=f aria-label=Secret>"
EDITABLE = "<div id=ce contenteditable role=textbox aria-label=Secret></div>"
EDITABLE_STYLE = f"<style>body{{margin:0;background:#fff}}#ce{{{RED}}}</style>"
# Where the trigger's value goes: small, black and always in view.
TRIGGER = (
    '<input id=trigger aria-label=Trigger style="position:fixed;left:0;bottom:0;'
    'width:80px;height:20px;font:12px sans-serif;color:#000;border:1px solid #000">'
)
FRAME = f"<!doctype html>{STYLE}{FIELD}"
XHTML = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
    f'{STYLE}</head><body><input id="f" aria-label="Secret"/>'
    '<input id="trigger" aria-label="Trigger" style="position:fixed;left:0;bottom:0;'
    'width:80px;height:20px;font:12px sans-serif;color:#000;border:1px solid #000"/>'
    "</body></html>"
)

# Where a case's value goes: the field `#f`, the field `#f2`, `#f` in the
# frame `#fr`, the editable `#ce`, `#f` in a frame on another origin (by
# `fill`, since `fill_secret` never fills there), or nowhere but the page's
# own text (`VALUE` in its body).
type Into = Literal["field", "second", "frame", "editable", "cross-site", "page"]


@dataclass(frozen=True)
class Case:
    """A hostile page: its body, where the value goes, and the script that
    starts its attack once the value is in. A control runs `arm`
    too, unless `control` names what it runs instead, when the attack
    would hide the field there too. `binding` names the secret the value
    goes in as, and `partial` says its field takes only part of it.
    `withholds` says a capture may be withheld: the page changes its
    fields between the snapshot's two calls."""

    body: str
    into: Into = "field"
    arm: str | None = None
    control: str | Literal[True] | None = True
    binding: str = "RED"
    xhtml: bool = False
    partial: bool = False
    withholds: bool = False


CLONE_ON_SIGNAL = """<script>let c=null,armed=false;window.arm=()=>{c=document.getElementById('f').cloneNode();armed=true};
(function t(){const f=document.getElementById('f');if(armed&&f.value===''){armed=false;const shown=c;document.body.append(shown);c=shown.cloneNode();
setTimeout(()=>{shown.remove();setTimeout(()=>armed=true,300)},5)}requestAnimationFrame(t)})()</script>"""
WRITEBACK_ON_SIGNAL = """<script>let armed=false,v='';window.arm=()=>{v=document.getElementById('f').value;armed=true};
(function t(){const f=document.getElementById('f');if(armed&&f.value===''){armed=false;f.value=v;setTimeout(()=>{f.value='';setTimeout(()=>armed=true,300)},5)}requestAnimationFrame(t)})()</script>"""
DECOY_SWAP = """<script>let real=null,decoy=null,armed=false;window.arm=()=>{real=document.getElementById('f');decoy=document.createElement('input');decoy.value='x';decoy.style.color='#000';real.replaceWith(decoy);armed=true};
(function t(){if(armed&&decoy.value===''){armed=false;decoy.after(real);setTimeout(()=>{real.remove();setTimeout(()=>armed=true,300)},0)}requestAnimationFrame(t)})()</script>"""
WRITEBACK_ON_SELECTION = """<script>let armed=false,v='';window.arm=()=>{v=document.getElementById('f').value;armed=true};
document.addEventListener('selectionchange',()=>{const f=document.getElementById('f');if(!armed||f.value!=='')return;armed=false;f.value=v;setTimeout(()=>{f.value='';setTimeout(()=>armed=true,500)},50)},true)</script>"""
SCOPED_TRANSITION = """<style>#box{width:800px;height:280px;contain:layout}#box::view-transition-group(*){animation-duration:100s}
#box::view-transition-old(x){animation:none;transform:translateY(150px)}#box::view-transition-new(x){animation:none;opacity:0}#f{view-transition-name:x}
::view-transition-group(*){animation-duration:100s}::view-transition-old(x){animation:none;transform:translateY(150px)}::view-transition-new(x){animation:none;opacity:0}</style>"""
TRANSITION = """<style>::view-transition-group(*){animation-duration:100s}::view-transition-old(x){animation:none;transform:translateX(420px)}
::view-transition-new(x){animation:none;opacity:0}#VT{view-transition-name:x}</style>"""
HIDDEN_EDITABLE = """<style>body{margin:0;background:#fff}#ce{font:bold 48px sans-serif;color:#f00;width:500px;height:70px}#decoy{width:20px;height:10px;border:0;color:#000}</style>
<div id=ce contenteditable role=textbox aria-label=Secret></div><input id=decoy value=x><script>let armed=false;window.arm=()=>{document.getElementById('ce').style.display='none';armed=true};
(function t(){const d=document.getElementById('decoy'),ce=document.getElementById('ce');if(armed&&d.value===''){armed=false;ce.style.display='block';setTimeout(()=>{ce.style.display='none';setTimeout(()=>armed=true,300)},0)}requestAnimationFrame(t)})()</script>"""
ALTERNATE = """<script>let armed=false,on=false;window.arm=()=>{armed=true};(function t(){if(armed){on=!on;f.value=on?f.dataset.v:''}requestAnimationFrame(t)})()</script>"""
REWRITE = """<script>let armed=false;window.arm=()=>{armed=true};(function t(){if(armed&&f.value!==f.dataset.v)f.value=f.dataset.v;requestAnimationFrame(t)})()</script>"""
M1_ATTACKS = """<style>x-pw-glass{display:none!important}</style><script>new MutationObserver(r=>r.forEach(m=>m.addedNodes.forEach(n=>n.remove&&n.remove())))
.observe(document.documentElement,{childList:true,subtree:true})</script>"""
# A shadow root's own <style> isn't in an MHTML snapshot, so the field is
# styled inline.
CLOSED_SHADOW = f"""<div id=h></div><script>const r=h.attachShadow({{mode:'closed'}});r.innerHTML='<input id=g style="{RED}">';
window.arm=()=>{{r.querySelector('input').value=f.value;f.remove()}}</script>"""
MOVING = """<style>@keyframes j{from{margin-left:0}to{margin-left:400px}}#f{animation:j 200ms steps(2) infinite}</style>
<script>(function t(){f.style.marginTop=(performance.now()%2>1?'60px':'0');requestAnimationFrame(t)})()</script>"""
SCROLL = """<div style='height:3000px'></div><script>window.arm=()=>{(function t(){scrollTo(0,(performance.now()|0)%2?1000:0);requestAnimationFrame(t)})()}</script>"""
RESTYLED = "<style>#f{-webkit-box-reflect:right 100px;filter:drop-shadow(380px 60px 0 #00f);font-size:60px}</style>"
BLIND_CLONE = """<script>let c=null;window.arm=()=>{c=document.getElementById('f').cloneNode();c.style.top='0';c.style.position='absolute';
(function t(){c.isConnected?c.remove():document.body.append(c);requestAnimationFrame(t)})();setInterval(()=>{c.isConnected?c.remove():document.body.append(c)},0)}</script>"""
BLIND_FLIP = """<script>window.arm=()=>{setInterval(()=>{f.type=f.type==='submit'?'text':'submit'},0)}</script>"""
BLIND_EDITABLE = """<script>window.arm=()=>{setInterval(()=>{ce.style.display=ce.style.display==='none'?'block':'none'},0)}</script>"""
HANG = """<script>window.arm=()=>{setInterval(()=>{const e=Date.now()+1500;while(Date.now()<e){}},2000)}</script>"""
SPLIT = """<script>window.arm=()=>{const t=ce.firstChild;t.splitText(4);t.after(document.createElement('b'))}</script>"""
SPLIT_COMMENT = """<script>window.arm=()=>{const t=ce.firstChild;t.splitText(4);t.after(document.createComment(''))}</script>"""
CANVAS_DRAW = """<canvas id=c width=500 height=70></canvas><input id=f2 aria-label=Secret style='position:absolute;top:400px'>
<script>window.arm=()=>{const x=c.getContext('2d');x.font='bold 48px sans-serif';x.fillStyle='#f00';x.fillText(f2.value,0,50)}</script>"""
BLOB_IMAGE = """<img id=i><input id=f2 aria-label=Secret style='position:absolute;top:400px'><script>window.arm=()=>{const c=document.createElement('canvas');
c.width=500;c.height=70;const x=c.getContext('2d');x.font='bold 48px sans-serif';x.fillStyle='#f00';x.fillText(f2.value,0,50);
c.toBlob(b=>document.getElementById('i').src=URL.createObjectURL(b))}</script>"""
CSS_ESCAPE = "<style>#p::after{content:'VALUE';font:bold 48px sans-serif;color:#f00}</style><p id=p></p>"
FRACTIONAL = "<style>#f{position:absolute;left:10.37px;top:20.61px;transform:translate(0.33px,0.5px) scale(1.013)}</style>"

CASES = {
    # The interrogate reviewers' attacks on value removal (scratchpad
    # interrogate-171; reviews/171-interrogate.md).
    "flip-submit": Case(STYLE + FIELD, arm="f.type='submit'"),
    "flip-button": Case(STYLE + FIELD, arm="f.type='button'"),
    "flip-reset": Case(STYLE + FIELD, arm="f.type='reset'"),
    "alternate-each-frame": Case(
        STYLE + FIELD + ALTERNATE, arm="f.dataset.v=VALUE;arm()", control=None
    ),
    "clone-on-signal": Case(STYLE + FIELD + CLONE_ON_SIGNAL, arm="arm()"),
    "decoy-swap": Case(STYLE + FIELD + DECOY_SWAP, arm="arm()", control=None),
    "write-back-on-signal": Case(STYLE + FIELD + WRITEBACK_ON_SIGNAL, arm="arm()"),
    "write-back-on-selection": Case(
        STYLE + FIELD + WRITEBACK_ON_SELECTION, arm="arm()"
    ),
    "element-scoped-transition": Case(
        STYLE + SCOPED_TRANSITION + "<div id=box>" + FIELD + "</div>",
        arm="document.getElementById('box').startViewTransition(()=>{})",
    ),
    "ancestor-frame-transition": Case(
        STYLE
        + TRANSITION.replace("#VT", "#fr")
        + "<iframe id=fr src=/shot/frame style='border:0;width:600px;height:120px'></iframe>",
        into="frame",
        arm="document.startViewTransition(()=>{})",
    ),
    "hidden-editable": Case(
        HIDDEN_EDITABLE, into="editable", arm="arm()", control=None
    ),
    "xhtml": Case(XHTML, xhtml=True),
    "xhtml-frame": Case(
        STYLE
        + "<iframe id=fr src=/shot/x.xhtml style='border:0;width:600px;height:120px'></iframe>",
        into="frame",
    ),
    "adopted-into-a-scriptless-frame": Case(
        STYLE
        + FIELD
        + """<iframe id=fr sandbox="allow-same-origin" style='border:0;width:600px;height:120px'
        srcdoc="<style>body{margin:0}input{font:bold 48px sans-serif;color:#f00;width:500px;border:0;background:#fff}</style><p>x</p>"></iframe>""",
        arm="const d=document.getElementById('fr').contentDocument;d.body.append(d.adoptNode(document.getElementById('f')))",
    ),
    "busy-main-thread": Case(STYLE + FIELD + HANG, arm="arm()"),
    # The prototype's attacks on a snapshot and its scrub (scratchpad
    # proto-171; reviews/171-prototype.md).
    "blind-clone": Case(
        STYLE
        + FIELD.replace(">", " style='position:absolute;top:200px'>")
        + BLIND_CLONE,
        arm="arm()",
        control=None,
        withholds=True,
    ),
    "blind-type-flip": Case(
        STYLE + FIELD + BLIND_FLIP, arm="arm()", control=None, withholds=True
    ),
    "blind-editable": Case(
        EDITABLE_STYLE + EDITABLE + BLIND_EDITABLE,
        into="editable",
        arm="arm()",
        control=None,
    ),
    "editable-split-by-element": Case(
        EDITABLE_STYLE + EDITABLE + SPLIT, into="editable", arm="arm()"
    ),
    "editable-split-by-comment": Case(
        EDITABLE_STYLE + EDITABLE + SPLIT_COMMENT, into="editable", arm="arm()"
    ),
    "html-specials-then-flip": Case(
        STYLE + FIELD, arm="f.type='submit'", binding="SPECIALS"
    ),
    "partial-then-flip": Case(
        STYLE + FIELD.replace(">", " maxlength=6>"), arm="f.type='submit'", partial=True
    ),
    "css-escape": Case(CSS_ESCAPE, into="page"),
    "canvas": Case(STYLE + CANVAS_DRAW, into="second", arm="arm()"),
    "blob-image": Case(STYLE + BLOB_IMAGE, into="second", arm="arm()"),
    # The owner's tricks (plan-v1-value-removal.md), the first two M1's
    # measured attacks on Playwright's mask (ADR-0033).
    "plain": Case(STYLE + FIELD),
    # After the field: the observer removes every node added after it.
    "m1-overlay-attacks": Case(STYLE + FIELD + M1_ATTACKS),
    "closed-shadow-root": Case(STYLE + FIELD + CLOSED_SHADOW, arm="arm()"),
    "same-origin-frame": Case(
        STYLE
        + "<iframe id=fr src=/shot/frame style='border:0;width:600px;height:120px'></iframe>",
        into="frame",
    ),
    "cross-site-frame": Case(
        STYLE
        + "<iframe id=fr src=OTHER/shot/frame style='border:0;width:600px;height:120px'></iframe>",
        into="cross-site",
    ),
    "moving": Case(STYLE + MOVING + FIELD),
    "scroll-jitter": Case(
        STYLE + SCROLL + FIELD.replace(">", " style='position:absolute;top:1000px'>"),
        arm="arm()",
        control="scrollTo(0,1000)",
    ),
    "restyled-password": Case(
        STYLE + RESTYLED + "<input id=f type=password>", binding="RED_PASSWORD"
    ),
    "clone": Case(
        STYLE
        + FIELD
        + "<script>window.arm=()=>{const c=f.cloneNode();c.value=f.value;document.body.append(c)}</script>",
        arm="arm()",
    ),
    "rewrite-each-frame": Case(STYLE + FIELD + REWRITE, arm="f.dataset.v=VALUE;arm()"),
    "view-transition": Case(
        STYLE + TRANSITION.replace("#VT", "#f") + FIELD,
        arm="document.startViewTransition(()=>{})",
    ),
    "partial-fill": Case(STYLE + FIELD.replace(">", " maxlength=6>"), partial=True),
    "editable": Case(EDITABLE_STYLE + EDITABLE, into="editable"),
    "fractional-geometry": Case(STYLE + FRACTIONAL + FIELD),
}

# Cases also run at device scale 2.
SCALED = ("plain", "moving", "restyled-password", "fractional-geometry")


async def serve_cases(page: Page, other: str) -> dict[str, str]:
    """Serve every case's page at `/shot/<name>` on any origin, its frame
    page at `/shot/frame` and the XHTML frame at `/shot/x.xhtml`, by route
    on `page`. `VALUE` in a body is the returned mapping's "value", which a
    test sets before each load; `OTHER` is `other`, another allowed origin."""
    current = {"value": ""}

    async def fulfil(route: Route) -> None:
        name = route.request.url.split("/shot/", 1)[1]
        if name == "frame":
            body, kind = FRAME, "text/html"
        elif name == "x.xhtml" or CASES[name].xhtml:
            body, kind = (
                XHTML if name == "x.xhtml" else CASES[name].body,
                "application/xhtml+xml",
            )
        else:
            escaped = "".join(f"\\{ord(char):x} " for char in current["value"])
            body = (
                "<!doctype html><meta charset=utf-8>"
                + TRIGGER
                + CASES[name].body.replace("VALUE", escaped).replace("OTHER", other)
            )
            kind = "text/html"
        await route.fulfill(body=body, content_type=f"{kind}; charset=utf-8")

    await page.route("**/shot/**", fulfil)
    return current


def armed(script: str, value: str) -> str:
    """`script` with `VALUE` as a JavaScript string of `value`."""
    return script.replace("VALUE", json.dumps(value))


# Counts pure red pixels in a PNG with the browser's own decoder.
RED_PIXELS = """async (data) => {
    const bitmap = await createImageBitmap(await (await fetch("data:image/png;base64," + data)).blob());
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const context = canvas.getContext("2d");
    context.drawImage(bitmap, 0, 0);
    const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
    let red = 0;
    for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i] >= 180 && pixels[i + 1] <= 80 && pixels[i + 2] <= 80) red++;
    }
    return red;
}"""


async def red_pixels(scratch: Page, png: bytes) -> int:
    """How many of the PNG's pixels are pure red (R at least 180, G and B
    at most 80), decoded in `scratch`, a page of its own."""
    return int(await scratch.evaluate(RED_PIXELS, base64.b64encode(png).decode()))
