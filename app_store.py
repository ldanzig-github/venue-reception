"""
App store scraper — Apple App Store + Google Play.

Pulls rating, total review count, current version, and the most-recent
named-reviewer snippets for each app. Pure-API where possible (Apple's
iTunes lookup + RSS feeds), `google-play-scraper` library for Android.

Returns dashboard-shaped data the renderer's app tab consumes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ─── tracked apps ────────────────────────────────────────────────────────
APPS = [
    {
        "key": "courtyard",
        "name": "Courtyard",
        "tagline": "Trading cards marketplace",
        "ios_id": "6748155184",
        "android_id": "io.courtyard.app",
        "ios_url":     "https://apps.apple.com/us/app/courtyard-trading-cards/id6748155184",
        "android_url": "https://play.google.com/store/apps/details?id=io.courtyard.app&hl=en_US",
        "insight_kind": "good",
    },
    {
        "key": "triumph",
        "name": "Triumph",
        "tagline": "Play games for cash",
        "ios_id": "1608987929",
        "android_id": None,
        "ios_url":     "https://apps.apple.com/us/app/triumph-play-for-cash/id1608987929",
        "android_url": None,
        "insight_kind": "good",
    },
    {
        "key": "bezel",
        "name": "Bezel",
        "tagline": "Authenticated watches",
        "ios_id": "1586195658",
        "android_id": None,
        "ios_url":     "https://apps.apple.com/us/app/bezel-authenticated-watches/id1586195658",
        "android_url": None,
        "insight_kind": "good",
    },
    {
        "key": "jackpot",
        "name": "Jackpot",
        "tagline": "Lottery app",
        "ios_id": "1608194866",
        "android_id": "com.jackpot.lotteryservices",
        "ios_url":     "https://apps.apple.com/us/app/jackpot-lottery-app/id1608194866",
        "android_url": "https://play.google.com/store/apps/details?id=com.jackpot.lotteryservices&hl=en_US",
        "insight_kind": "good",
    },
    {
        "key": "solitaire_smash",
        "name": "Solitaire Smash",
        "tagline": "Real cash card games",
        "ios_id": "6446482475",
        "android_id": None,
        "ios_url":     "https://apps.apple.com/us/app/solitaire-smash-real-cash/id6446482475",
        "android_url": None,
        "insight_kind": "good",
    },
    {
        "key": "travel_sort",
        "name": "Travel Sort",
        "tagline": "Match puzzle game",
        "ios_id": "6752299562",
        "android_id": None,
        "ios_url":     "https://apps.apple.com/us/app/travel-sort-match-puzzle/id6752299562",
        "android_url": None,
        "insight_kind": "good",
    },
    {
        "key": "packz",
        "name": "Packz",
        "tagline": "Trading card pack openings",
        "ios_id": "6755495631",
        "android_id": None,
        "ios_url":     "https://apps.apple.com/us/app/packz/id6755495631",
        "android_url": None,
        "insight_kind": "good",
    },
]


# ─── Apple iTunes API ────────────────────────────────────────────────────
def _fetch_ios(app_id: str) -> Optional[dict]:
    """iTunes lookup — totally free, no auth, returns rating + count + version."""
    try:
        r = requests.get(
            f"https://itunes.apple.com/lookup?id={app_id}&country=us",
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            logger.warning(f"iTunes lookup: no results for {app_id}")
            return None
        d = results[0]
        return {
            "rating": d.get("averageUserRating"),
            "count": d.get("userRatingCount"),
            "version": d.get("version"),
            "release_date": d.get("currentVersionReleaseDate"),
            "icon": d.get("artworkUrl512") or d.get("artworkUrl100"),
            "url": d.get("trackViewUrl"),
            "developer": d.get("artistName"),
            "price": d.get("formattedPrice"),
            # Parallel arrays — used to resolve App Store chart rank below.
            "genre_ids": d.get("genreIds") or [],
            "genre_names": d.get("genres") or [],
        }
    except Exception as e:
        logger.exception(f"iTunes lookup failed for {app_id}: {e}")
        return None


def _fetch_ios_reviews(app_id: str, n: int = 50) -> list[dict]:
    """RSS feed — returns up to N most-recent reviews (1 page = ~50)."""
    try:
        r = requests.get(
            f"https://itunes.apple.com/us/rss/customerreviews/page=1/id={app_id}/sortby=mostrecent/json",
            timeout=15,
        )
        r.raise_for_status()
        feed = r.json().get("feed") or {}
        entries = feed.get("entry") or []
        # First entry is app metadata (no rating). Skip it.
        reviews = []
        for e in entries[1 : n + 1]:
            try:
                reviews.append({
                    "name": ((e.get("author") or {}).get("name") or {}).get("label", "Anonymous"),
                    "rating": int(((e.get("im:rating") or {}).get("label") or 5)),
                    "title": ((e.get("title") or {}).get("label") or "").strip(),
                    "body": ((e.get("content") or {}).get("label") or "").strip()[:280],
                    "version": ((e.get("im:version") or {}).get("label") or ""),
                    "publish_time": ((e.get("updated") or {}).get("label") or ""),
                    "url": "",
                    "source": "ios",
                })
            except Exception as inner:
                logger.warning(f"iTunes RSS entry parse error: {inner}")
        return reviews
    except Exception as e:
        logger.warning(f"iTunes RSS failed for {app_id}: {e}")
        return []


# ─── App Store chart rank ────────────────────────────────────────────────
# Apple's Top Free RSS feed lists apps in rank order — an app's position in
# the list IS its rank. Free, no auth, and directly verifiable: open the same
# URL and count. Every tracked app is a free download, so Top Free is the one
# chart that applies consistently across the portfolio.
_RANK_CHART = "topfreeapplications"
_RANK_CHART_LABEL = "Top Free"
_RANK_LIMIT = 200


def _fetch_chart(genre_id: str, cache: dict) -> list[str]:
    """Ordered list of app IDs in a genre's Top Free chart. Cached per scrape cycle."""
    if genre_id in cache:
        return cache[genre_id]
    ids: list[str] = []
    try:
        r = requests.get(
            f"https://itunes.apple.com/us/rss/{_RANK_CHART}"
            f"/limit={_RANK_LIMIT}/genre={genre_id}/json",
            timeout=15,
        )
        r.raise_for_status()
        entries = (r.json().get("feed") or {}).get("entry") or []
        ids = [
            ((e.get("id") or {}).get("attributes") or {}).get("im:id")
            for e in entries
        ]
        ids = [i for i in ids if i]
    except Exception as e:
        logger.warning(f"chart fetch failed for genre {genre_id}: {e}")
    cache[genre_id] = ids
    return ids


