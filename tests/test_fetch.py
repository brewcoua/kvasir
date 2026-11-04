"""Extraction against saved pages: what must survive, what must go, and what is not a page."""

from __future__ import annotations

import contextlib
import re
import threading
import tomllib
from pathlib import Path

import httpx
import pytest

from kvasir.research import browser, fetch, text
from kvasir.research.fetch import FetchError, UnusablePage, extract, normalize, split
from kvasir.storm import runtime

PAGES = Path(__file__).parent / "fixtures" / "pages"
CATALOG = tomllib.loads((PAGES / "pages.toml").read_text())["page"]


def collapse(text):
    return " ".join(text.split())


def extraction_cases():
    return [
        pytest.param(page, check, id=f"{page['file']}-{check}")
        for page in CATALOG
        if page["kind"] == "article"
        for check in ("must", "must_not")
    ]


def unreadable_cases():
    return [pytest.param(p, id=p["file"]) for p in CATALOG if p["kind"] != "article"]


@pytest.mark.parametrize(("page", "check"), extraction_cases())
def test_extract_keeps_the_content_and_drops_the_furniture(page, check):
    text = collapse(extract((PAGES / page["file"]).read_text(encoding="utf-8")))

    if check == "must":
        assert [s for s in page["must"] if collapse(s) not in text] == []
    else:
        assert [s for s in page["must_not"] if collapse(s) in text] == []


def serve(monkeypatch, body, status=200):
    def stream(method, url, **kwargs):
        response = httpx.Response(
            status,
            headers={"content-type": "text/html"},
            content=body,
            request=httpx.Request(method, url),
        )
        return contextlib.nullcontext(response)

    monkeypatch.setattr(fetch.httpx, "stream", stream)


@pytest.mark.parametrize("page", unreadable_cases())
def test_a_shell_or_challenge_page_is_not_read_as_text(monkeypatch, page):
    serve(monkeypatch, (PAGES / page["file"]).read_bytes())

    with pytest.raises(UnusablePage) as raised:
        fetch.fetch_page(page["url"])
    assert raised.value.reason == ("shell" if page["kind"] == "shell" else "challenge")


def test_a_quote_from_a_table_row_or_a_list_item_is_still_quoted():
    page = (PAGES / "wiki_bisection.html").read_text(encoding="utf-8")
    lines = extract(page).splitlines()
    row = next(line for line in lines if line.startswith("|") and line.count("|") > 3)
    item = next(line for line in lines if line.startswith(("- ", "* ")))
    cells = [cell.strip() for cell in row.strip("|").split("|") if cell.strip()]

    assert text.quoted(" ".join(cells), extract(page))
    assert text.quoted(item[2:], extract(page))


def test_footnote_markers_are_removed():
    out = extract((PAGES / "wiki_kvasir.html").read_text(encoding="utf-8"))

    assert "<sup>" not in out
    assert not re.search(r"\\\[\w+\\\]", out)
    assert "Extremely wise, Kvasir traveled far and wide, teaching and spreading knowledge." in out


def test_a_bracketed_span_of_the_page_itself_is_kept():
    kvasir = extract((PAGES / "wiki_kvasir.html").read_text(encoding="utf-8"))
    bisection = extract((PAGES / "wiki_bisection.html").read_text(encoding="utf-8"))

    assert "ˈkwɑsez̠" in kvasir
    assert r"\[1, 2\]" in bisection


def test_the_cleaned_page_is_read_when_trafilatura_returns_little():
    html = "<body><nav>Home</nav><main><p>Short note.</p></main><footer>Contact</footer></body>"

    assert extract(html) == "Short note."


def serve_html(monkeypatch, body, url="https://example.org/a"):
    # Enough text that a short hand-written page is not mistaken for an empty shell.
    body += "<p>" + "padding " * 10 + "</p>"

    def stream(method, url, **kwargs):
        response = httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=body.encode(),
            request=httpx.Request(method, url),
        )
        return contextlib.nullcontext(response)

    monkeypatch.setattr(fetch.httpx, "stream", stream)
    return fetch.fetch_page(url)


def links_of(monkeypatch, body):
    return {link.url: link for link in serve_html(monkeypatch, body).links}


def regions(page):
    return {
        region: sum(link.region == region for link in page.links)
        for region in ("main", "related", "nav", "footer")
    }


