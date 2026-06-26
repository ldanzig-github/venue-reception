"""
Google Places API (New) client — primary source for Google ratings.

When `GOOGLE_PLACES_API_KEY` is set in the environment, the scraper
uses this module instead of headless-browser scraping for Google data.
Far more reliable and never blocked. Both endpoints cache to disk so the
30-min scrape loop doesn't re-bill the API on every cycle.

Two endpoints used:
  • POST /v1/places:searchText        — find a place_id from a text query (cached forever)
  • GET  /v1/places/{place_id}        — fetch rating + count + reviews (cached with TTL)

Field mask keeps response payload minimal so we stay in the cheap SKU.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_KEY_ENV = "GOOGLE_PLACES_API_KEY"
PLACE_ID_CACHE = Path(__file__).parent / "data" / "place_ids.json"

# Place Details are re-fetched on every 30-min scrape cycle by default, which
# re-bills the (expensive) Enterprise+Atmosphere SKU ~48x/day per venue even
# though ratings/reviews barely move within a day. Cache details on disk with a
# TTL so we hit the API at most once per TTL window. Override via env.
DETAILS_CACHE = Path(__file__).parent / "data" / "place_details.json"
DETAILS_TTL_SECONDS = int(os.getenv("PLACES_DETAILS_TTL_HOURS", "12")) * 3600

# Field masks let us pay only for fields we use (Places API New pricing).
SEARCH_FIELDS = "places.id,places.displayName"
DETAILS_FIELDS = ",".join([
    "id",
    "displayName",
    "rating",
    "userRatingCount",
    "googleMapsUri",
    "reviews",
])


def is_enabled() -> bool:
    return bool(os.getenv(API_KEY_ENV, "").strip())


def _load_cache(path: Path = PLACE_ID_CACHE) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict, path: Path = PLACE_ID_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))


def find_place_id(text_query: str) -> Optional[str]:
    """Resolve a text query to a Google place_id. Cached on disk forever."""
    cache = _load_cache()
    if text_query in cache and cache[text_query]:
        return cache[text_query]

    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        return None

    try:
        r = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": SEARCH_FIELDS,
            },
            json={"textQuery": text_query, "maxResultCount": 1},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        places = body.get("places") or []
        if not places:
            logger.warning(f"places searchText: no results for {text_query!r}")
            return None
        place_id = places[0].get("id")
        if place_id:
            cache[text_query] = place_id
            _save_cache(cache)
            logger.info(f"places searchText: cached {text_query!r} -> {place_id}")
        return place_id
    except Exception as e:
        logger.exception(f"places searchText failed for {text_query!r}: {e}")
        return None


def get_place_details(place_id: str) -> Optional[dict]:
    """Fetch rating / count / reviews for a place_id.

    Cached on disk with a TTL (default 12h, override via PLACES_DETAILS_TTL_HOURS)
    so the 30-min scrape loop doesn't re-bill the Enterprise+Atmosphere SKU on
    every cycle. On API error, falls back to stale cache when available.
    """
    cache = _load_cache(DETAILS_CACHE)
    entry = cache.get(place_id)
    if entry:
        age = time.time() - entry.get("fetched_at", 0)
        if age < DETAILS_TTL_SECONDS:
            logger.info(f"places details: cache hit for {place_id} (age {int(age // 60)}min)")
            return entry.get("payload")

    api_key = os.getenv(API_KEY_ENV, "").strip()
    if not api_key:
        return entry.get("payload") if entry else None
    try:
        r = requests.get(
            f"https://places.googleapis.com/v1/places/{place_id}",
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": DETAILS_FIELDS,
            },
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        cache[place_id] = {"fetched_at": time.time(), "payload": payload}
        _save_cache(cache, DETAILS_CACHE)
        logger.info(f"places details: fetched + cached fresh for {place_id}")
        return payload
    except Exception as e:
        logger.exception(f"places details failed for {place_id}: {e}")
        if entry:
            logger.warning(f"places details: serving stale cache for {place_id} after error")
            return entry.get("payload")
        return None


def fetch_google_data(text_query: str) -> Optional[dict]:
    """
    Top-level helper used by scraper.py. Returns a dict shaped like the
    headless scraper output:
        { rating, count, reviews, _source: { rating, count, reviews } }
    Or None if the API key isn't set or the call fails.
    """
    if not is_enabled():
        return None
    place_id = find_place_id(text_query)
    if not place_id:
        return None
    details = get_place_details(place_id)
    if not details:
        return None

    rating = details.get("rating")
    count = details.get("userRatingCount")
    raw_reviews = details.get("reviews") or []

    reviews = []
    for r in raw_reviews[:8]:  # API can return up to 5; grab all + extras for sorting
        author = (r.get("authorAttribution") or {}).get("displayName", "")
        text = (r.get("text") or {}).get("text", "")
        rel_time = r.get("relativePublishTimeDescription", "")
        publish_time = r.get("publishTime", "")  # ISO 8601 — used to sort by recency
        rev_rating = r.get("rating") or 5
        if author and text:
            reviews.append({
                "name": author,
                "date": rel_time,
                "publish_time": publish_time,
                "rating": int(rev_rating) if isinstance(rev_rating, (int, float)) else 5,
                "body": text[:260],
            })

    return {
        "rating": str(rating) if rating is not None else None,
        "count": str(count) if count is not None else None,
        "distribution": {},  # Places API doesn't return per-star breakdown
        "reviews": reviews,
        "place_id": place_id,  # exact Maps target for the headless scrape
        "_source": {
            "rating": "places-api" if rating is not None else None,
            "count": "places-api" if count is not None else None,
            "reviews": "places-api" if reviews else None,
        },
    }
