"""
HTTP + CSV plumbing. Pure stdlib except `requests`.

Every remote file is cached to data/cache/ with a TTL so a re-run during the day
(or a debugging session) does not re-download ~60MB of nflverse releases. The
daily task passes force=True for the things that actually change daily (ADP,
Sleeper injury status) and leaves last season's finished stats cached.
"""
from __future__ import annotations

import csv
import gzip
import io
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import CACHE_DIR, HTTP_RETRIES, HTTP_TIMEOUT, LOG_DIR, USER_AGENT

# The Windows console defaults to cp1252, so printing a name with an accent
# ("Pineiro", "Aiyuk") raises UnicodeEncodeError and would kill the daily task
# mid-fetch. Force UTF-8 on both streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_session = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        _session = s
    return _session


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / "fetch.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _cache_path(url: str) -> Path:
    safe = url.split("://", 1)[-1]
    for ch in '/\\:?&=%*"<>|':
        safe = safe.replace(ch, "_")
    return CACHE_DIR / safe[:180]


def fetch_bytes(url: str, ttl_hours: float = 24.0, force: bool = False) -> bytes:
    """GET with retry + on-disk cache. Returns raw (already gunzipped) bytes."""
    cp = _cache_path(url)
    if not force and cp.exists():
        age_h = (time.time() - cp.stat().st_mtime) / 3600.0
        if age_h < ttl_hours and cp.stat().st_size > 0:
            return cp.read_bytes()

    last = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session().get(url, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                raise FileNotFoundError(f"404 {url}")
            r.raise_for_status()
            raw = r.content
            if url.endswith(".gz"):
                raw = gzip.decompress(raw)
            cp.write_bytes(raw)
            return raw
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last = exc
            if attempt < HTTP_RETRIES:
                time.sleep(1.5 * attempt)
    # Fall back to a stale cache rather than failing the whole run.
    if cp.exists() and cp.stat().st_size > 0:
        log(f"  ! {url} failed ({last}); using stale cache")
        return cp.read_bytes()
    raise RuntimeError(f"fetch failed: {url}: {last}")


def decode(raw: bytes) -> str:
    """Decode upstream text.

    FantasyFootballCalculator serves cp1252 for accented names without declaring
    it, so a strict utf-8 decode fails and a lossy one turns 'Pineiro' into
    'Pi�eiro'. Try utf-8 first (correct for every nflverse file), then
    cp1252, and only then give up and replace.
    """
    for enc in ("utf-8", "cp1252"):
        try:
            text = raw.decode(enc)
            return text[1:] if text[:1] == "﻿" else text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def fetch_csv(url: str, ttl_hours: float = 24.0, force: bool = False) -> list:
    """Return a list of dicts from a (possibly gzipped) remote CSV."""
    return list(csv.DictReader(io.StringIO(decode(fetch_bytes(url, ttl_hours, force)))))


def fetch_json(url: str, ttl_hours: float = 6.0, force: bool = False):
    import json

    return json.loads(decode(fetch_bytes(url, ttl_hours, force)))


# ---------------------------------------------------------------- coercion --
def f(val, default=0.0) -> float:
    """CSV -> float, tolerating '', 'NA', None."""
    if val is None:
        return default
    s = str(val).strip()
    if not s or s.upper() in ("NA", "NULL", "NONE", "NAN"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def i(val, default=0) -> int:
    return int(f(val, default))


def s(val, default="") -> str:
    if val is None:
        return default
    out = str(val).strip()
    return out if out and out.upper() not in ("NA", "NULL", "NONE") else default


def norm_name(name: str) -> str:
    """Normalise a player name for cross-source joining.

    Sources disagree on suffixes, punctuation, spacing AND diacritics:
      'Marvin Harrison Jr.' / 'Marvin Harrison'  -> marvinharrison
      "Ja'Marr Chase"                            -> jamarrchase
      'Kenneth Walker III'                       -> kennethwalker
      'Eddy Pineiro' (Sleeper) / 'Eddy Pineiro' with a tilde (FFC) -> eddypineiro

    That last one is not hypothetical: the accent mismatch silently dropped him
    from the ADP join until the fold was added.
    """
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    n = n.lower().strip()
    n = "".join(ch for ch in n if ch.isalnum())
    for suf in ("jr", "sr", "iii", "iv", "ii", "v"):
        if n.endswith(suf) and len(n) > len(suf) + 3:
            n = n[: -len(suf)]
    return n


# nflverse and Sleeper disagree on a handful of franchise abbreviations.
# "AZ" is the one that matters most: Sleeper spells Arizona AZ while every other
# source uses ARI, which silently split the Cardinals into two half-teams and
# wrecked their projections until it was caught.
TEAM_FIX = {
    "LAR": "LA", "STL": "LA", "SD": "LAC", "OAK": "LV", "WSH": "WAS",
    "ARZ": "ARI", "AZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU",
    "JAC": "JAX", "LVR": "LV", "GNB": "GB", "KAN": "KC", "NWE": "NE",
    "NOR": "NO", "SFO": "SF", "TAM": "TB",
}


def norm_team(team: str) -> str:
    t = s(team).upper()
    return TEAM_FIX.get(t, t)


def today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