def wiki(monkeypatch, file):
    entry = next(p for p in CATALOG if p["file"] == file)
    return serve_html(monkeypatch, (PAGES / file).read_text(encoding="utf-8"), entry["url"])


def test_a_see_also_link_is_related_and_a_menu_link_is_nav(monkeypatch):
    page = wiki(monkeypatch, "wiki_bisection.html")
    by_url = {link.url: link for link in page.links}

    assert by_url["https://en.wikipedia.org/wiki/Binary_search_algorithm"].region == "related"
    assert by_url["https://en.wikipedia.org/wiki/Main_Page"].region == "nav"
    assert by_url["https://en.wikipedia.org/wiki/Mathematics"].region == "main"
    assert page.title == "Bisection method - Wikipedia"
    assert "bisection method" in by_url["https://en.wikipedia.org/wiki/Mathematics"].context


def test_the_root_classes_of_wikipedia_do_not_make_every_link_nav(monkeypatch):
    page = wiki(monkeypatch, "wiki_kvasir.html")

    assert regions(page)["main"] > 0
    assert regions(page)["related"] > 0


def test_the_menu_and_the_footer_of_a_cookie_wall_page_are_marked(monkeypatch):
    page = serve_html(monkeypatch, (PAGES / "cookie_wall.html").read_text(encoding="utf-8"))
    by_anchor = {link.anchor: link.region for link in page.links}

    assert by_anchor["Space"] == "nav"
    assert by_anchor["Privacy policy"] == "footer"
    assert "Sign in" in by_anchor


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://WWW.Example.com/a", "https://example.com/a"),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/docs/index.html", "https://example.com/docs"),
        ("https://example.com/a?utm_source=x&id=2", "https://example.com/a?id=2"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/#/page/2", "https://example.com#/page/2"),
        ("https://example.com/app#!/page", "https://example.com/app#!/page"),
    ],
)
def test_normalize_collapses_the_spellings_of_one_page(url, expected):
    assert normalize(url) == expected


def test_relative_links_resolve_against_the_final_url(monkeypatch):
    links = links_of(monkeypatch, '<body><p><a href="../b/c">C</a> <a href="/d">D</a></p></body>')

    assert set(links) == {"https://example.org/b/c", "https://example.org/d"}


def test_a_base_element_changes_what_relative_links_resolve_against(monkeypatch):
    html = (
        '<head><base href="https://cdn.example.net/x/"></head><body><p><a href="y">Y</a></p></body>'
    )

    assert set(links_of(monkeypatch, html)) == {"https://cdn.example.net/x/y"}


def test_links_that_are_not_pages_are_dropped(monkeypatch):
    html = (
        "<body><p>"
        '<a href="#top">top</a> <a href="javascript:void(0)">js</a> <a href="mailto:a@b.c">mail</a>'
        ' <a href="/a?utm_medium=x#end">self</a> <a href="ftp://h/f">ftp</a> <a href="/ok">ok</a>'
        "</p></body>"
    )

    assert set(links_of(monkeypatch, html)) == {"https://example.org/ok"}


def test_a_target_keeps_its_first_link_outside_nav_and_footer(monkeypatch):
    html = (
        '<body><nav><a href="/x">Menu X</a></nav><p>See <a href="/x">the X</a>.</p>'
        '<p><a href="/y">first Y</a> <a href="/y">second Y</a></p>'
        '<footer><a href="/z">Foot Z</a></footer></body>'
    )
    links = links_of(monkeypatch, html)

    assert (links["https://example.org/x"].anchor, links["https://example.org/x"].region) == (
        "the X",
        "main",
    )
    assert links["https://example.org/y"].anchor == "first Y"
    assert links["https://example.org/z"].region == "footer"


def test_a_link_under_a_related_heading_or_box_is_related(monkeypatch):
    html = (
        '<body><p><a href="/m">m</a></p><h2>Related articles</h2>'
        '<ul><li><a href="/b">b</a></li></ul>'
        '<h3>Notes</h3><p><a href="/c">c</a></p><div class="Recommended-posts"><a href="/d">d</a>'
        '</div><aside><a href="/e">e</a></aside></body>'
    )
    links = links_of(monkeypatch, html)

    assert {u[-1]: link.region for u, link in links.items()} == {
        "m": "main",
        "b": "related",
        "c": "main",
        "d": "related",
        "e": "related",
    }