def _fetch_chart_rank(
    app_id: str, genre_ids: list, genre_names: list, cache: dict
) -> Optional[dict]:
    """
    Best (lowest) verified rank for an app across every App Store genre it
    belongs to. Returns {"rank", "genre", "genre_id", "chart"} or None when
    the app is outside the top 200 of all its genres — we never show a rank
    that can't be confirmed against Apple's published chart.
    """
    if not app_id or not genre_ids:
        return None
    id_to_name = dict(zip(genre_ids, genre_names or []))
    best = None
    for gid in genre_ids:
        ids = _fetch_chart(str(gid), cache)
        if app_id in ids:
            rank = ids.index(app_id) + 1
            if best is None or rank < best["rank"]:
                best = {
                    "rank": rank,
                    "genre": id_to_name.get(gid) or f"genre {gid}",
                    "genre_id": str(gid),
                    "chart": _RANK_CHART_LABEL,
                }
    return best


# ─── App Store rating histogram (amp-api) ────────────────────────────────
# iTunes lookup gives only the average + total count — no star breakdown.
# The lifetime histogram the App Store shows comes from Apple's internal
# amp-api, which needs a bearer token. We harvest a fresh token by loading
# an App Store page in headless Chromium (same engine the venue scraper
# uses) and intercepting the request Apple's own page makes — letting
# Apple mint the token keeps this durable vs. hardcoding one. The token is
# a long-lived JWT, so it's cached on disk and reused across cycles.
_AMP_TOKEN_PATH = Path(__file__).parent / "data" / ".amp_token"
_AMP_HARVEST_APP_ID = "1608987929"  # any App Store page works


