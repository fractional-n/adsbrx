"""Consistency of the Web GUI's Japanese / English text: static/i18n.js, index.html and app.js.

The page ships its Japanese text in the HTML (so Japanese needs no script to render) and
i18n.js swaps it for the selected language.  These tests keep the three files in step without a
JavaScript engine: i18n.js keeps one `'key': 'text',` entry per line so that it can be read here.
"""

import re
from html.parser import HTMLParser

import pytest

from adsbrx import modes
from adsbrx.outputs import web

CJK = re.compile("[\u3000-\u9fff\uff00-\uffef]")
ENTRY = re.compile(r"^\s*'([A-Za-z0-9_.]+)':\s*'([^'\\]*)',\s*$")
BLOCK = re.compile(r"^ {4}(\w+): \{\s*$")
BLOCK_END = re.compile(r"^ {4}\},?\s*$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
SCRIPT_ORDER = ["/static/vendor/leaflet.js", "/static/i18n.js", "/static/app.js"]


def read(name: str) -> str:
    return (web.STATIC_DIR / name).read_text(encoding="utf-8")


def squash(text: str) -> str:
    return " ".join(text.split())


def parse_dictionary() -> dict[str, dict[str, str]]:
    tables: dict[str, dict[str, str]] = {}
    table = None
    for number, line in enumerate(read("i18n.js").splitlines(), 1):
        block = BLOCK.match(line)
        if block:
            assert block.group(1) not in tables, f"i18n.js:{number}: language {block.group(1)} is defined twice"
            table = tables.setdefault(block.group(1), {})
        elif BLOCK_END.match(line):
            table = None
        elif table is not None:
            entry = ENTRY.match(line)
            assert entry, f"i18n.js:{number}: cannot read {line.strip()!r} (one 'key': 'text', entry per line, no quotes inside)"
            assert entry.group(1) not in table, f"i18n.js:{number}: {entry.group(1)} is defined twice"
            table[entry.group(1)] = entry.group(2)
    return tables


class IndexPage(HTMLParser):
    """Collects the translatable parts of index.html."""

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
    ATTRIBUTES = ("title", "aria-label", "placeholder")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.title = ""
        self.scripts: list[str] = []
        self.text_keys: list[tuple[str, str]] = []  # (key, text shown before the script runs)
        self.attr_keys: list[tuple[str, str, str | None]] = []  # (key, attribute, value shown before the script runs)
        self.language_options: list[tuple[str | None, str]] = []  # (value, label) of the language selector
        self.untranslated: list[str] = []  # Japanese that no data-i18n attribute can replace

    def in_language_selector(self, attrs: dict) -> bool:
        return attrs.get("id") == "opt-lang" or any(el["attrs"].get("id") == "opt-lang" for el in self.stack)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name, value in attrs.items():
            if name.startswith("data-i18n-"):
                self.attr_keys.append((value, name[len("data-i18n-"):], attrs.get(name[len("data-i18n-"):])))
        for name in self.ATTRIBUTES:
            japanese = CJK.search(attrs.get(name) or "")
            if japanese and "data-i18n-" + name not in attrs and attrs.get("id") != "opt-lang":
                self.untranslated.append(f"<{tag}> {name}={attrs[name]!r}")
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])
        if tag not in self.VOID:
            self.stack.append({"tag": tag, "attrs": attrs, "text": []})

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        element = self.stack.pop()
        assert element["tag"] == tag, f"index.html: <{element['tag']}> is closed by </{tag}>"
        text = squash("".join(element["text"]))
        if "data-i18n" in element["attrs"]:
            self.text_keys.append((element["attrs"]["data-i18n"], text))
        if tag == "title":
            self.title = text
        if tag == "option" and self.in_language_selector(element["attrs"]):
            self.language_options.append((element["attrs"].get("value"), text))

    def handle_data(self, data):
        for element in self.stack:
            element["text"].append(data)
        if CJK.search(data) and not self.translatable():
            self.untranslated.append(f"text {squash(data)!r}")

    def translatable(self) -> bool:
        return any(
            "data-i18n" in el["attrs"] or el["tag"] in ("title", "noscript") or el["attrs"].get("id") == "opt-lang"
            for el in self.stack
        )


@pytest.fixture(scope="module")
def texts():
    return parse_dictionary()