def test_context_is_the_block_around_the_anchor_cut_to_200_characters(monkeypatch):
    long = "word " * 100
    html = (
        f'<body><p>{long}<a href="/t">the target</a> {long}</p>'
        '<ul><li>Short <a href="/s">item</a></li></ul></body>'
    )
    links = links_of(monkeypatch, html)

    assert len(links["https://example.org/t"].context) == 200
    assert "the target" in links["https://example.org/t"].context
    assert links["https://example.org/s"].context == "Short item"


def test_the_title_falls_back_to_the_first_h1(monkeypatch):
    assert serve_html(monkeypatch, "<body><h1>The  stone</h1><p>x</p></body>").title == "The stone"
    assert serve_html(monkeypatch, "<title>T</title><body><h1>H</h1></body>").title == "T"


ARTICLE = (PAGES / "cookie_wall.html").read_text(encoding="utf-8")
SHELL = (PAGES / "spa_shell.html").read_bytes()


def fake_render(monkeypatch, html=ARTICLE, status=200):
    monkeypatch.setattr(runtime, "_fetch_render", "auto")
    asked = []

    def render(url):
        asked.append(url)
        return browser.Rendered(html, url, status)

    monkeypatch.setattr(browser, "render", render)
    return asked


@pytest.mark.parametrize(
    ("body", "status"), [(SHELL, 200), (b"<html></html>", 403)], ids=["shell", "403"]
)
def test_a_shell_or_a_blocked_page_is_rendered(monkeypatch, body, status):
    serve(monkeypatch, body, status)
    asked = fake_render(monkeypatch)

    page = fetch.fetch_page("https://example.org/app")

    assert asked == ["https://example.org/app"]
    assert page.engine == "render"
    assert page.text


def test_a_blocked_page_is_rendered_after_its_fetch_permit_is_released(monkeypatch):
    # The render takes a permit of its own; taken while the static fetch still held one, two
    # blocked pages in flight held both of two permits and waited on each other for good.
    serve(monkeypatch, b"<html></html>", 403)
    fake_render(monkeypatch)
    render = browser.render

    def render_in_a_slot(url):
        with runtime.host_slot("example.org"):
            return render(url)

    monkeypatch.setattr(browser, "render", render_in_a_slot)
    monkeypatch.setattr(runtime, "_fetch_slots", threading.BoundedSemaphore(1))
    fetched = []
    worker = threading.Thread(
        target=lambda: fetched.append(fetch.fetch_page("https://example.org/app")), daemon=True
    )

    worker.start()
    worker.join(5)

    assert fetched and fetched[0].engine == "render"


def test_a_stealth_render_is_named_so(monkeypatch):
    monkeypatch.setattr(runtime, "_fetch_stealth", True)
    serve(monkeypatch, SHELL)
    fake_render(monkeypatch)

    assert fetch.fetch_page("https://example.org/app").engine == "stealth"


def test_without_a_browser_the_static_failure_stands(monkeypatch):
    serve(monkeypatch, SHELL)

    def render(url):
        raise browser.RenderUnavailable("no browser")

    monkeypatch.setattr(runtime, "_fetch_render", "auto")
    monkeypatch.setattr(browser, "render", render)

    with pytest.raises(UnusablePage) as raised:
        fetch.fetch_page("https://example.org/app")
    assert raised.value.reason == "shell"


def test_with_render_off_no_browser_is_asked(monkeypatch):
    serve(monkeypatch, SHELL)
    asked = fake_render(monkeypatch)
    monkeypatch.setattr(runtime, "_fetch_render", "off")

    with pytest.raises(UnusablePage):
        fetch.fetch_page("https://example.org/app")
    assert asked == []


@pytest.mark.parametrize(
    ("html", "status"),
    [((PAGES / "challenge_cloudflare.html").read_text(encoding="utf-8"), 200), (ARTICLE, 404)],
    ids=["challenge", "404"],
)
def test_a_render_that_is_still_a_challenge_or_an_error_fails(monkeypatch, html, status):
    serve(monkeypatch, SHELL)
    fake_render(monkeypatch, html, status)

    with pytest.raises(FetchError):
        fetch.fetch_page("https://example.org/app")


