"""HTML renderer for the venue + apps reception dashboard."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

OUT_DIR = Path(__file__).parent / "data"
OUT_PATH = OUT_DIR / "venue-reception.html"
DATA_PATH = OUT_DIR / "venue-reception.json"


VENUE_META = [
    {
        "key": "poolhouse",
        "name": "Poolhouse London",
        "addr": "100 Liverpool St · Cocktail bar · Opened Apr 8, 2026 · 3 wks live",
        "google_url": "https://www.google.com/maps/place/Poolhouse",
        "trip_url":   "https://www.tripadvisor.com/Attraction_Review-g186338-d34271730-Reviews-Poolhouse-London_England.html",
        "ot_url": None,
    },
    {
        "key": "philly",
        "name": "Ballers Philadelphia",
        "addr": "1325 N Beach St, Fishtown · Sports club · Opened Sept 2025 · 7 mo live",
        "google_url": "https://www.google.com/maps/place/Ballers/@39.967446,-75.126293",
        "trip_url": None,
        "ot_url": "https://www.opentable.com/r/ballers-philadelphia",
    },
    {
        "key": "boston",
        "name": "Ballers Boston Seaport",
        "addr": "25 Pier 4 Blvd · Sports club · Outdoor opened Apr 15, 2026 · 2 wks live",
        "google_url": "https://www.google.com/maps/place/Ballers+Boston+Seaport",
        "trip_url": None,
        "ot_url": "https://www.opentable.com/r/ballers-boston",
    },
    {
        "key": "dubai",
        "name": "Five Iron Golf Dubai",
        "addr": "Westin Mina Seyahi · Indoor golf · Opened Sept 2024 · 19 mo live",
        "google_url": "https://www.google.com/maps/place/Five+Iron+Golf",
        "trip_url":   "https://www.tripadvisor.com/Attraction_Review-g295424-d33368076-Reviews-Five_Iron_Golf-Dubai_Emirate_of_Dubai.html",
        "ot_url": None,
    },
]


# ─── small helpers ─────────────────────────────────────────────────────────
def fmt_count(n):
    if n is None: return "—"
    try: n = int(n)
    except (TypeError, ValueError): return str(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return f"{n:,}"


def _stars(rating, max_stars=5):
    if rating is None: rating = 5
    rating = max(0, min(max_stars, int(rating)))
    filled = "★" * rating
    muted = "★" * (max_stars - rating)
    return f'{filled}<span class="muted">{muted}</span>' if muted else filled


def _sparkline(series, kind="count", width=120, height=28):
    """
    Render a tiny inline SVG sparkline from a list of {ts, rating, count}.
    `kind` is "count" (cumulative growth) or "rating" (small fluctuations).
    """
    pts = [p.get(kind) for p in (series or [])]
    pts = [p for p in pts if p is not None]
    if len(pts) < 2:
        return f'<div class="spark empty" style="width:{width}px;height:{height}px;"></div>'

    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    n = len(pts)
    # Map each point into the viewBox
    coords = []
    for i, v in enumerate(pts):
        x = (i / (n - 1)) * width
        y = height - ((v - lo) / rng) * (height - 4) - 2
        coords.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(coords)
    last_x, last_y = coords[-1].split(",")

    # Fill area beneath the line
    area = f"M0,{height} L{poly.replace(' ', ' L')} L{width},{height} Z"

    color = "var(--good)" if (pts[-1] >= pts[0]) else "var(--bad)"
    fill = "rgba(34,197,94,0.10)" if (pts[-1] >= pts[0]) else "rgba(220,38,38,0.10)"

    return f'''<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" width="{width}" height="{height}">
        <path d="{area}" fill="{fill}" stroke="none"/>
        <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="{last_x}" cy="{last_y}" r="2" fill="{color}"/>
    </svg>'''


def _status_pill(trends, sparkline, count, current_rating, positive_pct=None):
    """
    Categorize entity momentum.
    Order of checks (most-informative first):
      • NEW       — fewer than 20 lifetime reviews
      • SLIPPING  — rating dropped ≥0.10 over any window  OR  recent positive% < 40
      • WATCH     — recent positive% 40–64
      • HOT       — count growing >2% / week
      • GROWING   — count growing but slower
      • STEADY    — no notable signal
    """
    # New: small total count
    if count is not None and count < 20:
        return ('NEW', 'new', 'building review base')

    # Trend-based signals
    week_delta = None
    for label, d in (trends or {}).items():
        if 'd' in label and d.get('count_delta') is not None:
            week_delta = d['count_delta']
            break
    rating_drift = None
    for label in ('30d', '7d', '24h'):
        d = (trends or {}).get(label)
        if d and d.get('rating_delta') not in (None, 0):
            rating_drift = d['rating_delta']
            break

    if rating_drift is not None and rating_drift <= -0.10:
        return ('SLIPPING', 'slipping', f'rating dropping {rating_drift:+.2f}')

    # Sentiment-based signals (fill the gap before history accumulates)
    if positive_pct is not None:
        if positive_pct < 40:
            return ('SLIPPING', 'slipping', f'recent reviews only {positive_pct}% positive')
        if positive_pct < 65:
            return ('WATCH', 'watch', f'recent reviews {positive_pct}% positive')

    # Velocity-based signals
    if week_delta is not None and count and week_delta > 0 and week_delta / count > 0.02:
        return ('HOT', 'hot', f'+{week_delta} reviews/wk')
    if week_delta is not None and week_delta > 0:
        return ('GROWING', 'growing', f'+{week_delta} reviews this week')
    return ('STEADY', 'steady', 'stable')


# ─── trend badges ──────────────────────────────────────────────────────────
_WINDOW_RE = __import__("re").compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _window_seconds(label: str) -> int:
    m = _WINDOW_RE.match(label)
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)] if m else 0


def _badge_label(label: str) -> str:
    return f"in {label}" if label in ("24h", "7d", "30d") else f"since {label}"


def _trends_row(trends: dict) -> str:
    if not trends:
        return '<div class="trends placeholder">collecting trend data…</div>'

    items = sorted(trends.items(), key=lambda kv: _window_seconds(kv[0]))
    badges = []

    def _count_badge(label, delta):
        sign = "+" if delta > 0 else ("" if delta == 0 else "−")
        cls = "up" if delta > 0 else ("flat" if delta == 0 else "down")
        return f'<span class="tb {cls}"><b>{sign}{abs(delta)}</b> reviews · {_badge_label(label)}</span>'

    def _rating_badge(label, delta):
        if delta is None or delta == 0: return None
        sign = "+" if delta > 0 else "−"
        cls = "up" if delta > 0 else "down"
        return f'<span class="tb {cls}"><b>{sign}{abs(delta):.2f}★</b> · {_badge_label(label)}</span>'

    nonzero_count_shown = False
    for label, d in items:
        if d.get("count_delta") not in (None, 0):
            badges.append(_count_badge(label, d["count_delta"]))
            nonzero_count_shown = True
        if len([b for b in badges if "reviews" in b]) >= 3:
            break
    if not nonzero_count_shown:
        for label, d in items:
            if "count_delta" in d:
                badges.append(_count_badge(label, d["count_delta"]))
                break

    rb_added = 0
    for label, d in items:
        b = _rating_badge(label, d.get("rating_delta"))
        if b:
            badges.append(b)
            rb_added += 1
            if rb_added >= 2: break

    if not badges:
        return '<div class="trends placeholder">no trend movement yet</div>'
    return f'<div class="trends">{"".join(badges)}</div>'


def _distribution_block(dist_dict: dict, total_label: str = "") -> str:
    """Proper proportional 5-bar distribution with counts."""
    counts = [int(dist_dict.get(str(s), 0)) for s in (5, 4, 3, 2, 1)]
    total = sum(counts) or 1
    rows = []
    for stars, c in zip([5, 4, 3, 2, 1], counts):
        pct = round(100 * c / total, 1)
        rows.append(
            f'<div class="dr"><span class="dl">{stars}★</span>'
            f'<span class="dt"><span class="df s{stars}" style="width:{pct}%"></span></span>'
            f'<span class="dn">{fmt_count(c)}</span></div>'
        )
    return '<div class="dist-block">' + "".join(rows) + (f'<div class="dist-total">{escape(total_label)}</div>' if total_label else '') + '</div>'


# ─── review cards ──────────────────────────────────────────────────────────
def _review_card(rev, default_url=None):
    src = rev.get("source", "g")
    label = {"g": "G", "t": "T", "o": "OT", "ios": "iOS", "android": "AND"}.get(src, src.upper())
    url = rev.get("url") or default_url or "#"
    when = rev.get("when") or (rev.get("publish_time", "")[:10])
    title_prefix = f"{escape(rev.get('title',''))} — " if rev.get('title') else ''
    return f"""<div class="rev">
        <div class="rev-top"><span class="stars">{_stars(rev.get('rating',5))}</span><span class="src-pill {src}">{label}</span></div>
        <div class="rev-body">{title_prefix}{escape(rev.get('body',''))}</div>
        <div class="rev-who"><a href="{escape(url)}" target="_blank"><span class="name">{escape(rev.get('name',''))}</span></a><span class="when">{escape(when)}</span></div>
      </div>"""


# ─── venue card ────────────────────────────────────────────────────────────
def _venue_block(meta, data):
    g = data.get("google") or {}
    t = data.get("trip") or {}
    o = data.get("opentable") or {}
    reviews = (data.get("reviews") or [])[:4]
    while len(reviews) < 4:
        reviews.append({"source": "g", "rating": 0, "body": "—", "name": "—", "when": "", "url": meta["google_url"]})
    dist = data.get("distribution", [0,0,0,0,0])
    if isinstance(dist, list):
        dist_dict = {str(s): n for s, n in zip([5,4,3,2,1], dist)}
    else:
        dist_dict = dist or {}
    sparkline_html = _sparkline(data.get("sparkline") or [], "count")

    primary_rating = g.get("rating")
    primary_count = g.get("count")
    status_label, status_cls, status_hint = _status_pill(
        data.get("trends") or {}, data.get("sparkline") or [],
        int(primary_count) if primary_count not in (None, "—") and str(primary_count).isdigit() else None,
        float(primary_rating) if primary_rating not in (None, "—") else None,
    )

    pills = []
    if g.get("rating"):
        pills.append(_score_pill("g", "G", g, meta["google_url"]))
    if t.get("rating") and meta.get("trip_url"):
        pills.append(_score_pill("t", "T", t, meta["trip_url"], extra=t.get("rank","")))
    if o.get("rating") and meta.get("ot_url"):
        pills.append(_score_pill("o", "OT", o, meta["ot_url"]))

    cards_html = "\n      ".join(_review_card(r, default_url=meta["google_url"]) for r in reviews)

    return f"""<article class="card">
    <header class="card-h">
      <div class="card-title">
        <h3>{escape(meta['name'])}</h3>
        <span class="status {status_cls}" title="{escape(status_hint)}">{status_label}</span>
      </div>
      <div class="card-sub">{escape(meta['addr'])}</div>
    </header>
    <div class="card-body">
      <div class="metrics-col">
        <div class="primary">
          <div class="primary-num">{escape(str(primary_rating)) if primary_rating else '—'}<small>/5</small></div>
          <div class="primary-sub">{fmt_count(primary_count)} Google reviews</div>
          <div class="spark-wrap">{sparkline_html}</div>
        </div>
        <div class="scores-row">{' '.join(pills)}</div>
        {_trends_row(data.get("trends") or {})}
      </div>
      <div class="dist-col">
        {_distribution_block(dist_dict, total_label=f"{fmt_count(sum(int(dist_dict.get(str(s),0)) for s in (5,4,3,2,1)))} ratings")}
      </div>
      <div class="reviews-col">
        <div class="reviews-h">Most recent reviews</div>
        <div class="reviews-grid">
          {cards_html}
        </div>
      </div>
    </div>
  </article>"""


def _score_pill(cls, label, block, url, extra=""):
    raw = block.get("rating")
    if isinstance(raw, (int, float)):
        rating_str = f"{raw:.1f}"
    elif raw is None:
        rating_str = "—"
    else:
        rating_str = str(raw)
    return (
        f'<a class="scoreP {cls}" href="{escape(url)}" target="_blank">'
        f'<span class="src">{label}</span>'
        f'<span class="num">{rating_str}<small>/5</small></span> '
        f'({fmt_count(block.get("count"))})'
        + (f' <span class="rank">{escape(str(extra))}</span>' if extra else '')
        + f'</a>'
    )


# ─── app card ──────────────────────────────────────────────────────────────
def _app_block(meta, data):
    ios = data.get("ios") or {}
    android = data.get("android") or {}
    combined = data.get("combined") or {}
    analytics = data.get("analytics") or {}
    reviews = (data.get("reviews") or [])[:4]
    while len(reviews) < 4:
        reviews.append({"source": "ios", "rating": 0, "body": "—", "name": "—", "when": "", "url": meta.get("ios_url","#")})

    dist = data.get("distribution") or {}
    sparkline_html = _sparkline(data.get("sparkline") or [], "count")

    primary_rating = combined.get("rating") or ios.get("rating") or android.get("rating")
    primary_count = combined.get("count") or ios.get("count") or android.get("count")
    status_label, status_cls, status_hint = _status_pill(
        data.get("trends") or {}, data.get("sparkline") or [],
        int(primary_count) if primary_count else None,
        float(primary_rating) if primary_rating else None,
        positive_pct=analytics.get("positive_pct"),
    )

    pills = []
    if ios.get("rating") is not None:
        pills.append(_score_pill("ios", "iOS", ios, meta.get("ios_url","#")))
    if android.get("rating") is not None and meta.get("android_url"):
        pills.append(_score_pill("android", "AND", android, meta["android_url"]))

    chips = []
    v = analytics.get("velocity_per_week")
    if v is not None:
        chips.append(f'<span class="chip">≈ <b>{v:.1f}</b> reviews/wk</span>')
    pos = analytics.get("positive_pct")
    if pos is not None:
        cls = "good" if pos >= 80 else ("warn" if pos >= 60 else "bad")
        chips.append(f'<span class="chip {cls}"><b>{pos}%</b> positive · last {analytics.get("sample_size",0)}</span>')
    gap = analytics.get("cross_store_gap")
    if gap is not None and abs(gap) >= 0.05:
        sign = "+" if gap > 0 else "−"
        chips.append(f'<span class="chip">iOS vs AND: <b>{sign}{abs(gap):.2f}★</b></span>')
    vb = analytics.get("version_breakdown")
    if vb and vb.get("count"):
        chips.append(f'<span class="chip">v{escape(vb["version"])}: <b>{vb["rating"]:.2f}★</b> · {vb["count"]} reviews</span>')

    ios_v = ios.get("version", "")
    and_v = android.get("version", "")
    version_str = " · ".join(filter(None, [
        f"iOS v{ios_v}" if ios_v else "",
        f"Android v{and_v}" if and_v else "",
    ]))
    sub = escape(meta.get('tagline','')) + (' · ' + escape(version_str) if version_str else '')

    cards_html = "\n      ".join(_review_card(r, default_url=meta.get("ios_url","#")) for r in reviews)

    if isinstance(primary_rating, (int, float)):
        rating_str = f"{primary_rating:.1f}" if primary_rating == round(primary_rating, 1) else f"{primary_rating:.2f}"
    else:
        rating_str = str(primary_rating) if primary_rating else "—"

    return f"""<article class="card">
    <header class="card-h">
      <div class="card-title">
        <h3>{escape(meta['name'])}</h3>
        <span class="status {status_cls}" title="{escape(status_hint)}">{status_label}</span>
      </div>
      <div class="card-sub">{sub}</div>
    </header>
    <div class="card-body">
      <div class="metrics-col">
        <div class="primary">
          <div class="primary-num">{rating_str}<small>/5</small></div>
          <div class="primary-sub">{fmt_count(primary_count)} total ratings</div>
          <div class="spark-wrap">{sparkline_html}</div>
        </div>
        <div class="scores-row">{' '.join(pills)}</div>
        {_trends_row(data.get("trends") or {})}
        <div class="chips">{''.join(chips)}</div>
      </div>
      <div class="dist-col">
        {_distribution_block(dist, total_label="rating distribution")}
      </div>
      <div class="reviews-col">
        <div class="reviews-h">Most recent reviews</div>
        <div class="reviews-grid">
          {cards_html}
        </div>
      </div>
    </div>
  </article>"""


# ─── hero KPI strip ────────────────────────────────────────────────────────
def _hero_strip(summary, label_singular):
    if not summary:
        return ""
    total = summary.get("total_count") or 0
    avg = summary.get("avg_rating")
    weekly = summary.get("weekly_growth") or 0
    top_key = summary.get("top_mover_key")
    top_delta = summary.get("top_mover_delta") or 0
    avg_str = f"{avg:.2f}" if isinstance(avg, (int, float)) else "—"
    weekly_sign = "+" if weekly >= 0 else "−"
    top_sign = "+" if top_delta >= 0 else "−"

    return f'''<div class="hero">
      <div class="kpi">
        <div class="kpi-label">total {label_singular} ratings</div>
        <div class="kpi-num">{fmt_count(total)}</div>
        <div class="kpi-sub">across {summary.get("entity_count",0)} {label_singular}{"s" if summary.get("entity_count",0)!=1 else ""}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">avg rating (weighted)</div>
        <div class="kpi-num">{avg_str}<small>/5</small></div>
        <div class="kpi-sub">across the portfolio</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">new ratings · 7d</div>
        <div class="kpi-num up">{weekly_sign}{abs(weekly):,}</div>
        <div class="kpi-sub">summed across all {label_singular}s</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">top mover · 24h</div>
        <div class="kpi-num">{escape(top_key.replace("_"," ").title()) if top_key else "—"}</div>
        <div class="kpi-sub">{top_sign}{abs(top_delta)} reviews</div>
      </div>
    </div>'''


# ─── render ────────────────────────────────────────────────────────────────
def render(data: dict) -> str:
    from app_store import APPS as APP_META
    last_scrape = data.get("last_scrape") or datetime.now().strftime("%b %-d, %Y · %-I:%M %p")
    summary = data.get("summary") or {}
    venues_html = "\n\n  ".join(
        _venue_block(meta, data["venues"].get(meta["key"], {})) for meta in VENUE_META
    )
    apps_html = "\n\n  ".join(
        _app_block(meta, (data.get("apps") or {}).get(meta["key"], {})) for meta in APP_META
    )
    return (_TEMPLATE
        .replace("{{LAST_SCRAPE}}", escape(last_scrape))
        .replace("{{HERO_VENUES}}", _hero_strip(summary.get("venues"), "venue"))
        .replace("{{HERO_APPS}}",   _hero_strip(summary.get("apps"),   "app"))
        .replace("{{VENUES}}", venues_html)
        .replace("{{APPS}}", apps_html))


def write_dashboard(data: dict, html_path: Path, json_path: Path | None = None):
    html_path.parent.mkdir(parents=True, exist_ok=True)
    if json_path:
        json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    html_path.write_text(render(data), encoding="utf-8")
    return html_path


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta http-equiv="refresh" content="300" />
<title>Reception · Live</title>
<style>
:root { color-scheme: light;
  --bg: #f7f7f9; --bg-grad: linear-gradient(180deg,#fafbfc 0%,#f4f5f8 100%);
  --card: #ffffff; --ink: #0a0e1a; --ink-soft: #475569; --ink-faint: #94a3b8;
  --line: #e7e9ee; --line-soft: #f0f2f5;
  --good: #16a34a; --bad: #dc2626; --warn: #d97706; --hot: #ea580c;
  --shadow: 0 1px 0 rgba(15,23,42,0.04), 0 1px 2px rgba(15,23,42,0.04);
  --shadow-h: 0 1px 0 rgba(15,23,42,0.04), 0 4px 12px rgba(15,23,42,0.06);
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text", system-ui, sans-serif;
  background: var(--bg-grad); color: var(--ink);
  font-size: 13px; line-height: 1.5; letter-spacing: -0.005em;
  font-feature-settings: "ss01", "tnum", "cv11";
}
.wrap { max-width: 1380px; margin: 0 auto; padding: 16px 22px 36px; }

/* ─── header ─── */
.header {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 12px; margin-bottom: 8px;
}
.brand { display: flex; align-items: baseline; gap: 12px; }
.brand .title { font-size: 18px; font-weight: 700; letter-spacing: -0.02em; }
.brand .live {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 10.5px; font-weight: 700; color: var(--good);
  background: #ecfdf5; padding: 3px 9px; border-radius: 999px;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.brand .live::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%;
  background: var(--good); animation: pulse 2s ease-in-out infinite;
  box-shadow: 0 0 0 0 rgba(34,197,94,0.7);
}
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.meta-right { font-size: 11px; color: var(--ink-faint); text-align: right; line-height: 1.5; }
.meta-right strong { color: var(--ink-soft); font-weight: 500; }

/* ─── tabs ─── */
.tabs {
  display: flex; gap: 2px; margin: 4px 0 18px;
  border-bottom: 1px solid var(--line);
}
.tabs .tab {
  background: none; border: none;
  padding: 9px 16px; font-size: 13px; font-weight: 600; font-family: inherit;
  color: var(--ink-faint); cursor: pointer;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  transition: color 0.15s, border-color 0.15s;
  display: inline-flex; align-items: center; gap: 7px;
}
.tabs .tab:hover { color: var(--ink-soft); }
.tabs .tab.active { color: var(--ink); border-bottom-color: var(--ink); }
.tabs .tab .ct {
  background: var(--line-soft); color: var(--ink-soft);
  padding: 1px 7px; border-radius: 999px; font-size: 10px;
  font-weight: 700;
}
.tabs .tab.active .ct { background: #1e293b; color: #fff; }
.panel.hidden { display: none; }

/* ─── hero KPI strip ─── */
.hero {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
  margin-bottom: 14px;
}
@media (max-width: 920px) { .hero { grid-template-columns: 1fr 1fr; } }
.kpi {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px 14px;
  box-shadow: var(--shadow);
}
.kpi-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-faint); font-weight: 700;
}
.kpi-num {
  font-size: 24px; font-weight: 700; letter-spacing: -0.025em;
  color: var(--ink); margin: 4px 0 2px; line-height: 1.1;
}
.kpi-num small { font-size: 13px; color: var(--ink-soft); font-weight: 500; }
.kpi-num.up   { color: var(--good); }
.kpi-num.down { color: var(--bad); }
.kpi-sub { font-size: 11px; color: var(--ink-faint); }

/* ─── card layout ─── */
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; margin-bottom: 12px;
  box-shadow: var(--shadow);
  transition: box-shadow 0.2s, transform 0.2s;
}
.card:hover { box-shadow: var(--shadow-h); }
.card-h {
  padding: 12px 16px 8px; border-bottom: 1px solid var(--line-soft);
}
.card-title { display: flex; align-items: center; gap: 10px; }
.card-title h3 {
  margin: 0; font-size: 15px; font-weight: 600;
  letter-spacing: -0.01em; color: var(--ink);
}
.status {
  font-size: 10px; font-weight: 700; padding: 2px 7px;
  border-radius: 4px; text-transform: uppercase; letter-spacing: 0.05em;
}
.status.hot      { background: #fff1eb; color: var(--hot); }
.status.growing  { background: #ecfdf5; color: var(--good); }
.status.steady   { background: #f1f5f9; color: var(--ink-soft); }
.status.slipping { background: #fef2f2; color: var(--bad); }
.status.watch    { background: #fef3c7; color: var(--warn); }
.status.new      { background: #fef3c7; color: #92400e; }
.card-sub { font-size: 11px; color: var(--ink-faint); margin-top: 2px; }

.card-body {
  display: grid;
  grid-template-columns: minmax(220px,260px) minmax(180px,220px) 1fr;
  gap: 16px; padding: 12px 16px 14px;
  align-items: start;
}
@media (max-width: 980px) { .card-body { grid-template-columns: 1fr 1fr; } .reviews-col { grid-column: 1 / -1; } }
@media (max-width: 640px) { .card-body { grid-template-columns: 1fr; } }

/* ── primary metric ── */
.primary { display: flex; flex-direction: column; gap: 1px; }
.primary-num {
  font-size: 36px; font-weight: 700; letter-spacing: -0.03em;
  color: var(--ink); line-height: 1;
}
.primary-num small { font-size: 16px; color: var(--ink-soft); font-weight: 500; }
.primary-sub { font-size: 11px; color: var(--ink-faint); margin-top: 2px; }
.spark-wrap { margin-top: 6px; height: 28px; }
.spark { display: block; }
.spark.empty { background: var(--line-soft); border-radius: 4px; }

/* ── score pills ── */
.scores-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.scoreP {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11.5px; padding: 3px 8px; border-radius: 6px;
  background: var(--line-soft); color: var(--ink); text-decoration: none;
  border: 1px solid var(--line);
  transition: background 0.15s, border-color 0.15s;
}
.scoreP:hover { background: #fff; border-color: #cbd5e1; }
.scoreP .src {
  font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 1px 5px; border-radius: 3px;
  background: #fff; color: var(--ink-soft); border: 1px solid var(--line);
}
.scoreP.g .src { background: #e8f0fe; color: #1d4ed8; border-color: #bfdbfe; }
.scoreP.t .src { background: #d8f3e8; color: #047857; border-color: #a7f3d0; }
.scoreP.o .src { background: #fce8ea; color: #9b1c1c; border-color: #fecaca; }
.scoreP.ios .src { background: #f1f5f9; color: #0f172a; border-color: #e2e8f0; }
.scoreP.android .src { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
.scoreP .num { font-weight: 700; font-size: 12.5px; }
.scoreP .num small { color: var(--ink-soft); font-weight: 500; font-size: 10.5px; }
.scoreP .rank { color: var(--ink-faint); font-size: 10.5px; }

/* ── distribution column ── */
.dist-col { display: flex; flex-direction: column; gap: 6px; }
.dist-block { display: flex; flex-direction: column; gap: 3px; }
.dr {
  display: grid; grid-template-columns: 18px 1fr 36px; gap: 6px;
  align-items: center; font-size: 10.5px; color: var(--ink-soft);
}
.dl { font-weight: 600; color: var(--ink); }
.dt { background: var(--line-soft); border-radius: 3px; height: 8px; overflow: hidden; }
.df { display: block; height: 100%; border-radius: 3px; transition: width 0.4s ease; }
.df.s5 { background: #22c55e; } .df.s4 { background: #84cc16; }
.df.s3 { background: #eab308; } .df.s2 { background: #f97316; } .df.s1 { background: #ef4444; }
.dn { text-align: right; font-variant-numeric: tabular-nums; }
.dist-total {
  margin-top: 4px; font-size: 10px; color: var(--ink-faint);
  text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
}

/* ── trends + chips ── */
.trends { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 9px; }
.tb {
  font-size: 10.5px; padding: 3px 8px; border-radius: 999px;
  background: var(--line-soft); color: var(--ink-soft);
  border: 1px solid var(--line); line-height: 1.3;
}
.tb b { font-weight: 700; }
.tb.up    { background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }
.tb.down  { background: #fef2f2; border-color: #fecaca; color: #991b1b; }
.tb.flat  { color: var(--ink-faint); }
.trends.placeholder { font-size: 10.5px; color: var(--ink-faint); font-style: italic; padding: 4px 0; }

.chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.chip {
  font-size: 10.5px; padding: 3px 8px; border-radius: 4px;
  background: #f8fafc; color: var(--ink-soft); border: 1px solid var(--line-soft);
  line-height: 1.3;
}
.chip b { color: var(--ink); font-weight: 700; }
.chip.good { background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }
.chip.warn { background: #fef3c7; border-color: #fde68a; color: #92400e; }
.chip.bad  { background: #fef2f2; border-color: #fecaca; color: #991b1b; }

/* ── reviews column ── */
.reviews-col { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.reviews-h {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-faint); font-weight: 700;
}
.reviews-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;
  min-width: 0;
}
@media (max-width: 1080px) { .reviews-grid { grid-template-columns: 1fr 1fr; } }
.rev {
  border: 1px solid var(--line-soft); border-radius: 7px;
  padding: 7px 9px; background: #fcfcfd;
  display: flex; flex-direction: column; gap: 4px;
  min-width: 0; transition: border-color 0.15s, background 0.15s;
}
.rev:hover { border-color: #cbd5e1; background: #fff; }
.rev-top {
  display: flex; align-items: center; justify-content: space-between; gap: 4px;
}
.stars { color: #f59e0b; font-size: 10.5px; letter-spacing: 0.6px; }
.stars .muted { color: #cbd5e1; }
.src-pill {
  font-size: 9px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 1px 5px; border-radius: 3px;
  background: var(--line-soft); color: var(--ink-faint);
}
.src-pill.g { background: #e8f0fe; color: #1d4ed8; }
.src-pill.t { background: #d8f3e8; color: #047857; }
.src-pill.o { background: #fce8ea; color: #9b1c1c; }
.src-pill.ios { background: #f1f5f9; color: #0f172a; }
.src-pill.android { background: #ecfdf5; color: #047857; }
.rev-body {
  font-size: 11.5px; line-height: 1.45; color: var(--ink);
  display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical;
  overflow: hidden; flex: 1;
}
.rev-who {
  font-size: 10px; color: var(--ink-soft);
  border-top: 1px dashed var(--line); padding-top: 4px;
  display: flex; justify-content: space-between; align-items: baseline;
}
.rev-who .name { font-weight: 600; color: var(--ink); }
.rev-who .when { color: var(--ink-faint); }
.rev-who a { color: inherit; text-decoration: none; }

/* ── footer ── */
.foot {
  margin-top: 16px; font-size: 10.5px; color: var(--ink-faint);
  text-align: center;
}
</style></head>
<body><div class="wrap">
  <header class="header">
    <div class="brand"><span class="title">Reception</span><span class="live">Live</span></div>
    <div class="meta-right">
      <div><strong>Scraped:</strong> {{LAST_SCRAPE}}</div>
      <div><strong>Reloaded:</strong> <span id="now"></span> · refresh in 5min</div>
    </div>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab" data-tab="venues" role="tab">Venues<span class="ct">4</span></button>
    <button class="tab" data-tab="apps"   role="tab">Apps<span class="ct">5</span></button>
  </nav>

  <section id="panel-venues" class="panel">
    {{HERO_VENUES}}
    {{VENUES}}
  </section>

  <section id="panel-apps" class="panel">
    {{HERO_APPS}}
    {{APPS}}
  </section>

  <div class="foot">Standalone deployment · auto-reload 5min · scrape every 30min</div>
</div>
<script>
document.getElementById('now').textContent = new Date().toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
function setTab(name) {
  if (!['venues','apps'].includes(name)) name = 'venues';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('hidden', p.id !== 'panel-' + name));
  history.replaceState(null, '', '#' + name);
}
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => setTab(t.dataset.tab)));
window.addEventListener('hashchange', () => setTab(location.hash.slice(1)));
setTab(location.hash.slice(1) || 'venues');
</script>
</body></html>
"""
