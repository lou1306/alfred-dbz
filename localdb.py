#!/usr/bin/env python3
"""Offline copy of DBLP, built from the monthly XML dump on Dagstuhl DROPS.

`build()` downloads a release and turns it into a DuckDB database with a
full-text index. `search()` and `get()` answer the same questions as the DBLP
search API and /rec/{key}.xml, in the same shapes, so callers can try the
local copy first and fall back to the network.

The index is DuckDB's FTS extension, flattened at build time into a table of
precomputed BM25 weights sorted by term (`postings`). Querying it directly is
~10x faster than FTS's match_bm25, which joins every record on each call, and
it needs no extension at query time.
"""

import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path
from sys import stderr

import duckdb
import requests
import xmltodict
from lxml import etree

DROPS = "https://drops.dagstuhl.de"
RELEASES_PAGE = f"{DROPS}/entities/collection/10.4230/dblp.xml"
ARTIFACTS = f"{DROPS}/storage/artifacts/dblp/xml"

BUNDLE_ID = "com.github.lou1306.alfred-dbz"
DB_NAME = "dblp.duckdb"
SCHEMA_VERSION = 2

# Top-level record types to keep. `www` (person pages) is skipped;
# proceedings and books must stay because other records crossref them.
PUB_TYPES = frozenset({
    "article", "inproceedings", "proceedings", "book", "incollection",
    "phdthesis", "mastersthesis", "data"})

# The last query word is matched as a prefix only from this length on;
# shorter ones would expand to a large slice of the vocabulary.
MIN_PREFIX = 3

_TIMEOUT = (5, 30)


def data_dir() -> Path:
    """Where the database lives: outside the cloud-synced workflow folder."""
    return Path(
        os.environ.get("DBZ_DATA_DIR")
        or os.environ.get("alfred_workflow_data")
        or Path.home() / "Library/Application Support/Alfred/Workflow Data" / BUNDLE_ID)


def db_path() -> Path:
    return data_dir() / DB_NAME


def _progress(what, done, total=None):
    if total:
        print(f"{what}: {done / 1e6:,.0f}/{total / 1e6:,.0f} MB", file=stderr)
    else:
        print(f"{what}: {done:,}", file=stderr)


# --- Downloading -------------------------------------------------------------

def latest_release() -> str:
    """Date (YYYY-MM-DD) of the newest XML release listed on DROPS."""
    r = requests.get(RELEASES_PAGE, timeout=_TIMEOUT)
    r.raise_for_status()
    dates = re.findall(r"dblp\.xml\.(\d{4}-\d{2}-\d{2})", r.text)
    if not dates:
        raise RuntimeError(f"No dblp XML releases listed on {RELEASES_PAGE}")
    return max(dates)


