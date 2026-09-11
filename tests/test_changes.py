"""Tests for the website's what's-new page, rendered from CHANGELOG.md."""

from __future__ import annotations

from idlerpg import __version__, web

SAMPLE = """# Changelog

Newest *first*.

## 1.0.0 - today

- **Big** thing with `CODE`,
  wrapped onto a second line.
  - nested under it
- a sibling

## 0.9.0 - before

- older
"""


class TestRendering:
    def test_headings_paragraphs_and_inline_marks(self):
        html = web.render_changelog(SAMPLE)
        assert "<h2>1.0.0 - today</h2>" in html and "<h2>0.9.0 - before</h2>" in html
        assert "<p>Newest <em>first</em>.</p>" in html
        assert ("<li><strong>Big</strong> thing with <code>CODE</code>, "
                "wrapped onto a second line.") in html
        assert "Changelog" not in html            # the page has its own title

    def test_lists_nest_and_close(self):
        html = web.render_changelog(SAMPLE)
        assert html.count("<ul>") == html.count("</ul>") == 3
        assert html.index("nested under it") < html.index("a sibling")

    def test_the_file_cannot_inject_markup(self):
        html = web.render_changelog("- <script>alert(1)</script> **<b>x</b>**")
        assert "<script>" not in html and "<b>" not in html


class TestThePage:
    def test_it_shows_the_running_version(self):
        assert f"<h2>{__version__} - " in web.page_changes()

    def test_every_page_links_to_it(self):
        assert 'href="/changes"' in web.page_game()
