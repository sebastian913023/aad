from __future__ import annotations

import re

from aad.web import render_app
from aad.web_quote import QUOTE_JS


def test_quote_tab_is_wired_into_the_shell():
    html = render_app()
    assert 'data-tab="quote"' in html
    assert 'id="panel-quote"' in html
    assert QUOTE_JS.strip() in html


def test_every_id_the_quote_script_reads_exists_in_the_page():
    html = render_app()
    page_ids = set(re.findall(r'id="([^"]+)"', html))
    script_ids = set(re.findall(r'\$\("#(quote-[a-z-]+)"\)', QUOTE_JS))
    script_ids |= set(re.findall(r'num\("(quote-[a-z-]+)"\)', QUOTE_JS))
    missing = sorted(i for i in script_ids if i not in page_ids)
    assert not missing, f"quote script reads ids that are not in the page: {missing}"


def test_cited_sources_are_only_attached_by_a_lookup_click():
    # Preset hours are labelled "house preset". A `{ label: ... }` (cited) source may only
    # be built in the two "Use" click handlers, never when a line is created.
    assert QUOTE_JS.count("hoursSource = {") == 1
    assert QUOTE_JS.count("partsSource = {") == 1
