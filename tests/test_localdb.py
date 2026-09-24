import localdb


def test_parser_resolves_entities_flattens_markup_and_skips_www(mini_dump):
    recs = {r["key"]: r for r in localdb.iter_records(mini_dump)}
    assert "homepages/p/AmirPnueli" not in recs
    assert len(recs) == 5
    godel = recs["journals/test/Godel31"]
    assert godel["authors"] == ["Kurt Gödel"]
    assert godel["title"] == (
        "Über formal unentscheidbare Sätze der Principia Mathematica "
        "und verwandter Systeme I.")
    assert recs["journals/test/Chemist20"]["title"] == "On H2O and Linearizability."
    assert recs["conf/popl/LichtensteinP85"]["authors"] == [
        "Orna Lichtenstein", "Amir Pnueli"]
    # Proceedings have no authors: fall back to the editors.
    assert recs["conf/popl/1985"]["authors"][0] == "Mary S. Van Deusen"


def test_doctype_names_the_dtd(mini_dump):
    assert localdb.doctype_dtd(mini_dump) == "dblp-test.dtd"


def keys(q):
    return [hit["key"] for hit in localdb.search(q)]


def test_search_exact_title(mini_db):
    assert keys("Checking That Finite State Concurrent Programs "
                "Satisfy Their Linear Specification") == ["conf/popl/LichtensteinP85"]


def test_last_word_is_a_prefix_unless_followed_by_space(mini_db):
    assert set(keys("linear")) == {"conf/popl/LichtensteinP85", "journals/test/Chemist20"}
    assert keys("linear ") == ["conf/popl/LichtensteinP85"]
    assert keys("Finite State Concurrent Programs Satisfy Their Linear Spec") == [
        "conf/popl/LichtensteinP85"]


def test_every_word_must_match(mini_db):
    assert keys("lamport concurrent") == []


def test_unfinished_word_must_match_too(mini_db):
    # Complete words matching is not enough: the prefix has to match as well.
    assert keys("lamport temporal act") == ["journals/toplas/Lamport94"]
    assert keys("lamport temporal xyz") == []


def test_short_unfinished_word_is_ignored_but_short_query_is_a_word(mini_db):
    # Starting to type the next word must not empty the results.
    assert keys("lamport temporal a") == ["journals/toplas/Lamport94"]
    assert keys("lamport temporal xy") == ["journals/toplas/Lamport94"]
    # A short query on its own is a whole word: not "Concurrent", "Monatshefte".
    assert set(keys("on")) == {"journals/test/Chemist20", "conf/popl/1985"}


def test_queries_need_no_fts_extension(mini_db):
    con = localdb._connect()
    assert con.execute("SELECT count(*) FROM duckdb_schemas() "
                       "WHERE schema_name LIKE 'fts%'").fetchone()[0] == 0
    assert {t for (t,) in con.execute("SHOW TABLES").fetchall()} == {
        "pubs", "words", "postings", "meta"}


def test_search_by_author_venue_and_year(mini_db):
    assert keys("pnueli") == ["conf/popl/LichtensteinP85"]
    assert set(keys("POPL 1985")) == {"conf/popl/LichtensteinP85", "conf/popl/1985"}


def test_search_ignores_accents(mini_db):
    assert keys("godel") == keys("Gödel") == ["journals/test/Godel31"]


def test_hits_have_the_search_api_shape(mini_db):
    [hit] = localdb.search("temporal logic of actions")
    assert hit == {
        "key": "journals/toplas/Lamport94",
        "title": "The Temporal Logic of Actions.",
        "year": "1994",
        "venue": "ACM Trans. Program. Lang. Syst.",
        "authors": {"author": [{"text": "Leslie Lamport"}]},
    }


def test_get_matches_what_dblp_rec_xml_returns(mini_db):
    # Recorded from https://dblp.org/rec/conf/popl/LichtensteinP85.xml,
    # parsed the way main.get() parses it.
    online = {
        "@key": "conf/popl/LichtensteinP85",
        "@mdate": "2026-02-19",
        "author": ["Orna Lichtenstein", "Amir Pnueli"],
        "title": "Checking That Finite State Concurrent Programs Satisfy "
                 "Their Linear Specification.",
        "pages": "97-107",
        "year": "1985",
        "crossref": "conf/popl/1985",
        "booktitle": "POPL",
        "url": "db/conf/popl/popl85.html#LichtensteinP85",
        "ee": {"@type": "oa", "#text": "https://doi.org/10.1145/318593.318622"},
    }
    info, dblp_type = localdb.get("conf/popl/LichtensteinP85")
    assert dblp_type == "inproceedings"
    assert info == online


def test_get_unknown_key(mini_db):
    assert localdb.get("conf/nope/Nobody99") is None


def test_release_and_availability(mini_db):
    assert localdb.available()
    assert localdb.release() == "2000-01-01"


def test_missing_db_is_unavailable(no_db):
    assert not localdb.available()


def test_db_in_an_old_format_is_unavailable(mini_db):
    localdb.close()
    con = localdb.duckdb.connect(str(localdb.db_path()))
    con.execute("UPDATE meta SET schema_version = schema_version - 1")
    con.close()
    assert not localdb.available()


def test_failed_build_keeps_the_old_db(mini_db, tmp_path):
    broken = tmp_path / "dblp-2000-02-01.xml.gz"
    broken.write_bytes(b"not gzip")
    try:
        localdb.build_from(broken, "2000-02-01")
    except Exception:
        pass
    else:
        raise AssertionError("building from garbage should fail")
    assert localdb.release() == "2000-01-01"
    assert not list(localdb.data_dir().glob("*.tmp*"))
