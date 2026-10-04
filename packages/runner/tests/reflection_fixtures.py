from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from packages.runner.tests import document_fixtures
from packages.runner.tests.document_fixtures import Sites, serving_sites

REFLECT = """<label>Password <input id="password" type="password"></label>
<p>Allowed text stays visible</p><main id="seen"></main><button id="named">Name</button>
<p id="upper" style="text-transform: uppercase"></p>
<p id="capital" style="text-transform: capitalize"></p>
<script>
password.addEventListener('input', () => {
    const value = password.value;
    const forms = [value, encodeURIComponent(value), JSON.stringify(value)];
    const utf8 = String.fromCharCode(...new TextEncoder().encode(value));
    for (const raw of [utf8, value].filter(s => [...s].every(c => c.charCodeAt(0) < 256))) {
        for (let k = 0; k < 3; k++) {
            const encoded = btoa('x'.repeat(k) + raw + 'tail');
            forms.push(encoded, encoded.replaceAll('+', '-').replaceAll('/', '_'), encodeURIComponent(encoded));
        }
    }
    seen.textContent = forms.join(' | ');
    named.setAttribute('aria-label', value);
    upper.textContent = value;
    capital.textContent = value;
    history.replaceState(null, '', '?value=' + encodeURIComponent(value));
});
</script>"""
CANARY = "<p>fake-off-origin-canary</p><button>Forbidden button</button>"


@contextmanager
def reflecting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sites]:
    original = document_fixtures.page

    def page(sites: Sites, path: str, query: dict[str, list[str]]) -> str | None:
        if path == "/reflect":
            return REFLECT + f'<iframe src="{sites.cdn}/canary"></iframe>'
        if path == "/canary":
            return CANARY
        return original(sites, path, query)

    monkeypatch.setattr(document_fixtures, "page", page)
    with serving_sites(monkeypatch) as sites:
        yield sites