def _artifact_url(filename: str) -> str:
    # Artifacts are filed by year: .../xml/2026/dblp-2026-09-01.xml.gz
    m = re.match(r"dblp-(\d{4})-\d{2}-\d{2}\.", filename)
    if not m:
        raise ValueError(f"Unexpected dblp artifact name: {filename}")
    return f"{ARTIFACTS}/{m.group(1)}/{filename}"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(filename: str, dest_dir: Path) -> Path:
    """Fetch a DROPS artifact into dest_dir, resuming a partial download and
    checking it against the published .md5. Returns the file's path."""
    url = _artifact_url(filename)
    r = requests.get(url + ".md5", timeout=_TIMEOUT)
    r.raise_for_status()
    expected = r.text.split()[0].lower()  # "<hash>" or "<hash>  <name>"

    dest = dest_dir / filename
    if dest.exists() and _md5(dest) == expected:
        return dest
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    with requests.get(url, headers=headers, stream=True, timeout=_TIMEOUT) as r:
        if r.status_code != 416:  # 416: the .part already holds everything
            r.raise_for_status()
            if r.status_code != 206:
                done = 0  # the server ignored Range: start over
            total = done + int(r.headers.get("Content-Length", 0))
            last = 0.0
            with open(part, "ab" if done else "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 2:
                        _progress(f"Downloading {filename}", done, total)
                        last = time.time()
    if _md5(part) != expected:
        part.unlink()
        raise RuntimeError(f"{filename}: MD5 mismatch, discarded the download")
    os.replace(part, dest)
    return dest


def doctype_dtd(xml_gz: Path) -> str:
    """The DTD file name a dump declares, e.g. dblp-2023-06-28.dtd."""
    with gzip.open(xml_gz, "rb") as f:
        head = f.read(1024).decode("latin-1")
    m = re.search(r'<!DOCTYPE\s+dblp\s+SYSTEM\s+"([^"]+)"', head)
    if not m:
        raise RuntimeError(f"{xml_gz.name}: no DOCTYPE declaration found")
    return m.group(1)


# --- Parsing -----------------------------------------------------------------

def _text(el) -> str:
    # itertext() flattens inline markup such as <i> and <sub> in titles.
    return " ".join("".join(el.itertext()).split())


def _record(el) -> dict:
    authors, editors, fields = [], [], {}
    for child in el:
        tag = child.tag
        if tag == "author":
            authors.append(_text(child))
        elif tag == "editor":
            editors.append(_text(child))
        elif tag in ("title", "year", "journal", "booktitle"):
            fields.setdefault(tag, _text(child))
    return {
        "key": el.get("key"),
        "type": el.tag,
        "title": fields.get("title", ""),
        # Fall back to editors so proceedings still get a byline.
        "authors": authors or editors,
        "year": fields.get("year"),
        "venue": fields.get("journal") or fields.get("booktitle"),
        "raw": etree.tostring(el, encoding="unicode", with_tail=False),
    }


# Decoded text no longer is in the encoding it declares (US-ASCII for
# /rec/{key}.xml), and lxml refuses str input that declares one at all.
_STR_PARSER = etree.XMLParser(encoding="utf-8")


def parse_record(xml: str):
    """(info, dblp_type) for the record in `xml`, either a /rec/{key}.xml
    document or a bare record as stored in pubs.raw, in xmltodict's shape.

    xmltodict keeps only the text outside child elements, so on its own it
    would turn "der <i>Principia</i> und" into "der  und". Per the DTD only
    <title> holds such markup (<i>, <sub>, <sup>, <tt>, <ref>, nested), but
    any field with children is flattened with _text() first."""
    root = etree.fromstring(xml.encode("utf-8"), _STR_PARSER)
    rec = root[0] if root.tag == "dblp" else root
    for field in rec:
        if len(field):
            text = _text(field)
            del field[:]
            field.text = text
    return xmltodict.parse(etree.tostring(rec, encoding="unicode"))[rec.tag], rec.tag


def iter_records(xml_gz: Path):
    """Yield one dict per publication in a dump, with flat memory use.

    The DTD the dump declares must sit next to it: dblp uses named character
    entities (&uuml; ...) that only the DTD defines."""
    # This libxml2 build cannot read .gz itself, so decompress in Python;
    # lxml resolves the DTD relative to the file object's name.
    with gzip.open(xml_gz, "rb") as f:
        for _, el in etree.iterparse(
                f, events=("end",), load_dtd=True, resolve_entities=True,
                huge_tree=True, no_network=True):
            parent = el.getparent()
            if parent is None or parent.getparent() is not None:
                continue  # the <dblp> root, or a field inside a record
            if el.tag in PUB_TYPES:
                yield _record(el)
            # Drop every finished record, skipped ones included, or the whole
            # multi-GB tree would pile up under the root.
            el.clear()
            while el.getprevious() is not None:
                del parent[0]


# --- Building ----------------------------------------------------------------

# DuckDB FTS's tokenizer, in plain SQL so queries need no extension. It must
# match the `ignore`, `strip_accents` and `lower` settings of the index below.
_TOKENIZE = r"""string_split_regex(regexp_replace(lower(strip_accents({})),
    '(\\.|[^a-z0-9])+', ' ', 'g'), '\s+')"""


def _sql_str(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _memory_limit() -> str:
    # Building the full-text index otherwise grows to most of the RAM.
    total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return f"{max(1, total // 2 // 2**30)}GB"


def build_from(xml_gz: Path, release: str) -> int:
    """Build the database from a downloaded dump, swapping it in only once it
    is complete: lookups keep working meanwhile, and a failure changes nothing.
    Returns the number of records."""
    ddir = data_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    tmp_db = ddir / (DB_NAME + ".tmp")
    scratch = ddir / "build.duckdb.tmp"
    tmp_json = ddir / "records.ndjson.tmp"
    leftovers = (tmp_db, Path(f"{tmp_db}.wal"), scratch, Path(f"{scratch}.wal"),
                 tmp_json)
    for p in leftovers:
        p.unlink(missing_ok=True)
    try:
        n = 0
        with open(tmp_json, "w", encoding="utf-8") as out:
            for rec in iter_records(xml_gz):
                out.write(json.dumps(rec, ensure_ascii=False))
                out.write("\n")
                n += 1
                if n % 500_000 == 0:
                    _progress("Parsed records", n)
        # The FTS index is only a stepping stone: build it in a scratch
        # database and keep just what queries need.
        con = duckdb.connect(str(scratch))
        try:
            con.execute(f"SET memory_limit = '{_memory_limit()}'")
            con.execute("INSTALL fts")
            con.execute("LOAD fts")
            print("Loading records into DuckDB...", file=stderr)
            con.execute(f"""
                CREATE TABLE pubs AS
                SELECT row_number() OVER (ORDER BY key) AS rid, key, type, title,
                       authors, array_to_string(authors, ' ') AS authors_text,
                       year, venue, raw
                FROM read_json({_sql_str(tmp_json)},
                    format = 'newline_delimited',
                    columns = {{key: 'VARCHAR', type: 'VARCHAR',
                               title: 'VARCHAR', authors: 'VARCHAR[]',
                               year: 'VARCHAR', venue: 'VARCHAR',
                               raw: 'VARCHAR'}})""")
            tmp_json.unlink()
            print("Building the full-text index...", file=stderr)
            # No stemming and no stopwords, like DBLP's own search; that also
            # keeps whole words in the dictionary for prefix matching.
            con.execute(r"""
                PRAGMA create_fts_index('pubs', 'rid', 'title', 'authors_text',
                    'venue', 'year', stemmer = 'none', stopwords = 'none',
                    ignore = '(\\.|[^a-z0-9])+', strip_accents = 1, lower = 1)""")
            print("Writing the database...", file=stderr)
            con.execute(f"ATTACH {_sql_str(tmp_db)} AS out")
            # Sorted by key (rid follows key order), for fast get(key).
            con.execute("""
                CREATE TABLE out.pubs AS
                SELECT rid::UINTEGER AS rid, key, type, title, authors, year,
                       venue, raw
                FROM pubs ORDER BY rid""")
            con.execute("""
                CREATE TABLE out.words AS
                SELECT term, termid::UINTEGER AS termid FROM fts_main_pubs.dict
                ORDER BY term""")
            # One row per (word, record) with its BM25 weight (k1 = 1.2,
            # b = 0.75, as match_bm25), sorted by word so that a query reads
            # only the runs for its own words.
            con.execute("""
                CREATE TABLE out.postings AS
                WITH tf AS (
                    SELECT termid, docid, count(*) AS tf
                    FROM fts_main_pubs.terms GROUP BY ALL)
                SELECT tf.termid::UINTEGER AS termid, d.name::UINTEGER AS rid,
                       (ln((st.num_docs - w.df + 0.5) / (w.df + 0.5) + 1)
                        * tf.tf * 2.2
                        / (tf.tf + 1.2 * (0.25 + 0.75 * d.len / st.avgdl)))::FLOAT AS w
                FROM tf
                JOIN fts_main_pubs.docs d USING (docid)
                JOIN fts_main_pubs.dict w USING (termid),
                fts_main_pubs.stats st
                ORDER BY termid, rid""")
            con.execute(
                "CREATE TABLE out.meta AS SELECT ? AS release, now() AS built_at, "
                "? AS records, ? AS schema_version", [release, n, SCHEMA_VERSION])
            con.execute("DETACH out")
        finally:
            con.close()
        close()
        os.replace(tmp_db, db_path())
    finally:
        for p in leftovers:
            p.unlink(missing_ok=True)
    return n


def build(release=None, keep_download=False) -> dict:
    """Download a release (default: the newest) and rebuild the database."""
    t0 = time.time()
    ddir = data_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    release = release or latest_release()
    xml_gz = download(f"dblp-{release}.xml.gz", ddir)
    dtd = download(doctype_dtd(xml_gz), ddir)
    records = build_from(xml_gz, release)
    if not keep_download:
        xml_gz.unlink()
        dtd.unlink()
    return {"release": release, "records": records,
            "bytes": db_path().stat().st_size, "seconds": time.time() - t0}


# --- Querying ----------------------------------------------------------------

_con = None


def _connect():
    global _con
    if _con is None:
        # read_only: a default read-write open locks the file exclusively,
        # and Alfred runs several lookups at once.
        _con = duckdb.connect(str(db_path()), read_only=True)
    return _con


def close():
    global _con
    if _con is not None:
        _con.close()
        _con = None


def available() -> bool:
    """Whether a usable local database exists."""
    if not db_path().exists():
        return False
    try:
        version = _connect().execute("SELECT schema_version FROM meta").fetchone()[0]
    except duckdb.Error as exc:
        print(f"Local DBLP copy unusable: {exc}", file=stderr)
        return False
    if version != SCHEMA_VERSION:
        print("Local DBLP copy is in an old format; run update-db to rebuild it.",
              file=stderr)
        return False
    return True


def release() -> str:
    return _connect().execute("SELECT release FROM meta").fetchone()[0]


def search(q: str, limit: int = 30) -> list:
    """Publications matching every word of q, best first, shaped like the
    `info` objects of the DBLP search API. Unless q ends in whitespace, its
    last word may be incomplete and is matched as a prefix."""
    con = _connect()
    tokens = [t for t in con.execute(
        f"SELECT {_TOKENIZE.format('?')}", [q]).fetchone()[0] if t]
    if not tokens:
        return []
    prefix = None
    if not q[-1].isspace():
        if len(tokens[-1]) >= MIN_PREFIX:
            prefix = tokens.pop()
        elif len(tokens) > 1:
            # Too short to expand, and most likely the start of the next word:
            # ignore it rather than empty the results (and go online) on
            # every keystroke. A short query on its own ("AI") is a word.
            tokens.pop()
    words = list(dict.fromkeys(tokens))

    parts, params = [], []
    if words:
        # Every complete word must occur: one posting per word per record.
        parts.append("""
            SELECT rid, sum(w) AS score FROM postings
            WHERE termid IN (SELECT termid FROM words WHERE term = ANY(?))
            GROUP BY rid HAVING count(*) = ?""")
        params += [words, len(words)]
    if prefix:
        # The unfinished word counts through its best-scoring completion.
        parts.append("""
            SELECT rid, max(w) AS score FROM postings
            WHERE termid IN (SELECT termid FROM words WHERE starts_with(term, ?))
            GROUP BY rid""")
        params.append(prefix)
    scored = parts[0] if len(parts) == 1 else f"""
        SELECT rid, a.score + b.score AS score
        FROM ({parts[0]}) a JOIN ({parts[1]}) b USING (rid)"""
    rids = [rid for (rid,) in con.execute(
        f"SELECT rid FROM ({scored}) ORDER BY score DESC, rid LIMIT ?",
        params + [limit]).fetchall()]
    if not rids:
        return []
    rows = {row[0]: row[1:] for row in con.execute(
        "SELECT rid, key, title, year, venue, authors FROM pubs WHERE rid = ANY(?)",
        [rids]).fetchall()}
    return [_as_hit(*rows[rid]) for rid in rids]


def _as_hit(key, title, year, venue, authors) -> dict:
    hit = {"key": key, "title": title, "year": year or "",
           "authors": {"author": [{"text": a} for a in authors]}}
    if venue:
        hit["venue"] = venue
    return hit


def get(key: str):
    """(info, dblp_type) for a record, as main.get() builds it from
    /rec/{key}.xml; None if the local copy does not have it."""
    row = _connect().execute(
        "SELECT raw FROM pubs WHERE key = ?", [key]).fetchone()
    if row is None:
        return None
    return parse_record(row[0])
