"""
The interface is one HTML file with inline JS and no build step.

That is a deliberate choice -- it has to work on an isolated camera network
with no npm and still open in five years -- but it removes the safety net a
bundler would give you. **A single syntax error takes the whole interface
down silently**: the page still serves 200, the layout still renders, and
every control is simply absent because the script died before wiring
anything up. Nothing in an HTTP test notices.

That happened while adding the exposure panel. `\\'` was written where JS
needs `\\\\'`, the string terminated early, and the tab bar vanished
entirely. These tests exist so it cannot happen again unseen.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

HTML = (pathlib.Path(__file__).resolve().parents[1]
        / "scanner" / "gui" / "static" / "index.html")


def _inline_js() -> str:
    m = re.search(r"<script[^>]*>(.*)</script>", HTML.read_text(), re.S)
    assert m, "index.html has no inline <script> block"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not installed; JS cannot be parsed")
def test_inline_javascript_parses(tmp_path):
    """
    The check that would have caught the exposure-panel bug immediately.

    A parse failure here means every control in the interface is dead, not
    that one feature is broken.
    """
    js = tmp_path / "app.js"
    js.write_text(_inline_js())
    r = subprocess.run(["node", "--check", str(js)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"inline JS does not parse:\n{r.stderr}"


def test_every_tab_has_a_panel():
    """
    A tab whose panel is missing renders as a dead button: it highlights,
    and nothing appears. The TABS list and the data-t panels have to agree.
    """
    html = HTML.read_text()
    m = re.search(r"const TABS\s*=\s*\[(.*?)\];", html, re.S)
    assert m, "TABS list not found"
    tab_ids = re.findall(r"\['([a-z]+)'", m.group(1))
    assert tab_ids, "no tabs parsed"

    panels = set(re.findall(r'<div class="tab[^"]*" data-t="([a-z]+)"', html))
    missing = [t for t in tab_ids if t not in panels]
    assert not missing, f"tabs with no panel: {missing}"


def test_every_referenced_element_id_exists():
    """
    `$('#foo').onclick = ...` against an id that is not in the markup throws
    at load and kills everything after it -- the same silent-death mode as a
    syntax error.
    """
    html = HTML.read_text()
    ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
    referenced = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", html))
    missing = sorted(referenced - ids)
    assert not missing, f"JS references ids that do not exist: {missing}"


def test_exposure_panel_is_wired():
    """The panel added for guided exposure calibration is actually reachable."""
    html = HTML.read_text()
    assert 'data-t="expose"' in html
    assert "'expose'" in html
    for el in ("btnMeter", "btnMeterApply", "expOut", "expIso", "expAp"):
        assert f'id="{el}"' in html, f"{el} missing from the markup"
        assert f"#{el}" in html, f"{el} never referenced from the script"
