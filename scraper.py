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

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)


# ─── target venues ───────────────────────────────────────────────────────
# Direct /maps/place/ URLs (with coords from our verified manual scrapes)
# avoid the flaky search→place redirect headless Chromium does inconsistently.
VENUE_TARGETS = [
    {"key": "poolhouse",      "type": "google",
     "url": "https://www.google.com/maps/place/Poolhouse/@51.5177849,-0.0831704,17z"},
    {"key": "poolhouse_trip", "type": "tripadvisor",
     "url": "https://www.tripadvisor.com/Attraction_Review-g186338-d34271730-Reviews-Poolhouse-London_England.html"},
    {"key": "philly",         "type": "google",
     "url": "https://www.google.com/maps/place/Ballers/@39.967446,-75.126293,17z"},
    {"key": "boston",         "type": "google",
     "url": "https://www.google.com/maps/place/Ballers+Boston+Seaport/@42.3495067,-71.0456173,17z"},
    {"key": "dubai",          "type": "google",
     "url": "https://www.google.com/maps/place/Five+Iron+Golf/@25.0930341,55.1487567,17z"},
    {"key": "dubai_trip",     "type": "tripadvisor",
     "url": "https://www.tripadvisor.com/Attraction_Review-g295424-d33368076-Reviews-Five_Iron_Golf-Dubai_Emirate_of_Dubai.html"},
]


# ─── fallback values for Tripadvisor pages ─────────────────────────────
# Tripadvisor blocks data-center IPs aggressively. When the live scrape
# fails, the dashboard falls back to these values (verified manually
# May 1, 2026 via a residential browser). Update whenever you have a
# successful manual scrape.
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
  const m = txt.match(/(\d\.\d)\s*\(([\d,]+)\)/);
  const stars = [];
  document.querySelectorAll('[role="img"][aria-label*="reviews"]').forEach(
    (el) => stars.push(el.getAttribute("aria-label") || "")
  );
  const dist = {};
  stars.forEach((s) => {
    const mm = s.match(/(\d)\s*stars?,\s*([\d,]+)\s*reviews?/);
    if (mm) dist[mm[1]] = parseInt(mm[2].replace(/,/g, ""));
  });
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
    const rating = rm ? parseInt(rm[1]) : null;
    const body = ((node.querySelector(".MyEned, .wiI7pd")?.innerText) || "")
      .replace(/…\s*More$/, "").trim();
    if (name && body) reviews.push({ name, date, rating, body: body.slice(0, 260) });
  });
  return JSON.stringify({
    rating: m ? m[1] : null, count: m ? m[2] : null,
    distribution: dist, reviews,
  });
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
    if target["type"] == "google":
        try:
            page.wait_for_function(
                "() => /\\d\\.\\d\\s*\\([\\d,]+\\)/.test(document.body.innerText)",
                timeout=18000,
            )
        except Exception:
            logger.warning(f"{target['key']}: rating+count pattern never appeared, scraping what's there")
        page.wait_for_timeout(2000)
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
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as e:
        logger.warning(f"bad JSON from {target['key']}: {e}")
        return None


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
        for target in VENUE_TARGETS:
            data = _scrape_one(page, target)
            # Apply Tripadvisor fallbacks when live scrape returned nothing.
            if (not data or not data.get("rating")) and target["key"] in TRIP_FALLBACKS:
                logger.info(f"{target['key']}: using TA fallback values")
                data = TRIP_FALLBACKS[target["key"]]
            raw[target["key"]] = data or {}
            if data:
                logger.info(
                    f"{target['key']}: rating={data.get('rating')} "
                    f"count={data.get('count')}"
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

    def merge(google_data, trip_data, max_n=4):
        out = []
        for r in (google_data or {}).get("reviews", [])[:max_n]:
            out.append({
                "source": "g",
                "rating": r.get("rating", 5),
                "body": r.get("body", ""),
                "name": r.get("name", ""),
                "when": _short_when(r.get("date", "")),
                "url": "https://www.google.com/maps/place/_",
            })
        for r in (trip_data or {}).get("reviews", [])[:max_n]:
            out.append({
                "source": "t", "rating": 5,
                "body": r.get("body", ""), "name": r.get("name", ""),
                "when": (r.get("date") or "").split("•")[0].strip() or "Tripadvisor",
                "url": "https://www.tripadvisor.com/",
            })
        return out[:max_n]

    return {
        "last_scrape": datetime.now().strftime("%b %-d, %Y · %-I:%M %p"),
        "venues": {
            "poolhouse": {
                "google": {"rating": poolhouse_g.get("rating"), "count": poolhouse_g.get("count")},
                "trip": {"rating": poolhouse_t.get("rating"), "count": poolhouse_t.get("count"), "rank": poolhouse_t.get("ranking")},
                "distribution": gdist(poolhouse_g),
                "reviews": merge(poolhouse_g, poolhouse_t),
                "insight": f"{poolhouse_g.get('count','?')} Google reviews · TA {poolhouse_t.get('rating','?')}★ {poolhouse_t.get('ranking','')}",
            },
            "philly": {
                "google": {"rating": philly_g.get("rating"), "count": philly_g.get("count")},
                "opentable": {"rating": "4.5", "count": "30"},
                "distribution": gdist(philly_g),
                "reviews": merge(philly_g, None) + [
                    {"source": "o", "rating": 5, "body": "Great vibe, great service and hospitality, excellent food and drinks (cocktails and a long liquor list!).", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-philadelphia"},
                    {"source": "o", "rating": 5, "body": "Best meatballs and drinks ever!", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-philadelphia"},
                ],
                "insight": "Steady — 90%+ Google reviews are 5★. Padel + pickleball + smash burger keywords dominate.",
            },
            "boston": {
                "google": {"rating": boston_g.get("rating"), "count": boston_g.get("count")},
                "opentable": {"rating": "5.0", "count": "2"},
                "distribution": gdist(boston_g),
                "reviews": merge(boston_g, None) + [
                    {"source": "o", "rating": 5, "body": "Fun setting, great service, grilled cheese, tomato soup, s'mores hot chocolate.", "name": "OpenTable diner", "when": "recent", "url": "https://www.opentable.com/r/ballers-boston"},
                ],
                "insight": "Bimodal — current Google reviews are about the closed winter ice-rink pop-up, not the new outdoor product.",
            },
            "dubai": {
                "google": {"rating": dubai_g.get("rating"), "count": dubai_g.get("count")},
                "trip": {"rating": dubai_t.get("rating"), "count": dubai_t.get("count"), "rank": dubai_t.get("ranking")},
                "distribution": gdist(dubai_g),
                "reviews": merge(dubai_g, dubai_t),
                "insight": f"{dubai_t.get('ranking','?')} in Dubai · {dubai_g.get('count','?')} Google reviews",
            },
        },
    }


def _short_when(s: str) -> str:
    s = (s or "").strip().split("\n")[0]
    if "NEW" in s.upper():
        s = s.split("NEW")[0].strip()
    return s[:18]