@pytest.fixture(scope="module")
def page():
    parser = IndexPage()
    parser.feed(read("index.html"))
    parser.close()
    assert not parser.stack, f"index.html: <{parser.stack[-1]['tag']}> is never closed"
    return parser


def test_japanese_is_the_default_and_english_can_be_chosen(texts, page):
    assert list(texts) == ["ja", "en"]  # the first language is the default one
    assert [value for value, _ in page.language_options] == list(texts)
    assert [label for _, label in page.language_options] == ["日本語", "English"]
    assert '<html lang="ja">' in read("index.html")
    assert re.search(r"\blang: 'ja'", read("app.js"))


def test_both_languages_define_the_same_keys(texts):
    ja, en = set(texts["ja"]), set(texts["en"])
    assert len(ja) > 50
    assert ja == en, f"only in ja: {sorted(ja - en)}; only in en: {sorted(en - ja)}"


def test_entries_are_filled_in_and_english_has_no_japanese(texts):
    for lang, table in texts.items():
        for key, text in table.items():
            assert text.strip(), f"{lang}: {key} is empty"
    for key, text in texts["en"].items():
        assert not CJK.search(text), f"en: {key} still has Japanese: {text!r}"


def test_placeholders_match_between_languages(texts):
    for key in set(texts["ja"]) & set(texts["en"]):
        assert set(PLACEHOLDER.findall(texts["ja"][key])) == set(PLACEHOLDER.findall(texts["en"][key])), key


def test_parameters_passed_by_the_script_exist_in_the_text(texts):
    calls = re.findall(r"\bt\('([\w.]+)',\s*\{\s*(\w+)", read("app.js"))
    assert calls
    for key, name in calls:
        assert name in PLACEHOLDER.findall(texts["ja"][key]), f"{key} has no {{{name}}}"


def test_html_shows_the_japanese_entries_before_the_script_runs(texts, page):
    ja = texts["ja"]
    assert page.title == ja["app.title"]
    assert page.text_keys and page.attr_keys
    for key, text in page.text_keys:
        assert key in ja, f"index.html: unknown key {key}"
        assert text == squash(ja[key]), f"index.html: the text for {key} is not the Japanese entry"
    for key, attr, value in page.attr_keys:
        assert key in ja, f"index.html: unknown key {key}"
        assert value is not None and squash(value) == squash(ja[key]), f"index.html: {attr} for {key} is not the Japanese entry"


def test_no_japanese_in_the_page_escapes_translation(page):
    assert page.untranslated == []
    assert "requires JavaScript" in read("index.html")  # the no-script notice is bilingual


def test_scripts_load_in_dependency_order(page):
    assert page.scripts == SCRIPT_ORDER


def test_app_keeps_its_text_in_the_dictionary_and_uses_only_exported_names():
    app = read("app.js")
    assert not CJK.search(app)
    exported = re.search(r"window\.adsbI18n = \{ ([^}]*) \};", read("i18n.js"))
    assert exported
    assert set(re.findall(r"\bi18n\.(\w+)", app)) <= {name.strip() for name in exported.group(1).split(",")}


def test_every_key_used_exists_and_every_entry_is_used(texts, page):
    ja = texts["ja"]
    app = read("app.js")
    groups = sorted({key.split(".")[0] for key in ja})
    literals = re.findall(r"'((?:%s)\.[A-Za-z0-9_.]*[A-Za-z0-9_])'" % "|".join(groups), app)
    used = {key for key, _ in page.text_keys} | {key for key, _, _ in page.attr_keys} | set(literals)

    fields = re.search(r"const DETAIL_FIELDS = \[(.*?)\];", app, re.S)
    assert fields
    used |= {"detail." + name for name in re.findall(r"'(\w+)'", fields.group(1))}
    used |= {"emergency." + state for state in modes.EMERGENCY_STATES if state != "none"}
    used |= {"source." + kind for kind in web._TYPES.values()}
    categories = {key for key in ja if key.startswith("category.")}
    assert all(re.fullmatch(r"category\.[ABCD][0-7]", key) for key in categories), sorted(categories)
    assert {f"category.A{n}" for n in range(8)} <= categories
    used |= categories

    assert sorted(used - set(ja)) == [], "keys the page asks for but i18n.js does not define"
    assert sorted(set(ja) - used) == [], "entries in i18n.js that nothing uses"
