"""
Venue reception scraper — Google Maps + Tripadvisor reviews.

Drives a headless Chromium via Playwright. Returns a JSON-serializable dict
shaped for the renderer in renderer.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

# playwright-stealth is a soft dependency: if it's not installed, the
# scraper still runs but is more easily detected as headless by Google.
try:
    from playwright_stealth import stealth_sync as _apply_stealth  # type: ignore
except ImportError:
    _apply_stealth = None

# Places API is the preferred Google source when GOOGLE_PLACES_API_KEY is set.
import places_api

logger = logging.getLogger(__name__)


def _parse_review_date(rev: dict) -> datetime:
    """
    Best-effort timestamp for sorting reviews by recency. Returns datetime
    in UTC. Falls back to a sentinel old date so missing/unparseable
    timestamps sort to the bottom.
    """
    # Places API ISO 8601 publish_time is the gold standard.
    pt = rev.get("publish_time") or ""
    if pt:
        try:
            return datetime.fromisoformat(pt.replace("Z", "+00:00"))
        except Exception:
            pass

    # Relative phrases: "12 hours ago", "a month ago", "3 weeks ago"
    desc = (rev.get("date") or "").lower().strip()
    m = re.match(r"(\d+|a|an)\s+(hour|day|week|month|year)s?\s+ago", desc)
    if m:
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit = m.group(2)
        delta = {
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
            "year": timedelta(days=365 * n),
        }[unit]
        return datetime.now(timezone.utc) - delta

    # Tripadvisor-style "Apr 17, 2026" / "Apr 2026"
    cleaned = re.sub(r"•.*$", "", desc).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return datetime(1970, 1, 1, tzinfo=timezone.utc)


# ─── target venues ───────────────────────────────────────────────────────
# Two-stage Google strategy applied uniformly to every google entry:
#   1. /maps/search/ — usually returns full venue panel (rating + count + reviews)
#   2. /search?q=...  — fallback if Maps serves the "limited view" headless
#      mode sometimes gets, which strips the count and breakdown
# `search_q` is the query string used for the /search?q= fallback. Adding
# a new venue: just add a row with `key`, `type:"google"`, `url`, `search_q`.
VENUE_TARGETS = [
    {"key": "poolhouse",      "type": "google",
     "url": "https://www.google.com/maps/search/Poolhouse+100+Liverpool+Street+London+EC2",
     "search_q":     "Poolhouse 100 Liverpool Street London",
     "places_query": "Poolhouse 100 Liverpool Street London EC2M"},
    {"key": "poolhouse_trip", "type": "tripadvisor",
     "url": "https://www.tripadvisor.com/Attraction_Review-g186338-d34271730-Reviews-Poolhouse-London_England.html"},
    {"key": "philly",         "type": "google",
     "url": "https://www.google.com/maps/search/Ballers+1325+N+Beach+Street+Philadelphia",
     "search_q":     "Ballers Fishtown Philadelphia",
     "places_query": "Ballers 1325 N Beach St Philadelphia"},
    {"key": "boston",         "type": "google",
     "url": "https://www.google.com/maps/search/Ballers+25+Pier+4+Boulevard+Boston+Seaport",
     "search_q":     "Ballers Boston Seaport",
     "places_query": "Ballers 25 Pier 4 Blvd Boston Seaport"},
    {"key": "dubai",          "type": "google",
     "url": "https://www.google.com/maps/search/Five+Iron+Golf+Westin+Mina+Seyahi+Dubai",
     "search_q":     "Five Iron Golf Dubai Marina",
     "places_query": "Five Iron Golf Westin Mina Seyahi Dubai Marina"},
    {"key": "dubai_trip",     "type": "tripadvisor",
     "url": "https://www.tripadvisor.com/Attraction_Review-g295424-d33368076-Reviews-Five_Iron_Golf-Dubai_Emirate_of_Dubai.html"},
]


# ─── Tripadvisor fallback values ─────────────────────────────────────────
# Tripadvisor blocks data-center IPs aggressively (CAPTCHA / anti-bot),
# so live scrapes from a VPS are unreliable. Verified manually May 1, 2026
# via a residential browser. Update by editing this dict whenever you
# check those pages live. Google data is NEVER faked — if Google scrape
# fails, the dashboard panel for that venue stays empty.
TRIP_FALLBACKS = {
    "poolhouse_trip": {
        "rating": "5.0", "count": "4", "ranking": "#290 of 1,007",
        "reviews": [
            {"name": "Ian S",    "date": "Apr 17, 2026", "title": "Fun evening with friends",
             "body": "Lively, impressive, spacious venue. Pool table tech and games are superb."},
            {"name": "Paul K",   "date": "Apr 9, 2026",  "title": "Pool house the next pool generation in",
             "body": "What an awesome experience! Interactive pool that creates a level playing field for all players. This will become big."},
            {"name": "Daniel M", "date": "Apr 9, 2026",  "title": "Great night out!",
             "body": "Great place, great service, great food and drinks. And the pool games are a lot of fun. Special thanks to Ethan for helping to explain how it all worked!"},
            {"name": "Michael P","date": "Apr 9, 2026",  "title": "Awesome Experience",
             "body": "An absolutely awesome concept beautifully executed with great food and super friendly staff."},
        ],
    },
    "dubai_trip": {
        "rating": "5.0", "count": "377", "ranking": "#1 of 474",
        "reviews": [
            {"name": "Wanderer65209268966", "date": "Apr 2026", "title": "Minigolf, Amazing review",
             "body": "Mini golf was amazing, Emma and JC were super helpful! 10/10 would recommend."},
            {"name": "Divya K",              "date": "Apr 2026", "title": "Miniature Golf",
             "body": "Tried the 9 hole miniature golf and had a blast! Emma was an absolute pleasure and helped us out with everything."},
            {"name": "Naveen n",             "date": "Apr 2026", "title": "Great Time Well Spent",
             "body": "I had a great time and really enjoyed every moment. It was refreshing."},
            {"name": "Dreamer25511642592",   "date": "Apr 2026", "title": "Good",
             "body": "Emma hospitality is very good"},
        ],
    },
}


GOOGLE_JS = r"""
() => {
  const txt = document.body.innerText || "";

  // Primary: visible "<rating> (<count>)" text in the venue panel header.
  const m = txt.match(/(\d\.\d)\s*\(([\d,]+)\)/);
  let rating = m ? m[1] : null;
  let count = m ? m[2] : null;
  // Track which extraction path each field came from — surfaced in logs.
  const source = { rating: rating ? "text" : null, count: count ? "text" : null };

  // Distribution from the per-star aria-labels (e.g. "5 stars, 82 reviews").
  const stars = [];
  document.querySelectorAll('[role="img"][aria-label*="reviews"]').forEach(
    (el) => stars.push(el.getAttribute("aria-label") || "")
  );
  const dist = {};
  stars.forEach((s) => {
    const mm = s.match(/(\d)\s*stars?,\s*([\d,]+)\s*reviews?/);
    if (mm) dist[mm[1]] = parseInt(mm[2].replace(/,/g, ""));
  });

  // Fallback A: derive count from the distribution sum when the visible
  // "(N)" text never rendered (happens for some venue panels in headless).
  if (!count) {
    const total = Object.values(dist).reduce((a, b) => a + b, 0);
    if (total > 0) { count = String(total); source.count = "dist-sum"; }
  }

  // Fallback B: pull rating from "X.X stars" aria-label if the visible
  // "<rating> (<count>)" text never matched.
  if (!rating) {
    const ratingEl = Array.from(document.querySelectorAll('[role="img"][aria-label]')).find(
      (el) => /^\s*\d\.\d\s*stars?\s*$/.test(el.getAttribute("aria-label") || "")
    );
    if (ratingEl) {
      const rm = (ratingEl.getAttribute("aria-label") || "").match(/(\d\.\d)/);
      if (rm) { rating = rm[1]; source.rating = "aria-label"; }
    }
  }

  const reviewNodes = document.querySelectorAll("div[data-review-id]");
  const seen = new Set();
  const reviews = [];
  reviewNodes.forEach((node) => {
    if (reviews.length >= 4) return;
    const rid = node.getAttribute("data-review-id") || "";
    if (seen.has(rid)) return;
    seen.add(rid);
    const name = (node.querySelector(".d4r55")?.innerText || "").trim();
    const date = (node.querySelector(".rsqaWe, .DU9Pgb")?.innerText || "").trim();
    const ratingEl = node.querySelector('[role="img"][aria-label*="star"]');
    const rl = ratingEl ? ratingEl.getAttribute("aria-label") || "" : "";
    const rm = rl.match(/(\d)/);
    const r = rm ? parseInt(rm[1]) : null;
    const body = ((node.querySelector(".MyEned, .wiI7pd")?.innerText) || "")
      .replace(/…\s*More$/, "").trim();
    if (name && body) reviews.push({ name, date, rating: r, body: body.slice(0, 260) });
  });
  return JSON.stringify({ rating, count, distribution: dist, reviews, _source: source });
}
"""

TRIP_JS = r"""
() => {
  const txt = document.body.innerText || "";
  const reviewCountMatch = txt.match(/All reviews\s*\(([\d,]+)\)/i) ||
                           txt.match(/\(([\d,]+)\s*reviews?\)/i);
  const rankingMatch = txt.match(/#(\d+)\s*of\s*([\d,]+)/);
  const ratingMatch = txt.match(/^\s*(\d\.\d)\s*$/m);
  const dist = {};
  ["Excellent", "Very good", "Average", "Poor", "Terrible"].forEach((k) => {
    const re = new RegExp(k + "\\s*\\n?\\s*([\\d,]+)", "i");
    const mm = txt.match(re);
    if (mm) dist[k] = parseInt(mm[1].replace(/,/g, ""));
  });
  const startIdx = txt.indexOf("All reviews");
  const block = txt.substring(startIdx, startIdx + 5000);
  const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
  const reviews = [];
  let i = 0;
  while (i < lines.length && reviews.length < 4) {
    if (/^\d+\s+contribution/.test(lines[i]) && i >= 1) {
      const name = lines[i - 1];
      const j = i + 2;
      reviews.push({
        name,
        date: lines[j + 1] || "",
        title: lines[j] || "",
        body: (lines[j + 2] || "").slice(0, 260),
      });
      i = j + 3;
    } else { i++; }
  }
  return JSON.stringify({
    rating: ratingMatch ? ratingMatch[1] : null,
    count: reviewCountMatch ? reviewCountMatch[1] : null,
    ranking: rankingMatch ? "#" + rankingMatch[1] + " of " + rankingMatch[2] : null,
    distribution: dist, reviews,
  });
}
"""


# Extracts rating + count from Google Search's right-side knowledge panel.
# Used as a fallback when the Maps page serves a stripped "limited view".
SEARCH_KP_JS = r"""
() => {
  const txt = document.body.innerText || "";
  // Common knowledge-panel patterns:
  //   "4.7 ★★★★★ (95)"
  //   "4.7 (95) Google reviews"
  //   "4.7 stars · 95 Google reviews"
  let m = txt.match(/(\d\.\d)\s*[★*]+\s*\(([\d,]+)\)/);
  if (!m) m = txt.match(/(\d\.\d)\s*\(([\d,]+)\)\s*Google\s*reviews?/i);
  if (!m) m = txt.match(/(\d\.\d)\s*(?:stars?)?\s*[·,]?\s*([\d,]+)\s*Google\s*reviews?/i);
  if (!m) m = txt.match(/(\d\.\d)\s+\(([\d,]+)\)/);  // last-resort generic
  return JSON.stringify({
    rating: m ? m[1] : null,
    count: m ? m[2] : null,
  });
}
"""


def _scrape_one(page, target):
    try:
        page.goto(target["url"], timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning(f"goto failed for {target['key']}: {e}")
        return None
    # Wait for the data we care about to actually render. Google Maps
    # redirects /maps/search → /maps/place asynchronously and the review
    # count appears late; Tripadvisor lazy-loads the rating block. With
    # only a fixed wait, fast venues scrape OK and slow ones return None.
    page.wait_for_timeout(3000)
    # Two ways the venue data appears: visible "<rating> (<count>)" text, OR
    # the per-star aria-labels (e.g. "5 stars, 82 reviews"). Either is
    # enough to populate the dashboard. Wait for whichever shows up first.
    ready_re = (
        "() => /\\d\\.\\d\\s*\\([\\d,]+\\)/.test(document.body.innerText) "
        "|| document.querySelectorAll('[role=\"img\"][aria-label*=\"reviews\"]').length >= 3"
    )
    if target["type"] == "google":
        try:
            page.wait_for_function(ready_re, timeout=15000)
        except Exception:
            # Maybe we're on a search results page that didn't auto-redirect.
            try:
                first_result = page.query_selector('a.hfpxzc')
                if first_result:
                    logger.info(f"{target['key']}: clicking first search result to open venue panel")
                    first_result.click()
                    page.wait_for_timeout(3000)
                    page.wait_for_function(ready_re, timeout=12000)
                else:
                    logger.warning(f"{target['key']}: venue data didn't render; scraping what's there")
            except Exception as e:
                logger.warning(f"{target['key']}: click-to-place failed ({e}); scraping what's there")

        # Some venue panels (e.g. newer venues or short review lists) only
        # lazy-load the per-star distribution after a scroll. Trigger a scroll
        # within the side panel and wait briefly. Idempotent — no harm if the
        # data is already there.
        try:
            page.evaluate(
                "() => {"
                "  const candidates = ["
                "    document.querySelector('[role=\"main\"] [role=\"region\"]'),"
                "    document.querySelector('div[role=\"main\"] > div'),"
                "    document.querySelector('[role=\"feed\"]'),"
                "    ...document.querySelectorAll('div[tabindex=\"-1\"]'),"
                "  ];"
                "  const panel = [...candidates].find(el => el && el.scrollHeight > el.clientHeight + 50);"
                "  if (panel) panel.scrollBy(0, 1200);"
                "}"
            )
            page.wait_for_timeout(2500)
            # Optional second scroll for very long panels
            page.evaluate(
                "() => {"
                "  const candidates = ["
                "    document.querySelector('[role=\"main\"] [role=\"region\"]'),"
                "    document.querySelector('div[role=\"main\"] > div'),"
                "    document.querySelector('[role=\"feed\"]'),"
                "    ...document.querySelectorAll('div[tabindex=\"-1\"]'),"
                "  ];"
                "  const panel = [...candidates].find(el => el && el.scrollHeight > el.clientHeight + 50);"
                "  if (panel) panel.scrollBy(0, 1200);"
                "}"
            )
            page.wait_for_timeout(1500)
        except Exception as e:
            logger.warning(f"{target['key']}: scroll-to-load failed: {e}")

        page.wait_for_timeout(1000)
    else:
        # Tripadvisor: wait for "All reviews (N)" or "X.X of 5".
        try:
            page.wait_for_function(
                "() => /All reviews\\s*\\(\\d/.test(document.body.innerText) || /\\d\\.\\d\\s*of\\s*5/.test(document.body.innerText)",
                timeout=18000,
            )
        except Exception:
            logger.warning(f"{target['key']}: TA rating block never appeared (likely anti-bot block on VPS IP)")
        page.wait_for_timeout(2000)

    try:
        raw = page.evaluate(GOOGLE_JS if target["type"] == "google" else TRIP_JS)
    except Exception as e:
        logger.warning(f"evaluate failed for {target['key']}: {e}")
        return None
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError as e:
        logger.warning(f"bad JSON from {target['key']}: {e}")
        return None

    # If Maps gave us only the rating (limited-view headless detection),
    # fall back to Google Search's knowledge panel for the count.
    if (
        target["type"] == "google"
        and target.get("search_q")
        and data
        and not data.get("count")
    ):
        try:
            from urllib.parse import quote_plus
            search_url = f"https://www.google.com/search?q={quote_plus(target['search_q'])}"
            logger.info(f"{target['key']}: Maps gave limited view, trying Search knowledge panel")
            page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            # Reject consent walls quickly if encountered
            try:
                btn = page.query_selector('button:has-text("Reject all"), button:has-text("I agree")')
                if btn:
                    btn.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass
            search_raw = page.evaluate(SEARCH_KP_JS)
            search_data = json.loads(search_raw) if search_raw else None
            if search_data and search_data.get("count"):
                data["count"] = search_data["count"]
                if not data.get("rating"):
                    data["rating"] = search_data.get("rating")
                src = data.get("_source") or {}
                src["count"] = "search-kp"
                if not src.get("rating"):
                    src["rating"] = "search-kp"
                data["_source"] = src
                logger.info(f"{target['key']}: Search KP filled count={search_data['count']}")
            else:
                logger.warning(f"{target['key']}: Search KP also missing count")
        except Exception as e:
            logger.warning(f"{target['key']}: Search KP fallback failed: {e}")

    return data


def scrape_all_venues(headless: bool = True) -> dict:
    """Drive a single browser across all targets, return a renderer-ready dict."""
    raw: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        if _apply_stealth:
            try:
                _apply_stealth(page)
                logger.info("playwright-stealth patches applied")
            except Exception as e:
                logger.warning(f"stealth apply failed: {e}")

        places_enabled = places_api.is_enabled()
        if places_enabled:
            logger.info("Google Places API key detected — using API as primary for Google data")

        for target in VENUE_TARGETS:
            data = None

            # ─── Google Places API path (when key is set) ───────────────
            if (
                places_enabled
                and target["type"] == "google"
                and target.get("places_query")
            ):
                api_data = places_api.fetch_google_data(target["places_query"])
                if api_data and api_data.get("rating"):
                    data = api_data
                    logger.info(
                        f"{target['key']}: rating={data.get('rating')} "
                        f"count={data.get('count')} [via Places API]"
                    )
                    # Places API doesn't return per-star distribution AND its
                    # 5 reviews are sorted by Google relevance, not strictly
                    # recency. Run a best-effort headless scrape to grab BOTH
                    # the lifetime distribution AND the truly newest reviews
                    # (Google Maps' venue panel defaults to newest first).
                    try:
                        supplemental = _scrape_one(page, target)
                        if supplemental:
                            if supplemental.get("distribution"):
                                data["distribution"] = supplemental["distribution"]
                                logger.info(f"{target['key']}: lifetime distribution from headless aria-labels")
                            if supplemental.get("reviews"):
                                data["reviews"] = supplemental["reviews"]
                                logger.info(f"{target['key']}: {len(supplemental['reviews'])} newest reviews from headless")
                    except Exception as e:
                        logger.warning(f"{target['key']}: supplemental headless scrape failed: {e}")

            # ─── Headless scrape path (fallback or non-Google) ──────────
            if not data:
                data = _scrape_one(page, target)
            # Apply Tripadvisor fallbacks when live scrape returned nothing.
            if (not data or not data.get("rating")) and target["key"] in TRIP_FALLBACKS:
                logger.info(f"{target['key']}: using TA fallback values")
                data = TRIP_FALLBACKS[target["key"]]
            raw[target["key"]] = data or {}
            if data:
                src = data.get("_source") or {}
                src_str = f" [rating:{src.get('rating','—')} count:{src.get('count','—')}]" if src else ""
                logger.info(
                    f"{target['key']}: rating={data.get('rating')} "
                    f"count={data.get('count')}{src_str}"
                )
            else:
                logger.warning(f"{target['key']}: no data")
        context.close()
        browser.close()
    return _build_dashboard_data(raw)


def _build_dashboard_data(scrape: dict) -> dict:
    poolhouse_g = scrape.get("poolhouse") or {}
    poolhouse_t = scrape.get("poolhouse_trip") or {}
    philly_g = scrape.get("philly") or {}
    boston_g = scrape.get("boston") or {}
    dubai_g = scrape.get("dubai") or {}
    dubai_t = scrape.get("dubai_trip") or {}

    def gdist(d):
        if not d: return [0, 0, 0, 0, 0]
        x = d.get("distribution", {}) or {}
        return [int(x.get(str(s), 0)) for s in (5, 4, 3, 2, 1)]

    def merge(google_data, trip_data, ot_reviews=None, max_n=4):
        """Combine reviews from all sources, sort by publish date (newest first), take top N."""
        pool = []
        for r in (google_data or {}).get("reviews", []) or []:
            pool.append({
                "source": "g",
                "rating": r.get("rating", 5),
                "body": r.get("body", ""),
                "name": r.get("name", ""),
                "when": _short_when(r.get("date", "")),
                "_date_key": _parse_review_date(r),
                "url": "https://www.google.com/maps/place/_",
            })
        for r in (trip_data or {}).get("reviews", []) or []:
            pool.append({
                "source": "t", "rating": 5,
                "body": r.get("body", ""), "name": r.get("name", ""),
                "when": (r.get("date") or "").split("•")[0].strip() or "Tripadvisor",
                "_date_key": _parse_review_date(r),
                "url": "https://www.tripadvisor.com/",
            })
        for r in (ot_reviews or []):
            r2 = dict(r)
            r2.setdefault("_date_key", _parse_review_date(r))
            pool.append(r2)
        # Sort newest first; entries with no parseable date sink to the bottom.
        pool.sort(key=lambda x: x.get("_date_key") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        for r in pool:
            r.pop("_date_key", None)
        return pool[:max_n]

    # Build merged review lists once so analytics can use them too
    poolhouse_reviews = merge(poolhouse_g, poolhouse_t)
    philly_ot_reviews = [
        {"source": "o", "rating": 5, "body": "Great vibe, great service and hospitality, excellent food and drinks (cocktails and a long liquor list!).", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-philadelphia"},
        {"source": "o", "rating": 5, "body": "Best meatballs and drinks ever!", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-philadelphia"},
    ]
    philly_reviews = merge(philly_g, None, ot_reviews=philly_ot_reviews)
    boston_ot_reviews = [
        {"source": "o", "rating": 5, "body": "Fun setting, great service, grilled cheese, tomato soup, s'mores hot chocolate.", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-boston"},
    ]
    boston_reviews = merge(boston_g, None, ot_reviews=boston_ot_reviews)
    dubai_reviews = merge(dubai_g, dubai_t)

    return {
        "last_scrape": datetime.now().strftime("%b %-d, %Y · %-I:%M %p"),
        "venues": {
            "poolhouse": {
                "google": {"rating": poolhouse_g.get("rating"), "count": poolhouse_g.get("count")},
                "trip": {"rating": poolhouse_t.get("rating"), "count": poolhouse_t.get("count"), "rank": poolhouse_t.get("ranking")},
                "distribution": gdist(poolhouse_g),
                "reviews": poolhouse_reviews,
                "analytics": _venue_analytics(poolhouse_reviews),
                "insight": f"{poolhouse_g.get('count','?')} Google reviews · TA {poolhouse_t.get('rating','?')}★ {poolhouse_t.get('ranking','')}",
            },
            "philly": {
                "google": {"rating": philly_g.get("rating"), "count": philly_g.get("count")},
                "opentable": {"rating": "4.5", "count": "30"},
                "distribution": gdist(philly_g),
                "reviews": philly_reviews,
                "analytics": _venue_analytics(philly_reviews),
                "insight": "Steady — 90%+ Google reviews are 5★. Padel + pickleball + smash burger keywords dominate.",
            },
            "boston": {
                "google": {"rating": boston_g.get("rating"), "count": boston_g.get("count")},
                "opentable": {"rating": "5.0", "count": "2"},
                "distribution": gdist(boston_g),
                "reviews": boston_reviews,
                "analytics": _venue_analytics(boston_reviews),
                "insight": "Bimodal — current Google reviews are about the closed winter ice-rink pop-up, not the new outdoor product.",
            },
            "dubai": {
                "google": {"rating": dubai_g.get("rating"), "count": dubai_g.get("count")},
                "trip": {"rating": dubai_t.get("rating"), "count": dubai_t.get("count"), "rank": dubai_t.get("ranking")},
                "distribution": gdist(dubai_g),
                "reviews": dubai_reviews,
                "analytics": _venue_analytics(dubai_reviews),
                "insight": f"{dubai_t.get('ranking','?')} in Dubai · {dubai_g.get('count','?')} Google reviews",
            },
        },
    }


def _short_when(s: str) -> str:
    s = (s or "").strip().split("\n")[0]
    if "NEW" in s.upper():
        s = s.split("NEW")[0].strip()
    return s[:18]


def _venue_analytics(reviews_pool: list) -> dict:
    """
    Compute lightweight analytics for a venue from the merged reviews list.
    `reviews_pool` is the post-merge list of dicts each with `rating` (int 1-5).
    """
    sample = len(reviews_pool)
    if not sample:
        return {"velocity_per_week": None, "positive_pct": None, "recent_distribution": {}, "sample_size": 0}

    positive = sum(1 for r in reviews_pool if (r.get("rating") or 0) >= 4)
    positive_pct = round(100 * positive / sample) if sample else None

    recent_dist = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
    for r in reviews_pool:
        s = str(int(r.get("rating") or 0))
        if s in recent_dist:
            recent_dist[s] += 1

    return {
        "velocity_per_week": None,  # venues' merged-review pool is small (≤4); leave blank
        "positive_pct": positive_pct,
        "recent_distribution": recent_dist,
        "sample_size": sample,
    }
