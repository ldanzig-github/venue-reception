"""
Standalone Flask server for the venue reception dashboard.

Single process: Flask serves the rendered HTML, APScheduler runs the
scraper periodically in a background thread. Behind gunicorn for
production via venue-dashboard.service.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, Response, request

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("venue-reception")


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR") or PROJECT_ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
HTML_PATH = DATA_DIR / "venue-reception.html"
JSON_PATH = DATA_DIR / "venue-reception.json"

PATH_TOKEN = os.getenv("VENUE_RECEPTION_PATH_TOKEN", "").strip()
BASIC_AUTH = os.getenv("VENUE_RECEPTION_BASIC_AUTH", "").strip()
SCRAPE_INTERVAL_MIN = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "30"))


def _do_scrape_cycle():
    """Called by APScheduler. Idempotent — overwrites the output file each run."""
    try:
        from scraper import scrape_all_venues
        from renderer import write_dashboard

        log.info("starting scrape cycle")
        data = scrape_all_venues(headless=True)
        write_dashboard(data, html_path=HTML_PATH, json_path=JSON_PATH)
        log.info(f"wrote {HTML_PATH}")
    except Exception:
        log.exception("scrape cycle failed; previous file kept")


def _check_basic_auth():
    if not BASIC_AUTH:
        return True
    auth = request.authorization
    if not auth:
        return False
    expected_user, _, expected_pw = BASIC_AUTH.partition(":")
    return auth.username == expected_user and auth.password == expected_pw


def _basic_auth_response():
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="venue-reception"'},
    )


# ─── Flask app ────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def root():
    return Response("ok", 200, {"Content-Type": "text/plain"})


@app.route("/healthz")
def healthz():
    return {"status": "ok", "html_exists": HTML_PATH.exists()}


# Dashboard route — gated by path token (mandatory) + optional basic auth.
@app.route(f"/dashboard/{PATH_TOKEN}" if PATH_TOKEN else "/dashboard/<token>")
def dashboard(token: str | None = None):
    # If PATH_TOKEN is unset, require any token in the URL but reject all of them.
    # Operator MUST configure VENUE_RECEPTION_PATH_TOKEN for this route to work.
    if not PATH_TOKEN:
        return Response("Server not configured: VENUE_RECEPTION_PATH_TOKEN missing", 500)
    if not _check_basic_auth():
        return _basic_auth_response()
    if not HTML_PATH.exists():
        return Response(
            "Dashboard hasn't been rendered yet — first scrape cycle is in progress. "
            "Check back in ~60 seconds.",
            503,
            {"Content-Type": "text/plain"},
        )
    return Response(
        HTML_PATH.read_text(encoding="utf-8"),
        200,
        {
            "Content-Type": "text/html; charset=utf-8",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Cache-Control": "public, max-age=60, must-revalidate",
        },
    )


# ─── scheduler ────────────────────────────────────────────────────────────
def _start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _do_scrape_cycle,
        trigger="interval",
        minutes=SCRAPE_INTERVAL_MIN,
        next_run_time=None,  # don't fire immediately under gunicorn — see below
        id="venue-reception-scrape",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    log.info(f"scheduler started, interval={SCRAPE_INTERVAL_MIN} min")
    # Kick off one immediate run in a thread so it doesn't block worker startup.
    import threading
    threading.Thread(target=_do_scrape_cycle, daemon=True).start()


# Only start scheduler in the gunicorn master/worker context, not on import-only.
if os.getenv("DISABLE_SCHEDULER") != "1":
    _start_scheduler()


if __name__ == "__main__":
    # Dev mode: flask run / python server.py
    port = int(os.getenv("PORT", "8090"))
    app.run(host="0.0.0.0", port=port, debug=False)
