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
    for app in APPS:
        ios_data = _fetch_ios(app["ios_id"]) if app.get("ios_id") else None
        android_data = _fetch_android(app["android_id"]) if app.get("android_id") else None
        ios_reviews = _fetch_ios_reviews(app["ios_id"]) if ios_data else []
        android_reviews = _fetch_android_reviews(app["android_id"]) if android_data else []

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
        }

        # Logging line per app for journalctl debugging
        logger.info(
            f"app {app['key']}: "
            f"iOS={ios_rating}/{ios_count}  "
            f"Android={android_rating}/{android_count}  "
            f"reviews={len(merged)}  "
            f"velocity={analytics['velocity_per_week']}/wk  "
            f"positive={analytics['positive_pct']}%"
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