def current_split(text, size):
    """`split` as it was before headings: lines only, hard cut for a long line."""
    pieces, current = [], ""
    for line in text.splitlines():
        while len(line) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:size])
            line = line[size:]
        if current and len(current) + 1 + len(line) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return [*pieces, current] if current else pieces


SECTIONED = ["wiki_bisection.html", "wiki_kvasir.html", "fastapi_first_steps.html"]
SIZES = [800, 2000, 6000]


def page_text(name):
    return extract((PAGES / name).read_text(encoding="utf-8"))


def top_mark(text):
    """The marker of the shallowest heading level that occurs twice."""
    levels = [len(m[1]) for m in re.finditer(r"^(#{1,6}) \S", text, re.M)]
    return "#" * min(level for level in set(levels) if levels.count(level) > 1) + " "


@pytest.mark.parametrize("name", SECTIONED)
@pytest.mark.parametrize("size", SIZES)
def test_split_keeps_pieces_in_size_and_in_one_top_level_section(name, size):
    text = page_text(name)
    mark = top_mark(text)
    pieces = split(text, size)

    assert all(len(piece) <= size for piece in pieces)
    assert all(sum(line.startswith(mark) for line in p.splitlines()) <= 1 for p in pieces)
    first = next(
        i for i, p in enumerate(pieces) if any(line.startswith(mark) for line in p.splitlines())
    )
    assert all(re.match(r"#{1,6} \S", piece) for piece in pieces[first:])


@pytest.mark.parametrize("name", SECTIONED)
@pytest.mark.parametrize("size", SIZES)
def test_split_loses_no_line_of_the_text(name, size):
    text = page_text(name)
    kept = iter(line for piece in split(text, size) for line in piece.splitlines())

    # In order, once the headings added to a piece are skipped over. A line over `size` is cut.
    assert all(line in kept for line in text.splitlines() if line.strip() and len(line) <= size)


def test_split_prefixes_a_piece_cut_from_a_section_with_the_headings_above_it():
    paragraphs = "\n\n".join(["p" * 40] * 4)
    text = f"# Page\n\nabout\n\n## History\n\nlead\n\n### Principate\n\n{paragraphs}"
    text += "\n\n## Legacy\n\nlater"

    pieces = split(text, 120)

    assert pieces[0] == "# Page\n\nabout"
    assert pieces[1].startswith("## History\n\nlead\n\n### Principate\n\n")
    assert pieces[2].startswith("## History\n### Principate\n")
    assert all(len(piece) <= 120 for piece in pieces)
    assert [p for p in pieces if "## Legacy" in p] == ["## Legacy\n\nlater"]


def test_split_cuts_a_section_over_the_size_between_paragraphs():
    paragraphs = ["a" * 30, "b" * 30, "c" * 30]
    text = "## A\n\n" + "\n\n".join(paragraphs) + "\n\n## B\n\nx"

    pieces = split(text, 80)

    assert pieces[:2] == [
        "## A\n\n" + paragraphs[0] + "\n\n" + paragraphs[1],
        "## A\n" + paragraphs[2],
    ]
    assert pieces[2] == "## B\n\nx"


def test_split_without_headings_is_the_line_split():
    text = "aaa\nbbb\n\ncc\n" + "d" * 25 + "\n#not a heading\nee"

    for size in (7, 10, 100):
        assert split(text, size) == current_split(text, size)


# Wikipedia's Kvasir has no subsections, so nothing in it is merged.
@pytest.mark.parametrize("name", ["wiki_bisection.html", "fastapi_first_steps.html"])
def test_a_quote_across_a_merged_boundary_is_quoted_on_its_piece(name):
    pieces = split(page_text(name), 6000)
    # A piece with a heading inside it, so with two sections merged.
    lines = next(
        p.splitlines() for p in pieces if any(line.startswith("#") for line in p.splitlines()[1:])
    )
    at = next(i for i, line in enumerate(lines) if i and line.startswith("#"))
    before = next(line for line in reversed(lines[:at]) if line.strip() and line[0] != "#")
    after = next(line for line in lines[at:] if line.strip() and line[0] != "#")

    quote = f"{' '.join(before.split()[-6:])} ... {' '.join(after.split()[:6])}"
    assert text.quoted(quote, "\n".join(lines))
