"""
Gridiron Edge - local server.

This serves the SAME static build that GitHub Pages serves, so what you see
locally is byte-for-byte what you get on your phone. There is one frontend and
one valuation engine (static/engine.js), not a local variant and a hosted
variant that can drift apart.

Python's job is everything that depends only on the data and the scoring system:
fetch, projections, coaching context, injury profiles. That output is written to
docs/ by export_static.py. The browser does everything that depends on your
league. Run:

    python -m data.fetch_all      # refresh the data
    python export_static.py       # rebuild docs/
    python app.py                 # serve it

app.py rebuilds docs/ automatically when the data has moved on, so in practice
you only ever run the fetch.
"""
from __future__ import annotations

import os

from flask import Flask, abort, send_from_directory

from config import BASE_DIR, HOST, PORT
from models.database import db, get_meta, init_db

DOCS = BASE_DIR / "docs"

app = Flask(__name__, static_folder=None)


def _ensure_build() -> None:
    """Rebuild the static site if it is missing or older than the data."""
    meta = DOCS / "data" / "meta.json"
    # An encrypted build has no readable meta.json. Rebuilding over the top of
    # one would silently replace ciphertext with plaintext, so leave it alone.
    manifest = DOCS / "data" / "manifest.json"
    if manifest.exists():
        import json as _json
        try:
            if _json.loads(manifest.read_text(encoding="utf-8")).get("encrypted"):
                print("Encrypted build present - leaving docs/ untouched.")
                return
        except (ValueError, OSError):
            pass
    try:
        with db() as conn:
            version = get_meta(conn, "data_version", "0")
    except Exception:  # noqa: BLE001 - no database yet
        version = None

    stale = True
    if meta.exists():
        import json
        try:
            built = json.loads(meta.read_text(encoding="utf-8")).get("data_version")
            stale = (version is not None and str(built) != str(version))
        except (ValueError, OSError):
            stale = True

    if stale:
        print("Data has moved on - rebuilding docs/ ...")
        from export_static import build
        build()


@app.after_request
def _no_cache(resp):
    # The whole point of the daily refresh is that the numbers change. Serving a
    # cached board on draft morning would be the worst possible failure.
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/")
def index():
    return send_from_directory(DOCS, "index.html")


@app.route("/auction.html")
def auction_sheet():
    return send_from_directory(DOCS, "auction.html")


@app.route("/<path:filename>")
def static_file(filename):
    target = (DOCS / filename).resolve()
    try:
        target.relative_to(DOCS.resolve())
    except ValueError:
        abort(404)          # no escaping the build directory
    if not target.is_file():
        abort(404)
    return send_from_directory(DOCS, filename)


if __name__ == "__main__":
    init_db()
    _ensure_build()
    print(f"\n  Gridiron Edge  ->  http://{HOST}:{PORT}\n")
    use_reload = os.environ.get("FF_NO_RELOAD") != "1"
    app.run(host=HOST, port=PORT, debug=use_reload, use_reloader=use_reload)
