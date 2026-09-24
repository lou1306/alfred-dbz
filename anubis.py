#!/usr/bin/env python3
"""Transparent Anubis bot-challenge solver for requests-based HTTP clients."""

import hashlib
import json
import re
import time
from sys import stderr
from urllib.parse import urlencode, urlparse

import requests

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# (connect, read) seconds. Without these, a tarpitted or half-open DBLP
# connection blocks the process forever: Alfred then shows no result and no
# notification, and the wedged `uv run` process lingers for days.
_TIMEOUT = (5, 10)

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": _BROWSER_UA})
    return _session


def _is_challenge(resp: requests.Response) -> bool:
    return (
        "text/html" in resp.headers.get("Content-Type", "")
        and "anubis_challenge" in resp.text
    )


def _script_json(html: str, script_id: str):
    m = re.search(
        r'<script[^>]+id=["\']' + re.escape(script_id) + r'["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None


def _solve(sess: requests.Session, resp: requests.Response) -> bool:
    html = resp.text

    data = _script_json(html, "anubis_challenge")
    if data is None:
        return False

    challenge = data.get("challenge", {})
    rules = data.get("rules", {})
    algorithm = rules.get("algorithm", "fast")
    difficulty = rules.get("difficulty", 4)
    challenge_id = challenge.get("id", "")
    random_data = challenge.get("randomData", "")

    p = urlparse(resp.url)
    base = f"{p.scheme}://{p.netloc}"
    base_prefix = _script_json(html, "anubis_base_prefix") or ""
    pass_url = f"{base}{base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge"

    if algorithm == "metarefresh":
        return _solve_metarefresh(
            sess, html, resp.headers, pass_url, random_data, challenge_id, resp.url, difficulty
        )
    elif algorithm in ("fast", "slow"):
        return _solve_pow(
            sess, pass_url, random_data, challenge_id, resp.url, difficulty
        )
    return False


def _solve_metarefresh(
    sess, html, headers, pass_url, random_data, challenge_id, redir, difficulty
) -> bool:
    refresh_url = None
    m = re.search(r'content=["\'][^;]+;\s*url=([^"\'>\s]+)', html, re.IGNORECASE)
    if m:
        refresh_url = m.group(1)
    elif "Refresh" in headers:
        m2 = re.search(r"url=(.+)", headers["Refresh"], re.IGNORECASE)
        if m2:
            refresh_url = m2.group(1)
    if not refresh_url:
        refresh_url = (
            pass_url + "?" + urlencode({"redir": redir, "challenge": random_data, "id": challenge_id})
        )

    wait = difficulty + 1
    print(f"Anubis: metarefresh challenge, waiting {wait}s…", file=stderr)
    time.sleep(wait)
    r = sess.get(refresh_url, timeout=_TIMEOUT)
    return not _is_challenge(r)


_POW_MAX_HASHES = 1 << 22  # ~4M iterations; gives up if difficulty is impossibly high


def _solve_pow(sess, pass_url, random_data, challenge_id, redir, difficulty) -> bool:
    if not random_data or not challenge_id:
        return False

    n_bytes = difficulty // 2
    odd = difficulty % 2 != 0
    print(
        f"Anubis: PoW challenge (difficulty={difficulty}, ~{16**difficulty:,} hashes expected)…",
        file=stderr,
    )

    t0 = time.time()
    h = b""
    for nonce in range(_POW_MAX_HASHES):
        h = hashlib.sha256(f"{random_data}{nonce}".encode()).digest()
        ok = all(b == 0 for b in h[:n_bytes])
        if ok and odd and h[n_bytes] >> 4:
            ok = False
        if ok:
            break
    else:
        print(
            f"Anubis: PoW difficulty={difficulty} exceeded {_POW_MAX_HASHES:,} hash budget, giving up.",
            file=stderr,
        )
        return False

    elapsed = int((time.time() - t0) * 1000)
    print(f"Anubis: solved nonce={nonce} in {elapsed}ms", file=stderr)

    r = sess.get(pass_url, params={
        "id": challenge_id,
        "response": h.hex(),
        "nonce": nonce,
        "redir": redir,
        "elapsedTime": elapsed,
    }, timeout=_TIMEOUT)
    return not _is_challenge(r)


def get(url: str, params=None) -> requests.Response:
    """Drop-in for requests.get that transparently solves Anubis challenges."""
    sess = session()
    for attempt in range(3):
        resp = sess.get(url, params=params, timeout=_TIMEOUT)
        if not _is_challenge(resp):
            return resp
        print(f"Anubis: challenge detected (attempt {attempt + 1}/3)", file=stderr)
        if not _solve(sess, resp):
            print("Anubis: could not solve challenge.", file=stderr)
            return resp
    return resp
