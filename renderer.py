"""HTML renderer for the venue reception dashboard."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

VENUE_META = [
    {
        "key": "poolhouse",
        "name": "Poolhouse London",
        "addr": "100 Liverpool St · Cocktail bar · Opened Apr 8, 2026 · 3 wks live",
        "google_url": "https://www.google.com/maps/place/Poolhouse",
        "trip_url": "https://www.tripadvisor.com/Attraction_Review-g186338-d34271730-Reviews-Poolhouse-London_England.html",
        "ot_url": None,
        "insight_kind": "good",
    },
    {
        "key": "philly",
        "name": "Ballers Philadelphia",
        "addr": "1325 N Beach St, Fishtown · Sports club · Opened Sept 2025 · 7 mo live",
        "google_url": "https://www.google.com/maps/place/Ballers/@39.967446,-75.126293",
        "trip_url": None,
        "ot_url": "https://www.opentable.com/r/ballers-philadelphia",
        "insight_kind": "good",
    },
    {
        "key": "boston",
        "name": "Ballers Boston Seaport",
        "addr": "25 Pier 4 Blvd · Sports club · Outdoor opened Apr 15, 2026 · 2 wks live",
        "google_url": "https://www.google.com/maps/place/Ballers+Boston+Seaport",
        "trip_url": None,
        "ot_url": "https://www.opentable.com/r/ballers-boston",
        "insight_kind": "warn",
    },
    {
        "key": "dubai",
        "name": "Five Iron Golf Dubai",
        "addr": "Westin Mina Seyahi · Indoor golf · Opened Sept 2024 · 19 mo live",
        "google_url": "https://www.google.com/maps/place/Five+Iron+Golf",
        "trip_url": "https://www.tripadvisor.com/Attraction_Review-g295424-d33368076-Reviews-Five_Iron_Golf-Dubai_Emirate_of_Dubai.html",
        "ot_url": None,
        "insight_kind": "good",
    },
]


def _stars(rating, max_stars=5):
    if rating is None:
        rating = 5
    rating = max(0, min(max_stars, int(rating)))
    filled = "★" * rating
    muted = "★" * (max_stars - rating)
    return f'{filled}<span class="muted">{muted}</span>' if muted else filled


def _review_card(rev):
    src = rev.get("source", "g")
    label = {"g": "G", "t": "T", "o": "OT"}.get(src, src.upper())
    return f"""<div class="rev">
        <div class="top"><span class="stars">{_stars(rev.get('rating',5))}</span><span class="src-pill {src}">{label}</span></div>
        <div class="body">{escape(rev.get('body',''))}</div>
        <div class="who"><a href="{escape(rev.get('url','#'))}" target="_blank"><span class="name">{escape(rev.get('name',''))}</span></a><span class="when">{escape(rev.get('when',''))}</span></div>
      </div>"""


def _venue_block(meta, data):
    g = data.get("google") or {}
    t = data.get("trip") or {}
    o = data.get("opentable") or {}
    reviews = (data.get("reviews") or [])[:4]
    while len(reviews) < 4:
        reviews.append({"source": "g", "rating": 0, "body": "—", "name": "no review", "when": "", "url": meta["google_url"]})
    dist = data.get("distribution", [0, 0, 0, 0, 0])
    total = max(1, sum(dist))
    pct = [round(100 * d / total, 1) for d in dist]
    insight = escape(data.get("insight", ""))
    insight_kind = meta["insight_kind"]

    pills = []
    if g.get("rating"):
        pills.append(
            f'<span class="scoreP g"><span class="src">G</span>'
            f'<a href="{escape(meta["google_url"])}" target="_blank">'
            f'<span class="num">{escape(str(g["rating"]))}<small>/5</small></span> '
            f'({escape(str(g.get("count","")))})</a></span>'
        )
    if t.get("rating") and meta.get("trip_url"):
        rank = f' {escape(str(t.get("rank","")))}' if t.get("rank") else ""
        pills.append(
            f'<span class="scoreP t"><span class="src">T</span>'
            f'<a href="{escape(meta["trip_url"])}" target="_blank">'
            f'<span class="num">{escape(str(t["rating"]))}<small>/5</small></span> '
            f'({escape(str(t.get("count","")))}){rank}</a></span>'
        )
    if o.get("rating") and meta.get("ot_url"):
        pills.append(
            f'<span class="scoreP o"><span class="src">OT</span>'
            f'<a href="{escape(meta["ot_url"])}" target="_blank">'
            f'<span class="num">{escape(str(o["rating"]))}<small>/5</small></span> '
            f'({escape(str(o.get("count","")))})</a></span>'
        )

    segs = "".join(f'<div class="seg s{5-i}" style="width:{pct[i]}%"></div>' for i in range(5))
    cap = "  ".join(f"{5-i}★·{dist[i]:,}" for i in range(5))
    cards = "\n      ".join(_review_card(r) for r in reviews)

    return f"""<article class="venue">
    <div class="v-info">
      <div class="name">{escape(meta['name'])}</div>
      <div class="addr">{escape(meta['addr'])}</div>
      <div class="scores">{' '.join(pills)}</div>
      <div class="micro-dist" title="Google distribution: {' · '.join(str(d) for d in dist)}">{segs}</div>
      <div class="micro-cap">{cap}</div>
      <div class="insight {insight_kind}">{insight}</div>
    </div>
    <div class="reviews">
      {cards}
    </div>
  </article>"""


def render(data: dict) -> str:
    last_scrape = data.get("last_scrape") or datetime.now().strftime("%b %-d, %Y · %-I:%M %p")
    venues_html = "\n\n  ".join(
        _venue_block(meta, data["venues"].get(meta["key"], {})) for meta in VENUE_META
    )
    return _TEMPLATE.replace("{{LAST_SCRAPE}}", escape(last_scrape)).replace("{{VENUES}}", venues_html)


def write_dashboard(data: dict, html_path: Path, json_path: Path | None = None):
    html_path.parent.mkdir(parents=True, exist_ok=True)
    if json_path:
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    html_path.write_text(render(data), encoding="utf-8")
    return html_path


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta http-equiv="refresh" content="300" />
<title>Venue Reception · Live</title>
<style>
:root { color-scheme: light;
  --bg: #fafbfc; --card: #ffffff; --ink: #0a0e1a; --ink-soft: #4a5568; --ink-faint: #94a3b8;
  --line: #eceef2; --line-soft: #f3f4f7;
  --google: #4285F4; --trip: #00aa6c; --opentable: #da3743;
  --good: #16a34a; --bad: #dc2626; --warn: #d97706; }
* { box-sizing: border-box; }
html, body { margin: 0; }
body { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Display", "Segoe UI", sans-serif;
  background: var(--bg); color: var(--ink); font-size: 13px; line-height: 1.45; letter-spacing: -0.005em; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 14px 18px 24px; }
.header { display: flex; align-items: center; justify-content: space-between; padding-bottom: 10px; margin-bottom: 12px; border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand .title { font-size: 14px; font-weight: 600; letter-spacing: -0.01em; }
.brand .live { display: inline-flex; align-items: center; gap: 5px; font-size: 10.5px; font-weight: 600; color: var(--good); background: #ecfdf5; padding: 2px 8px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.04em; }
.brand .live::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--good); animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
.meta-right { font-size: 10.5px; color: var(--ink-faint); text-align: right; line-height: 1.5; }
.meta-right strong { color: var(--ink-soft); font-weight: 500; }
.venue { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; display: grid; grid-template-columns: minmax(220px, 280px) 1fr; gap: 16px; align-items: stretch; }
@media (max-width: 920px) { .venue { grid-template-columns: 1fr; } }
.v-info { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.v-info .name { font-size: 14.5px; font-weight: 600; letter-spacing: -0.01em; }
.v-info .addr { font-size: 11px; color: var(--ink-faint); }
.v-info .scores { display: flex; flex-wrap: wrap; gap: 5px 10px; margin-top: 6px; }
.scoreP { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--ink); }
.scoreP .src { font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; padding: 1px 5px; border-radius: 4px; background: #f1f5f9; color: var(--ink-soft); }
.scoreP.g .src { background: #e8f0fe; color: #1d4ed8; }
.scoreP.t .src { background: #d8f3e8; color: #047857; }
.scoreP.o .src { background: #fce8ea; color: #9b1c1c; }
.scoreP .num { font-weight: 700; font-size: 13px; }
.scoreP .num small { color: var(--ink-soft); font-weight: 500; font-size: 10.5px; }
.scoreP a { color: inherit; text-decoration: none; }
.scoreP a:hover { color: var(--google); }
.v-info .insight { margin-top: 6px; font-size: 11px; color: var(--ink-soft); padding: 5px 8px; border-radius: 6px; background: var(--line-soft); }
.v-info .insight.warn { background: #fef3c7; color: #92400e; }
.v-info .insight.good { background: #ecfdf5; color: #065f46; }
.micro-dist { display: flex; gap: 1px; height: 4px; margin-top: 2px; border-radius: 2px; overflow: hidden; }
.micro-dist .seg { flex: 0 0 auto; }
.micro-dist .s5 { background: #22c55e; } .micro-dist .s4 { background: #84cc16; }
.micro-dist .s3 { background: #eab308; } .micro-dist .s2 { background: #f97316; } .micro-dist .s1 { background: #ef4444; }
.micro-cap { font-size: 9.5px; color: var(--ink-faint); margin-top: 2px; }
.reviews { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; min-width: 0; }
@media (max-width: 720px) { .reviews { grid-template-columns: 1fr 1fr; } }
.rev { border: 1px solid var(--line-soft); border-radius: 8px; padding: 8px 10px; background: #fcfcfd; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rev .top { display: flex; align-items: center; justify-content: space-between; gap: 4px; }
.rev .stars { color: #f59e0b; font-size: 10px; letter-spacing: 0.5px; }
.rev .stars .muted { color: #cbd5e1; }
.rev .src-pill { font-size: 9px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; padding: 1px 5px; border-radius: 4px; background: #f1f5f9; color: var(--ink-faint); }
.rev .src-pill.g { background: #e8f0fe; color: #1d4ed8; }
.rev .src-pill.t { background: #d8f3e8; color: #047857; }
.rev .src-pill.o { background: #fce8ea; color: #9b1c1c; }
.rev .body { font-size: 11.5px; line-height: 1.42; color: var(--ink); display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical; overflow: hidden; flex: 1; }
.rev .who { font-size: 10px; color: var(--ink-soft); border-top: 1px dashed var(--line); padding-top: 4px; display: flex; justify-content: space-between; align-items: baseline; }
.rev .who .name { font-weight: 600; color: var(--ink); }
.rev .who .when { color: var(--ink-faint); }
.rev .who a { color: inherit; text-decoration: none; }
.foot { margin-top: 12px; font-size: 10.5px; color: var(--ink-faint); text-align: center; }
</style></head>
<body><div class="wrap">
  <header class="header">
    <div class="brand"><span class="title">Venue Reception</span><span class="live">Live</span></div>
    <div class="meta-right">
      <div><strong>Scraped:</strong> {{LAST_SCRAPE}}</div>
      <div><strong>Reloaded:</strong> <span id="now"></span> · refresh in 5min</div>
    </div>
  </header>

  {{VENUES}}

  <div class="foot">Standalone deployment · auto-reload 5min · scrape every 30min</div>
</div>
<script>document.getElementById('now').textContent = new Date().toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });</script>
</body></html>
"""
