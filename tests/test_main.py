import json

import pytest

import main


class FakeZotero:
    """Just enough of pyzotero for add_to_zotero_fn, with no network."""
    created = []

    def __init__(self, *args):
        pass

    def item_template(self, item_type):
        return {"itemType": item_type, "title": "", "creators": [],
                "proceedingsTitle": "", "publisher": "", "date": "",
                "pages": "", "series": "", "volume": "", "DOI": "",
                "ISBN": "", "url": "", "extra": ""}

    def create_items(self, items):
        FakeZotero.created.extend(items)
        return {"success": {"0": "ABCD1234"}, "unchanged": {}, "failed": {}}


@pytest.fixture
def offline(monkeypatch):
    """Make any DBLP network access fail the test."""
    def boom(*args, **kwargs):
        raise AssertionError("unexpected DBLP network access")
    monkeypatch.setattr(main, "fetch", boom)
    monkeypatch.setattr(main, "query_dblp", boom)
    monkeypatch.setattr(main, "_debounce", lambda q: True)


@pytest.fixture
def online(monkeypatch):
    """Record DBLP API queries and answer them with one canned hit."""
    queries = []

    def query_dblp(q):
        queries.append(q)
        hit = {"key": "journals/new/Paper26", "title": "A Paper Newer Than The Dump.",
               "year": "2026", "venue": "J. New",
               "authors": {"author": {"text": "Nora Newcomer"}}}
        return [hit], 1, 1, 1, "url", main.DBLP
    monkeypatch.setattr(main, "query_dblp", query_dblp)
    monkeypatch.setattr(main, "_debounce", lambda q: True)
    return queries


def lookup(capsys, q):
    main.alfred_lookup.callback(q)
    return json.loads(capsys.readouterr().out)["items"]


def test_local_hit_never_touches_the_network(mini_db, offline, capsys):
    items = lookup(capsys, "Checking That Finite State Concurrent")
    assert items[0]["arg"] == "conf/popl/LichtensteinP85"
    assert items[0]["subtitle"] == "Orna Lichtenstein, Amir Pnueli (POPL, 1985)"
    assert items[-1]["autocomplete"] == "Checking That Finite State Concurrent!"
    assert items[-1]["valid"] is False
    assert "2000-01-01" in items[-1]["subtitle"]


def test_zero_local_hits_fall_back_to_dblp(mini_db, online, capsys):
    items = lookup(capsys, "newer than the dump")
    assert online == ["newer than the dump"]
    assert [i["arg"] for i in items] == ["journals/new/Paper26"]


def test_trailing_bang_forces_dblp(mini_db, online, capsys):
    items = lookup(capsys, "Checking That Finite State!")
    assert online == ["Checking That Finite State"]
    assert items[0]["arg"] == "journals/new/Paper26"


def test_missing_db_falls_back_to_dblp(no_db, online, capsys):
    lookup(capsys, "Checking That Finite State")
    assert online == ["Checking That Finite State"]


def test_broken_local_search_falls_back_to_dblp(mini_db, online, capsys, monkeypatch):
    def broken(q):
        raise RuntimeError("corrupt database")
    monkeypatch.setattr(main.localdb, "search", broken)
    items = lookup(capsys, "Checking That Finite State")
    assert online == ["Checking That Finite State"]
    assert items[0]["arg"] == "journals/new/Paper26"


def test_add_to_zotero_offline_resolves_crossref(mini_db, offline, monkeypatch, capsys):
    monkeypatch.setattr(main.zotero, "Zotero", FakeZotero)
    FakeZotero.created.clear()
    main.add_to_zotero.callback("conf/popl/LichtensteinP85", False)
    assert capsys.readouterr().out.strip() == "Added conf/popl/LichtensteinP85 to Zotero."
    [item] = FakeZotero.created
    assert item["title"] == ("Checking That Finite State Concurrent Programs "
                             "Satisfy Their Linear Specification")
    assert item["DOI"] == "10.1145/318593.318622"
    assert item["pages"] == "97-107"
    assert item["publisher"] == "ACM Press"
    assert item["proceedingsTitle"].startswith("Conference Record of the Twelfth")
    assert [(c["creatorType"], c["lastName"]) for c in item["creators"]] == [
        ("author", "Lichtenstein"), ("author", "Pnueli"),
        ("editor", "Van Deusen"), ("editor", "Galil"), ("editor", "Reid")]


def test_add_to_zotero_keeps_markup_text_in_title(mini_db, offline, monkeypatch):
    # xmltodict alone turns this <title> into {'i': ..., '#text': '... der  und ...'}.
    monkeypatch.setattr(main.zotero, "Zotero", FakeZotero)
    FakeZotero.created.clear()
    main.add_to_zotero_fn("journals/test/Godel31", True)
    [item] = FakeZotero.created
    assert item["title"] == ("Über formal unentscheidbare Sätze der Principia "
                             "Mathematica und verwandter Systeme I")


def test_get_online_keeps_markup_text_in_title(no_db, monkeypatch):
    class Resp:
        status_code = 200
        # As /rec/{key}.xml serves it: an encoding declaration and numeric
        # character references.
        text = ('<?xml version="1.0" encoding="US-ASCII"?>\n<dblp>\n'
                '<article key="journals/test/Chemist20"><author>Ann Chemist</author>'
                '<title>On H<sub>2</sub>O and <i>Lin&#233;arit&#233;</i>.</title>'
                '</article>\n</dblp>\n')
    monkeypatch.setattr(main, "fetch", lambda path, params=None: (Resp(), main.DBLP))
    info, dblp_type = main.get("journals/test/Chemist20")
    assert (dblp_type, info["title"]) == ("article", "On H2O and Linéarité.")


def test_get_falls_back_to_dblp_for_unknown_keys(mini_db, monkeypatch):
    class Resp:
        status_code = 200
        text = ('<?xml version="1.0"?><dblp><article key="journals/new/Paper26">'
                '<title>New.</title></article></dblp>')
    monkeypatch.setattr(main, "fetch", lambda path, params=None: (Resp(), main.DBLP))
    info, dblp_type = main.get("journals/new/Paper26")
    assert (dblp_type, info["title"]) == ("article", "New.")