def _harvest_amp_token() -> Optional[str]:
    """Intercept a fresh amp-api bearer token from an App Store web page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright unavailable — cannot harvest amp-api token")
        return None
    grabbed: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            def _on_request(req):
                auth = req.headers.get("authorization", "")
                if "amp-api" in req.url and auth.lower().startswith("bearer "):
                    grabbed["token"] = auth.split(" ", 1)[1]

            page.on("request", _on_request)
            try:
                page.goto(
                    f"https://apps.apple.com/us/app/id{_AMP_HARVEST_APP_ID}",
                    wait_until="load", timeout=30000,
                )
                page.wait_for_timeout(3000)  # let the amp-api XHRs fire
            except Exception as e:
                logger.warning(f"amp-api harvest page load: {e}")
            browser.close()
    except Exception as e:
        logger.warning(f"amp-api token harvest failed: {e}")
    return grabbed.get("token")


def _get_amp_token(force_refresh: bool = False) -> Optional[str]:
    """Cached amp-api token; harvests a fresh one when missing or forced."""
    if not force_refresh:
        try:
            cached = _AMP_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"amp-api token cache read failed: {e}")
    token = _harvest_amp_token()
    if token:
        try:
            _AMP_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AMP_TOKEN_PATH.write_text(token, encoding="utf-8")
        except Exception as e:
            logger.warning(f"amp-api token cache write failed: {e}")
    return token


def _fetch_ios_histogram(app_id: str, token: str):
    """
    Verified lifetime star histogram for an iOS app from Apple's amp-api —
    the exact data the App Store renders. Returns {"5":n,...,"1":n}, the
    string "EXPIRED" when the token is stale, or None on any other miss.
    """
    if not token:
        return None
    try:
        r = requests.get(
            f"https://amp-api.apps.apple.com/v1/catalog/us/apps/{app_id}",
            params={"platform": "web", "l": "en-US"},
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://apps.apple.com",
            },
            timeout=15,
        )
        if r.status_code in (401, 403):
            return "EXPIRED"
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            return None
        attrs = data[0].get("attributes") or {}
        ur = attrs.get("userRating") or {}
        rcl = ur.get("ratingCountList")
        if not isinstance(rcl, list) or len(rcl) != 5:
            # One-time diagnostic so a single deploy reveals the response shape.
            logger.warning(
                f"amp-api {app_id}: no ratingCountList — "
                f"userRating keys={list(ur.keys())}  attr keys={list(attrs.keys())[:14]}"
            )
            return None
        # amp-api ratingCountList is ascending: index 0 = 1★ … index 4 = 5★.
        return {
            "1": int(rcl[0]), "2": int(rcl[1]), "3": int(rcl[2]),
            "4": int(rcl[3]), "5": int(rcl[4]),
        }
    except Exception as e:
        logger.warning(f"amp-api histogram failed for {app_id}: {e}")
        return None


# ─── Google Play (via google-play-scraper) ───────────────────────────────
def _fetch_android(package_name: str) -> Optional[dict]:
    try:
        from google_play_scraper import app as gps_app  # noqa: WPS433
        d = gps_app(package_name, lang="en", country="us")
        # `histogram` is [count_1★, count_2★, count_3★, count_4★, count_5★]
        hist = d.get("histogram") or []
        # Normalize to {"5": n, "4": n, ...} dict for renderer's distribution code
        dist = {}
        if isinstance(hist, list) and len(hist) == 5:
            for stars, count in zip([1, 2, 3, 4, 5], hist):
                dist[str(stars)] = int(count or 0)
        return {
            "rating": d.get("score"),
            "count": d.get("ratings") or d.get("reviews"),
            "version": d.get("version"),
            "release_date": str(d.get("updated", "")),
            "icon": d.get("icon"),
            "url": d.get("url"),
            "developer": d.get("developer"),
            "price": "Free" if d.get("free") else (d.get("priceText") or ""),
            "installs": d.get("installs"),
            "distribution": dist,  # {"5": n, "4": n, "3": n, "2": n, "1": n}
        }
    except Exception as e:
        logger.exception(f"google-play-scraper app failed for {package_name}: {e}")
        return None


def _fetch_android_reviews(package_name: str, n: int = 4) -> list[dict]:
    try:
        from google_play_scraper import reviews as gps_reviews, Sort  # noqa: WPS433
        result, _ = gps_reviews(
            package_name, lang="en", country="us", count=n, sort=Sort.NEWEST
        )
        out = []
        for r in result:
            at = r.get("at")
            out.append({
                "name": r.get("userName") or "Anonymous",
                "rating": int(r.get("score") or 5),
                "title": "",
                "body": (r.get("content") or "")[:280],
                "version": r.get("reviewCreatedVersion") or "",
                "publish_time": at.isoformat() if at else "",
                "url": "",
                "source": "android",
            })
        return out
    except Exception as e:
        logger.warning(f"google-play-scraper reviews failed for {package_name}: {e}")
        return []


# ─── public API ───────────────────────────────────────────────────────────
def scrape_all_apps() -> dict:
    """
    Scrape all configured apps. Returns a dict shaped for the renderer:
      { "last_scrape": "...", "apps": { <key>: { ios: {...}, android: {...},
                                                  reviews: [...], _source: {...} } } }
    """
    out_apps = {}
    chart_cache: dict = {}  # genre_id -> ordered app-id list, shared across this cycle
    amp_token = _get_amp_token()  # for verified iOS lifetime histograms
    for app in APPS:
        ios_data = _fetch_ios(app["ios_id"]) if app.get("ios_id") else None
        android_data = _fetch_android(app["android_id"]) if app.get("android_id") else None
        ios_reviews = _fetch_ios_reviews(app["ios_id"]) if ios_data else []
        android_reviews = _fetch_android_reviews(app["android_id"]) if android_data else []

        # Verified lifetime star histogram from Apple's amp-api. On a stale
        # token (401), refresh once and retry — covers the ~6-month expiry.
        if ios_data and amp_token:
            hist = _fetch_ios_histogram(app["ios_id"], amp_token)
            if hist == "EXPIRED":
                amp_token = _get_amp_token(force_refresh=True)
                hist = _fetch_ios_histogram(app["ios_id"], amp_token) if amp_token else None
            if isinstance(hist, dict):
                ios_data["distribution"] = hist

        # Verified App Store chart rank (None when outside the top 200).
        rank = (
            _fetch_chart_rank(
                app["ios_id"],
                ios_data.get("genre_ids") or [],
                ios_data.get("genre_names") or [],
                chart_cache,
            )
            if ios_data
            else None
        )

        # Merge reviews from both stores, sort by publish time desc, take top 4.
        merged = []
        for r in ios_reviews:
            r2 = dict(r)
            r2["_date_key"] = _parse_date(r.get("publish_time", ""))
            merged.append(r2)
        for r in android_reviews:
            r2 = dict(r)
            r2["_date_key"] = _parse_date(r.get("publish_time", ""))
            merged.append(r2)
        merged.sort(key=lambda x: x.get("_date_key") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        for r in merged:
            r.pop("_date_key", None)

        ios_count = (ios_data or {}).get("count")
        android_count = (android_data or {}).get("count")
        # Combined count for trends — sum across stores so cross-store growth is captured.
        combined_count = (ios_count or 0) + (android_count or 0)
        # Combined rating — weighted avg by count when both exist.
        combined_rating = None
        ios_rating = (ios_data or {}).get("rating")
        android_rating = (android_data or {}).get("rating")
        if ios_count and android_count and ios_rating and android_rating:
            combined_rating = round(
                (ios_rating * ios_count + android_rating * android_count) / combined_count, 2
            )
        elif ios_rating is not None:
            combined_rating = ios_rating
        elif android_rating is not None:
            combined_rating = android_rating

        # ── Analytics computed from the full merged review list ─────────
        analytics = _compute_app_analytics(ios_reviews, android_reviews, ios_data, android_data)

        # ── Distribution: prefer Android histogram (covers ALL reviews ever),
        #    fall back to recent-review distribution sampled from RSS for iOS-only apps.
        distribution = (android_data or {}).get("distribution") or {}
        if not distribution:
            distribution = analytics["recent_distribution"]

        out_apps[app["key"]] = {
            "ios": ios_data or {},
            "android": android_data or {},
            "reviews": merged[:4],
            "combined": {
                "rating": combined_rating,
                "count": combined_count if combined_count else None,
            },
            "distribution": distribution,
            "analytics": analytics,
            "rank": rank,
        }

        # Logging line per app for journalctl debugging
        rank_str = f"#{rank['rank']} {rank['genre']}" if rank else "—"
        ios_hist = (ios_data or {}).get("distribution")
        hist_str = (
            "/".join(str(ios_hist.get(s, 0)) for s in ("5", "4", "3", "2", "1"))
            if ios_hist else "—"
        )
        logger.info(
            f"app {app['key']}: "
            f"iOS={ios_rating}/{ios_count}  "
            f"Android={android_rating}/{android_count}  "
            f"reviews={len(merged)}  "
            f"velocity={analytics['velocity_per_week']}/wk  "
            f"positive={analytics['positive_pct']}%  "
            f"rank={rank_str}  "
            f"hist[5-1]={hist_str}"
        )

    return {
        "last_scrape": datetime.now().strftime("%b %-d, %Y · %-I:%M %p"),
        "apps": out_apps,
    }


def _compute_app_analytics(ios_reviews, android_reviews, ios_data, android_data) -> dict:
    """
    Compute richer review analytics from the full pulled review pool:
      - velocity_per_week   : reviews per week, derived from the most recent N reviews' span
      - positive_pct        : % of recent reviews rated 4+
      - recent_distribution : {"5": n, ...} computed from recent reviews (used when
                              no store-side histogram is available, i.e. iOS-only apps)
      - version_breakdown   : recent rating per current/previous version
      - cross_store_gap     : iOS rating - Android rating (or None)
      - sample_size         : how many reviews the analytics were computed over
    """
    all_reviews = list(ios_reviews) + list(android_reviews)
    sample = len(all_reviews)

    # Velocity: how many reviews span how much wall-clock time?
    dated = []
    for r in all_reviews:
        d = _parse_date(r.get("publish_time", ""))
        if d:
            dated.append((d, r.get("rating") or 5, r.get("version") or ""))
    velocity = None
    if len(dated) >= 5:
        dated.sort(key=lambda x: x[0])
        span_days = (dated[-1][0] - dated[0][0]).total_seconds() / 86400
        if span_days >= 1:
            velocity = round(len(dated) / span_days * 7, 1)
        else:
            velocity = float(len(dated) * 7)

    # Sentiment %
    positive = sum(1 for r in all_reviews if (r.get("rating") or 0) >= 4)
    positive_pct = round(100 * positive / sample) if sample else None

    # Recent distribution from the sampled reviews
    recent_dist = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    for r in all_reviews:
        s = str(int(r.get("rating") or 0))
        if s in recent_dist:
            recent_dist[s] += 1

    # Per-version breakdown — only for the current iOS or Android version.
    cur_version = (ios_data or {}).get("version") or (android_data or {}).get("version") or ""
    version_breakdown = None
    if cur_version:
        v_reviews = [r for r in all_reviews if (r.get("version") or "") == cur_version]
        if v_reviews:
            v_avg = sum((r.get("rating") or 5) for r in v_reviews) / len(v_reviews)
            version_breakdown = {
                "version": cur_version,
                "rating": round(v_avg, 2),
                "count": len(v_reviews),
            }

    # Cross-store gap
    ios_r = (ios_data or {}).get("rating")
    and_r = (android_data or {}).get("rating")
    cross_store_gap = round(ios_r - and_r, 2) if (ios_r is not None and and_r is not None) else None

    return {
        "velocity_per_week": velocity,
        "positive_pct": positive_pct,
        "recent_distribution": recent_dist,
        "version_breakdown": version_breakdown,
        "cross_store_gap": cross_store_gap,
        "sample_size": sample,
    }


def _parse_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    # ISO 8601 (Apple RSS uses this; google-play-scraper datetime exported as ISO)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Force UTC if naive — Apple RSS gives tz-aware, Android library gives naive,
        # and a mixed list is not sortable.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    return None
