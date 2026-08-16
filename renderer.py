"""HTML renderer for the venue + apps reception dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
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


def _pace_block(pace: dict, noun: str = "reviews", width=160, height=30):
    """
    Review-pace panel: weekly rate + how many new reviews landed each day.

    Replaces the old cumulative sparkline, which was structurally flat — a
    lifetime total in the hundreds barely moves, so the line said nothing.
    The daily *arrival* rate is the signal underneath it.
    """
    buckets = (pace or {}).get("buckets") or []
    if not buckets:
        return (f'<div class="pace empty">review pace — needs 2+ days of history</div>')

    vals = [b.get("new") or 0 for b in buckets]
    hi = max(vals) or 1
    n = len(vals)
    slot = width / n
    bw = max(3.0, slot * 0.68)
    bars = []
    for i, (b, v) in enumerate(zip(buckets, vals)):
        x = slot * i + (slot - bw) / 2
        last = i == n - 1
        if v > 0:
            bh = max(2.5, (height - 2) * (v / hi))
            fill = "var(--good)" if last else "#94a3b8"
        else:
            bh, fill = 2.0, "var(--line)"
        bars.append(
            f'<rect x="{x:.1f}" y="{height - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{fill}">'
            f'<title>{escape(b.get("day", ""))} · {v:g} new</title></rect>'
        )

    per_week = (pace or {}).get("per_week")
    rate = f"{per_week:g}" if isinstance(per_week, (int, float)) else "—"
    shown = round(sum(vals))
    return f'''<div class="pace">
      <div class="pace-head"><b>{rate}</b> new {escape(noun)} / wk</div>
      <svg class="pace-bars" viewBox="0 0 {width} {height}" width="{width}" height="{height}">{"".join(bars)}</svg>
      <div class="pace-foot">{fmt_count(shown)} new · last {n}d</div>
    </div>'''


def _attendance_bars(series, avg_line=None, width=320, height=150):
    """Vertical bars of avg attendance/game by season; current year highlighted.

    A narrow viewBox (close to the tile's own width) keeps the uniform-scale
    small, so bars render tall and labels stay legible. The 10-yr average is a
    bare dashed reference line — its value is in the caption above, no label.
    """
    if not series:
        return '<div class="gauge-empty">no attendance data</div>'
    vals = [s["avg"] for s in series]
    hi = max(vals) * 1.05
    pad_t, pad_b, pad_x = 16, 18, 4
    iw, ih = width - pad_x * 2, height - pad_t - pad_b
    n = len(series)
    slot = iw / n
    bw = slot * 0.68
    parts = []
    if avg_line:
        ly = pad_t + ih * (1 - avg_line / hi)
        parts.append(f'<line x1="{pad_x}" y1="{ly:.1f}" x2="{pad_x+iw}" y2="{ly:.1f}" '
                     f'stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="3 3" opacity="0.6"/>')
    for i, s in enumerate(series):
        x = pad_x + slot * i + (slot - bw) / 2
        bh = ih * (s["avg"] / hi)
        y = pad_t + (ih - bh)
        cur = s.get("is_current")
        fill = "var(--good)" if cur else "#cbd5e1"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{fill}"/>')
        parts.append(f"<text x=\"{x+bw/2:.1f}\" y=\"{height-5}\" text-anchor=\"middle\" font-size=\"8.5\" "
                     f"fill=\"{'var(--good)' if cur else 'var(--ink-faint)'}\">'{str(s['year'])[2:]}</text>")
        if cur:
            parts.append(f'<text x="{x+bw/2:.1f}" y="{y-3:.1f}" text-anchor="middle" font-size="8.5" '
                         f'font-weight="700" fill="var(--good)">{s["avg"]:,}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'preserveAspectRatio="xMidYMid meet" class="bars">{"".join(parts)}</svg>')


def _trend_chart(series, key="v", width=260, height=84, zero_line=False):
    """Season-long line chart from [{d, v}]. zero_line splits above/below zero."""
    pts = [p.get(key) for p in (series or []) if p.get(key) is not None]
    if len(pts) < 2:
        return '<div class="gauge-empty">not enough data yet</div>'
    lo, hi = min(pts), max(pts)
    if zero_line:
        m = max(abs(lo), abs(hi), 1)
        lo, hi = -m, m
    rng = (hi - lo) or 1
    n = len(pts)
    pad = 4
    iw, ih = width - pad * 2, height - pad * 2

    def X(i):
        return pad + iw * i / (n - 1)

    def Y(v):
        return pad + ih * (1 - (v - lo) / rng)

    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(pts))
    up = pts[-1] >= pts[0]
    color = "var(--good)" if up else "var(--bad)"
    fill = "rgba(34,197,94,0.10)" if up else "rgba(220,38,38,0.10)"
    area = f"M{pad},{height - pad} L{poly.replace(' ', ' L')} L{width - pad},{height - pad} Z"
    baseline = ""
    if zero_line:
        zy = Y(0)
        baseline = (f'<line x1="{pad}" y1="{zy:.1f}" x2="{width - pad}" y2="{zy:.1f}" '
                    f'stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="3 3" opacity="0.6"/>')
    lx, ly = X(n - 1), Y(pts[-1])
    return f'''<svg class="trend" viewBox="0 0 {width} {height}" preserveAspectRatio="none" width="100%" height="{height}">
        <path d="{area}" fill="{fill}" stroke="none"/>
        {baseline}
        <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.5" fill="{color}"/>
    </svg>'''


def _status_pill(trends, count, current_rating, positive_pct=None):
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
    analytics = data.get("analytics") or {}
    reviews = (data.get("reviews") or [])[:4]
    while len(reviews) < 4:
        reviews.append({"source": "g", "rating": 0, "body": "—", "name": "—", "when": "", "url": meta["google_url"]})
    dist = data.get("distribution", [0,0,0,0,0])
    if isinstance(dist, list):
        dist_dict = {str(s): n for s, n in zip([5,4,3,2,1], dist)}
    else:
        dist_dict = dist or {}
    pace_html = _pace_block(data.get("pace") or {}, "reviews")

    primary_rating = g.get("rating")
    primary_count = g.get("count")
    status_label, status_cls, status_hint = _status_pill(
        data.get("trends") or {},
        int(primary_count) if primary_count not in (None, "—") and str(primary_count).isdigit() else None,
        float(primary_rating) if primary_rating not in (None, "—") else None,
        positive_pct=analytics.get("positive_pct"),
    )

    pills = []
    if g.get("rating"):
        pills.append(_score_pill("g", "G", g, meta["google_url"]))
    if t.get("rating") and meta.get("trip_url"):
        pills.append(_score_pill("t", "T", t, meta["trip_url"], extra=t.get("rank","")))
    if o.get("rating") and meta.get("ot_url"):
        pills.append(_score_pill("o", "OT", o, meta["ot_url"]))

    # Distribution must represent the LIFETIME breakdown (sum should match the
    # total review count). If it doesn't, hide the panel — recent-sample
    # distributions are misleading for venues with hundreds/thousands of reviews.
    dist_total = sum(int(dist_dict.get(str(s), 0)) for s in (5, 4, 3, 2, 1))
    primary_count_int = None
    try:
        primary_count_int = int(str(primary_count).replace(",", "")) if primary_count not in (None, "—") else None
    except (TypeError, ValueError):
        primary_count_int = None
    is_lifetime = (
        dist_total > 0 and primary_count_int is not None and
        # Allow tiny tolerance — sometimes lifetime distribution is a few off due to flagged reviews
        abs(dist_total - primary_count_int) / max(primary_count_int, 1) < 0.10
    )
    if is_lifetime:
        dist_label = f"{fmt_count(dist_total)} ratings"
    else:
        dist_total = 0  # force empty render
        dist_label = "lifetime distribution unavailable"

    # Analytics chips for venues — same vocab as apps
    chips = []
    pos = analytics.get("positive_pct")
    if pos is not None and analytics.get("sample_size", 0) > 0:
        cls = "good" if pos >= 80 else ("warn" if pos >= 60 else "bad")
        chips.append(f'<span class="chip {cls}"><b>{pos}%</b> positive · last {analytics["sample_size"]}</span>')
    if t.get("rank"):
        chips.append(f'<span class="chip">{escape(str(t["rank"]))} on Tripadvisor</span>')

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
          {pace_html}
        </div>
        <div class="scores-row">{' '.join(pills)}</div>
        {_trends_row(data.get("trends") or {})}
        <div class="chips">{''.join(chips)}</div>
      </div>
      <div class="dist-col">
        {_distribution_block(dist_dict, total_label=dist_label) if dist_total > 0 else _empty_dist_block(dist_label)}
      </div>
      <div class="reviews-col">
        <div class="reviews-h">Most recent reviews</div>
        <div class="reviews-grid">
          {cards_html}
        </div>
      </div>
    </div>
  </article>"""


def _empty_dist_block(label: str) -> str:
    return f'<div class="dist-block empty"><div class="empty-msg">{escape(label)}</div></div>'


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

    # Distribution panel, in preference order:
    #   1. Android lifetime histogram — the Play Store exposes the real
    #      per-star breakdown; shown when it reconciles with the rating count.
    #   2. recent-reviews sample — iOS publishes no lifetime histogram, so for
    #      iOS-only apps we render the star breakdown of the most recent ~N
    #      reviews, labelled honestly as a sample (never claimed "lifetime").
    android_hist = (android.get("distribution") or {})
    android_total = (android.get("count") or 0)
    dist = {}
    dist_label = "lifetime distribution unavailable"
    if android_hist:
        h_total = sum(int(android_hist.get(str(s), 0)) for s in (5, 4, 3, 2, 1))
        if h_total > 0 and abs(h_total - android_total) / max(android_total, 1) < 0.10:
            dist = android_hist
            dist_label = f"{fmt_count(h_total)} Android ratings"
    if not dist:
        recent = analytics.get("recent_distribution") or {}
        r_total = sum(int(recent.get(str(s), 0)) for s in (5, 4, 3, 2, 1))
        if r_total > 0:
            dist = recent
            dist_label = f"over last ~{fmt_count(r_total)} reviews"
    pace_html = _pace_block(data.get("pace") or {}, "ratings")

    primary_rating = combined.get("rating") or ios.get("rating") or android.get("rating")
    primary_count = combined.get("count") or ios.get("count") or android.get("count")
    status_label, status_cls, status_hint = _status_pill(
        data.get("trends") or {},
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
    rank = data.get("rank")
    if rank and rank.get("rank"):
        chips.append(
            f'<span class="chip">'
            f'<b>#{rank["rank"]}</b> · {escape(str(rank["genre"]))} '
            f'({escape(str(rank["chart"]))})</span>'
        )

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
          {pace_html}
        </div>
        <div class="scores-row">{' '.join(pills)}</div>
        {_trends_row(data.get("trends") or {})}
        <div class="chips">{''.join(chips)}</div>
      </div>
      <div class="dist-col">
        {_distribution_block(dist, total_label=dist_label) if dist else _empty_dist_block(dist_label)}
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
def _team_block(team):
    """Render one team's card: record, odds, schedule, standings, news."""
    name = team.get("name") or "—"
    record = team.get("record") or "—"
    standing = team.get("standing_summary") or ""
    sub = " · ".join(filter(None, [team.get("league"), standing]))
    odds = team.get("odds") or {}

    def _odds_box(label, value, sub_txt="", cls=""):
        return (f'<div class="odds-box {cls}"><div class="ol">{escape(label)}</div>'
                f'<div class="ov">{escape(value or "—")}</div>'
                f'<div class="os">{escape(sub_txt)}</div></div>')

    def _book(b):
        return f"via {b}" if b else "moneyline"

    odds_html = "".join([
        _odds_box("Make Playoffs", odds.get("playoff_pct"), "ESPN model prob.", "pct"),
        _odds_box("Win AL West", odds.get("division"), _book(odds.get("division_book"))),
        _odds_box(odds.get("pennant_label") or "Win League", odds.get("pennant"), _book(odds.get("pennant_book"))),
        _odds_box("Win World Series", odds.get("world_series"), _book(odds.get("world_series_book"))),
    ])

    def _next_game(g):
        opp_bits = " · ".join(filter(None, [
            g.get("opp_record"),
            " ".join(filter(None, [g.get("opp_rank"), g.get("opp_div")])),
        ]))
        wx = g.get("weather")
        wx_html = ""
        if wx:
            precip = wx.get("precip")
            wx_html = f'{wx.get("icon","")} {wx.get("temp","")}°F'
            if precip not in (None, "", 0):
                wx_html += f' · {precip}%'
        return (f'<div class="g-next">'
                f'<span class="gd">{escape(g.get("date",""))}</span>'
                f'<span class="gm">{escape(g.get("home_away",""))} {escape(g.get("opponent",""))}</span>'
                f'<span class="gopp">{escape(opp_bits)}</span>'
                f'<span class="gwx">{escape(wx_html)}</span>'
                f'<span class="gtv2">{escape(g.get("tv") or "")}</span>'
                f'<span class="gt">{escape(g.get("time",""))}</span></div>')

    def _prev_game(g):
        res = g.get("result") or ""
        rcls = "win" if res.startswith("W") else ("loss" if res.startswith("L") else "")
        opp_bits = " · ".join(filter(None, [
            g.get("opp_record"),
            " ".join(filter(None, [g.get("opp_rank"), g.get("opp_div")])),
        ]))
        hl = g.get("statline") or ""
        return (f'<div class="g-prev">'
                f'<span class="gd">{escape(g.get("date",""))}</span>'
                f'<span class="gm">{escape(g.get("home_away",""))} {escape(g.get("opponent",""))}</span>'
                f'<span class="gopp">{escape(opp_bits)}</span>'
                f'<span class="gr {rcls}">{escape(res)}</span>'
                f'<span class="ghl" title="{escape(hl)}">{escape(hl)}</span></div>')

    next_html = "".join(_next_game(g) for g in (team.get("next_games") or [])) or '<div class="g-next"><span class="gm">No upcoming games</span></div>'
    prev_html = "".join(_prev_game(g) for g in (team.get("previous_games") or [])) or '<div class="g-prev"><span class="gm">No recent games</span></div>'

    rows = ""
    for r in (team.get("division_table") or []):
        me = " class=\"me\"" if r.get("is_team") else ""
        rows += (f'<tr{me}><td>{escape(r.get("name",""))}</td>'
                 f'<td>{escape(str(r.get("wins","")))}</td><td>{escape(str(r.get("losses","")))}</td>'
                 f'<td>{escape(str(r.get("pct","")))}</td><td>{escape(str(r.get("gb","")))}</td></tr>')
    stand_html = (f'<table class="stand"><thead><tr><th>{escape(team.get("division_name",""))}</th>'
                  f'<th>W</th><th>L</th><th>PCT</th><th>GB</th></tr></thead><tbody>{rows}</tbody></table>')

    news = list(team.get("news") or [])
    news_items = ""
    for n in news:
        url = n.get("url") or "#"
        news_items += (f'<a href="{escape(url)}" target="_blank" rel="noopener">'
                       f'<span class="nd">{escape(n.get("published",""))}</span>{escape(n.get("headline",""))}</a>')
    news_items += '<div class="empty-row">—</div>' * max(0, 4 - len(news))  # keep 4 rows
    news_html = f'<div class="team-news"><div class="team-h">Recent news</div>{news_items}</div>'

    # ── Non-team events at the home venue ──
    venue_name = team.get("venue_name") or "the venue"
    events = list(team.get("venue_events") or [])
    ve_items = ""
    for e in events:
        url = e.get("url") or "#"
        when = e.get("date", "") + (f' · {e["time"]}' if e.get("time") else "")
        ve_items += (f'<a class="ve" href="{escape(url)}" target="_blank" rel="noopener">'
                     f'<span class="ve-date">{escape(when)}</span>'
                     f'<span class="ve-name">{escape(e.get("name",""))}</span></a>')
    ve_items += '<div class="empty-row">—</div>' * max(0, 4 - len(events))  # keep 4 rows
    venue_events_html = (
        f'<div class="venue-events"><div class="team-h">Also at {escape(venue_name)} · non-Rangers</div>{ve_items}</div>'
    )

    # ── Live score banner (only while a game is in progress) ──
    live = team.get("live") or {}
    live_html = ""
    if live:
        outs = live.get("outs")
        outs_txt = f" · {outs} out" if outs is not None else ""
        bases = "".join("◆" if live.get(b) else "◇" for b in ("on_first", "on_second", "on_third"))
        live_html = (
            f'<div class="live-banner">'
            f'<span class="live-dot">● LIVE</span>'
            f'<span class="live-score">{escape(str(live.get("us_score","")))}–{escape(str(live.get("them_score","")))}</span>'
            f'<span class="live-meta">{escape(live.get("home_away",""))} {escape(live.get("opponent",""))} · '
            f'{escape(live.get("detail",""))}{escape(outs_txt)} <span class="bases">{bases}</span></span>'
            f'</div>'
        )

    # ── Season trends: games-ahead trajectory + attendance-by-year bars ──
    charts = team.get("charts") or {}
    ahead_series = charts.get("games_ahead") or []
    att_years = charts.get("attendance_by_year") or []
    att_current = charts.get("attendance_avg")
    prior = [r["avg"] for r in att_years if not r.get("is_current")]
    ten_yr_avg = round(sum(prior) / len(prior)) if prior else None
    def _fmt_when(iso):
        try:
            return datetime.fromisoformat(iso).strftime("%b %-d")
        except Exception:
            return iso
    last5 = charts.get("attendance_last5") or []
    last5_rows = "".join(
        f'<span class="ald">{escape(_fmt_when(g.get("d","")))}</span>'
        f'<span class="alv">{g["v"]:,}</span>'
        for g in last5 if g.get("v")
    )
    att_cell = ""
    if att_years:
        vs_txt = ""
        if att_current and ten_yr_avg:
            d = att_current - ten_yr_avg
            vs_txt = f'{att_current:,} avg · {"+" if d >= 0 else "−"}{abs(d):,} vs 10-yr'
        att_cell = f"""<div class="trend-cell wide">
            <div class="tc-title">Attendance / game · vs last 10 yrs</div>
            <div class="tc-sub tc-lead">{escape(vs_txt)}</div>
            {_attendance_bars(att_years, avg_line=ten_yr_avg, height=64)}
            <div class="att-last5">
              <div class="al-h">Last 5 home games</div>
              <div class="al-grid">{last5_rows}</div>
            </div>
          </div>"""
    ahead_last = ahead_series[-1]["v"] if ahead_series else None
    if ahead_last is None:
        ahead_txt, ahead_cls = "—", ""
    elif ahead_last > 0:
        ahead_txt, ahead_cls = f"+{ahead_last:g} ahead", "good"
    elif ahead_last < 0:
        ahead_txt, ahead_cls = f"{ahead_last:g} behind", "bad"
    else:
        ahead_txt, ahead_cls = "tied for lead", ""

    # ── Division-standings extras: performance stats not shown elsewhere ──
    ds = team.get("division_stats") or {}

    def _kpi(label, val, cls=""):
        if not val:
            return ""
        return (f'<div class="skpi"><span class="skl">{escape(label)}</span>'
                f'<span class="skv {cls}">{escape(str(val))}</span></div>')

    rd = ds.get("run_diff") or ""
    rd_cls = "good" if rd.startswith("+") else ("bad" if rd.startswith("-") else "")
    streak = ds.get("streak") or ""
    streak_cls = "good" if streak.startswith("W") else ("bad" if streak.startswith("L") else "")
    rpg = f'{ds["rpg_for"]} / {ds["rpg_against"]}' if ds.get("rpg_for") and ds.get("rpg_against") else None
    kpis = "".join([
        _kpi("Run diff", rd, rd_cls),
        _kpi("Last 10", ds.get("last10")),
        _kpi("Streak", streak, streak_cls),
        _kpi("Home", ds.get("home")),
        _kpi("Road", ds.get("road")),
        _kpi("Runs/G (for/vs)", rpg),
    ])
    std_kpis_html = f'<div class="std-kpis">{kpis}</div>' if kpis else ""

    trends_html = f"""<div class="team-trends">
        <div class="team-h">Season trends</div>
        <div class="trend-grid">
          <div class="trend-cell">
            <div class="tc-title">Games ahead / behind</div>
            <div class="tc-val {ahead_cls}">{escape(ahead_txt)}</div>
            {_trend_chart(ahead_series, zero_line=True)}
            <div class="tc-sub">in AL West · dashed = tied for lead</div>
          </div>
          {att_cell}
          <div class="trend-cell stand-cell">
            <div class="tc-title">Division standings</div>
            {stand_html}
            {std_kpis_html}
          </div>
        </div>
      </div>"""

    return f"""<article class="card">
    <header class="card-h">
      <div class="card-title">
        <h3>{escape(name)}</h3>
        <span class="status good" title="Overall record">{escape(record)}</span>
      </div>
      <div class="card-sub">{escape(sub)}</div>
    </header>
    <div class="card-body" style="display:block">
      {live_html}
      <div class="team-odds">{odds_html}</div>
      {trends_html}
      <div class="team-schedule">
        <div class="next-half">
          <div class="team-h">Next 3 games</div>{next_html}
        </div>
        <div class="team-h" style="margin-top:10px">Previous 3 games</div>{prev_html}
      </div>
      <div class="team-bottom">
        {venue_events_html}
        {news_html}
      </div>
    </div>
  </article>"""


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


# ─── feed tab ────────────────────────────────────────────────────────────
def _rel_to_dt(s: str, now: datetime):
    """Best-effort timestamp from an ISO date or a Google-style relative string."""
    s = (s or "").strip().lower()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:10])
    except Exception:
        pass
    if "today" in s or "hour" in s or "minute" in s or "just now" in s:
        return now
    if "yesterday" in s:
        return now - timedelta(days=1)
    head = s.split()[0] if s.split() else ""
    n = int(head) if head.isdigit() else 1
    for unit, days in (("year", 365), ("month", 30), ("week", 7), ("day", 1)):
        if unit in s:
            return now - timedelta(days=n * days)
    return None


def _feed_event_html(e: dict) -> str:
    if e["kind"] == "review":
        body = (
            f'<span class="fe-stars">{_stars(e["rating"])}</span> '
            f'<span class="fe-quote">"{escape(e["snippet"])}"</span> '
            f'<span class="fe-author">— {escape(e["author"])}</span>'
        )
    else:
        body = escape(e["text"])
    return (
        '<div class="feed-event">'
        f'<div class="fe-icon {e["icon_cls"]}">{escape(e["icon"])}</div>'
        '<div class="fe-main">'
        f'<div class="fe-entity">{escape(e["entity"])}</div>'
        f'<div class="fe-text">{body}</div>'
        '</div>'
        f'<div class="fe-when">{escape(e.get("when", ""))}</div>'
        '</div>'
    )


def _feed_block(data: dict) -> str:
    """Reverse-chronological feed of recent reviews + data updates across all entities."""
    from app_store import APPS as APP_META
    now = datetime.now()
    venue_names = {m["key"]: m["name"] for m in VENUE_META}
    app_names = {m["key"]: m["name"] for m in APP_META}

    entities = []
    for k, v in (data.get("venues") or {}).items():
        entities.append((venue_names.get(k, k.replace("_", " ").title()), v))
    for k, a in (data.get("apps") or {}).items():
        entities.append((app_names.get(k, k.replace("_", " ").title()), a))

    events = []

    # Review events — one per real review across every venue and app.
    for name, ent in entities:
        for rev in (ent.get("reviews") or []):
            body = (rev.get("body") or "").strip()
            if not body or body == "—":
                continue
            when = rev.get("when") or (rev.get("publish_time") or "")[:10]
            ts = _rel_to_dt(rev.get("publish_time") or rev.get("when") or "", now)
            rating = rev.get("rating") or 0
            events.append({
                "ts": ts or datetime(1970, 1, 1),
                "dated": ts is not None,
                "kind": "review",
                "icon": "★",
                "icon_cls": "good" if rating >= 4 else ("bad" if 0 < rating <= 2 else "mid"),
                "entity": name,
                "rating": rating,
                "snippet": body[:150],
                "author": rev.get("name") or "Anonymous",
                "when": when,
            })

    # Data-update events — freshest trend window per entity that moved.
    for name, ent in entities:
        trends = ent.get("trends") or {}
        for win in ("24h", "7d", "30d"):
            d = trends.get(win)
            if not d:
                continue
            cd, rd = d.get("count_delta"), d.get("rating_delta")
            if not cd and not rd:
                continue
            bits, icon, icon_cls = [], "+", "good"
            if cd:
                bits.append(f"{'+' if cd > 0 else '−'}{abs(cd)} review{'' if abs(cd) == 1 else 's'}")
                icon = "+" if cd > 0 else "−"
                icon_cls = "good" if cd > 0 else "bad"
            if rd:
                bits.append(f"rating {'+' if rd > 0 else '−'}{abs(rd):.2f}★")
                if not cd:
                    icon, icon_cls = ("▲", "good") if rd > 0 else ("▼", "bad")
            events.append({
                "ts": now, "dated": True, "kind": "update",
                "icon": icon, "icon_cls": icon_cls, "entity": name,
                "text": " · ".join(bits), "when": f"in {win}",
            })
            break

    if not events:
        return ('<div class="feed"><div class="feed-empty">No activity yet — the feed '
                'fills in as reviews and trend data accumulate.</div></div>')

    events.sort(key=lambda e: e["ts"], reverse=True)

    today = now.date()
    yesterday = today - timedelta(days=1)
    out, cur = [], object()
    for e in events:
        if e["dated"]:
            d = e["ts"].date()
            label = ("Today" if d == today
                     else "Yesterday" if d == yesterday
                     else e["ts"].strftime("%B %-d, %Y"))
        else:
            d, label = None, "Earlier"
        if d != cur:
            cur = d
            out.append(f'<div class="feed-day-header">{escape(label)}</div>')
        out.append(_feed_event_html(e))
    return '<div class="feed">' + "".join(out) + '</div>'


# ─── render ────────────────────────────────────────────────────────────────
def render(data: dict) -> str:
    from app_store import APPS as APP_META
    last_scrape = data.get("last_scrape") or datetime.now().strftime("%b %-d, %Y · %-I:%M %p")
    summary = data.get("summary") or {}
    venues_html = "\n\n  ".join(
        _venue_block(meta, data["venues"].get(meta["key"], {})) for meta in VENUE_META
    )
    # Apps tab is always ordered ascending by review count (least → most).
    apps_data = data.get("apps") or {}

    def _app_review_count(meta):
        a = apps_data.get(meta["key"], {})
        return (
            (a.get("combined") or {}).get("count")
            or (a.get("ios") or {}).get("count")
            or (a.get("android") or {}).get("count")
            or 0
        )

    apps_html = "\n\n  ".join(
        _app_block(meta, apps_data.get(meta["key"], {}))
        for meta in sorted(APP_META, key=_app_review_count)
    )
    # Teams tab — ordered by the config list in teams.py.
    from teams import TEAMS as TEAM_META
    teams_data = data.get("teams") or {}
    teams_html = "\n\n  ".join(
        _team_block(teams_data[m["key"]])
        for m in TEAM_META if teams_data.get(m["key"])
    ) or '<div class="empty-msg">No team data yet — populates on the next scrape cycle.</div>'

    return (_TEMPLATE
        .replace("{{LAST_SCRAPE}}", escape(last_scrape))
        .replace("{{HERO_VENUES}}", _hero_strip(summary.get("venues"), "venue"))
        .replace("{{HERO_APPS}}",   _hero_strip(summary.get("apps"),   "app"))
        .replace("{{VENUES_COUNT}}", str(len(VENUE_META)))
        .replace("{{APPS_COUNT}}", str(len(APP_META)))
        .replace("{{TEAMS_COUNT}}", str(len(teams_data)))
        .replace("{{VENUES}}", venues_html)
        .replace("{{APPS}}", apps_html)
        .replace("{{TEAMS}}", teams_html)
        .replace("{{FEED}}", _feed_block(data)))


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
<title>Sharp Alpha Venue &amp; App Review Dashboard</title>
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

/* ─── Poolhouse live shot counter (embedded widget) ─── */
.shot-counter { margin-bottom: 16px; }
.shot-counter .team-h { margin-bottom: 6px; }
/* Tall enough for the widget's own title + 150px odometer wheels so the
   graphic is never clipped; no padding around it that could crop the frame. */
.sc-frame {
  width: 100%; height: 250px; border: 1px solid var(--line);
  border-radius: 12px; display: block; background: #202020;
  box-shadow: var(--shadow);
}

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
.pace { margin-top: 8px; }
.pace-head { font-size: 10.5px; color: var(--ink-faint); line-height: 1.2; }
.pace-head b { font-size: 14px; font-weight: 700; color: var(--ink); }
.pace-bars { display: block; margin-top: 3px; }
.pace-bars rect { transition: opacity 0.15s; }
.pace-bars:hover rect { opacity: 0.75; }
.pace-bars rect:hover { opacity: 1; }
.pace-foot { font-size: 9.5px; color: var(--ink-faint); margin-top: 2px; }
.pace.empty {
  margin-top: 8px; font-size: 10.5px; color: var(--ink-faint);
  font-style: italic; padding: 6px 0;
}

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
.dist-block.empty { padding: 8px 10px; background: var(--line-soft); border-radius: 6px; }
.dist-block.empty .empty-msg { font-size: 10.5px; color: var(--ink-faint); font-style: italic; text-align: center; }

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

/* ── teams tab ── */
.team-odds { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.odds-box {
  flex: 1 1 120px; min-width: 110px; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
}
.odds-box .ol { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint); font-weight: 700; }
.odds-box .ov { font-size: 18px; font-weight: 800; color: var(--ink); margin-top: 2px; }
.odds-box .os { font-size: 9.5px; color: var(--ink-faint); }
.odds-box.pct .ov { color: var(--good); }
.team-h { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-faint); font-weight: 700; margin-bottom: 4px; }
/* rich one-row games */
.team-schedule { margin-bottom: 14px; }
.g-next, .g-prev { display: grid; align-items: baseline; column-gap: 12px; padding: 3px 0; border-bottom: 1px solid var(--line-soft); font-size: 11.5px; line-height: 1.3; }
/* Identical first three columns (date · opponent · record) in BOTH tables so the
   opponent names line up and the records line up across Next and Previous.
   justify-content:start keeps Next packed to the left ~half (no track stretching). */
/* All Next columns are fixed width so weather, TV and time line up across every
   row (each row is its own grid, so auto tracks would drift row-to-row). */
.g-next { grid-template-columns: 82px 145px 130px 100px 168px auto; justify-content: start; }
.g-prev { grid-template-columns: 82px 145px 130px 56px minmax(0,1fr); }
.g-next:last-child, .g-prev:last-child { border-bottom: 0; }
.g-next .gd, .g-prev .gd { color: var(--ink-faint); white-space: nowrap; }
.gm { color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gopp { color: var(--ink-soft); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gwx { color: var(--ink-soft); font-size: 11px; white-space: nowrap; }
.gtv2 { color: var(--ink-faint); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.g-next .gt { color: var(--ink-soft); font-size: 11px; white-space: nowrap; text-align: left; }
.g-prev .gr { font-weight: 700; text-align: left; }
.g-prev .gr.win { color: var(--good); } .g-prev .gr.loss { color: var(--bad); }
.ghl { color: var(--ink-soft); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
@media (max-width: 720px) {
  .g-next { grid-template-columns: 68px 1fr auto auto; }
  .g-next .gopp, .g-next .gtv2 { display: none; }
  .g-prev { grid-template-columns: 68px 1fr 52px; }
  .g-prev .gopp, .g-prev .ghl { display: none; }
}
.empty-row { padding: 6px 0; border-bottom: 1px solid var(--line-soft); color: var(--line); font-size: 12px; }
.empty-row:last-child { border-bottom: 0; }
/* bottom row: venue events + news side by side */
.team-bottom { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px 24px; align-items: start; }
/* live score banner */
.live-banner {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
  padding: 8px 12px; margin-bottom: 14px;
}
.live-dot { font-size: 10px; font-weight: 800; letter-spacing: 0.04em; color: var(--bad); text-transform: uppercase;
  animation: livepulse 1.6s ease-in-out infinite; }
@keyframes livepulse { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }
.live-score { font-size: 20px; font-weight: 800; color: var(--ink); }
.live-meta { font-size: 12px; color: var(--ink-soft); }
.live-meta .bases { letter-spacing: 1px; color: var(--warn); }
/* season trends */
.team-trends { margin-bottom: 14px; }
.trend-grid { display: grid; grid-template-columns: 1fr 0.95fr 1.35fr; gap: 12px; align-items: stretch; }
@media (max-width: 860px) { .trend-grid { grid-template-columns: 1fr; } }
.trend-cell { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: var(--card); text-align: center; display: flex; flex-direction: column; }
.trend-cell.wide, .trend-cell.stand-cell { text-align: left; }
.trend-cell.stand-cell table.stand { margin-top: 5px; }
.tc-title { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint); font-weight: 700; }
.tc-val { font-size: 17px; font-weight: 800; color: var(--ink); margin: 3px 0; }
.tc-val.good { color: var(--good); } .tc-val.bad { color: var(--bad); }
.tc-sub { font-size: 9.5px; color: var(--ink-faint); margin-top: 3px; }
/* the games-ahead line chart grows to fill its cell so there's no gap below it */
.trend-cell svg.trend { display: block; flex: 1 1 auto; min-height: 68px; height: auto; width: 100%; }
svg.bars { display: block; height: auto; margin-top: 6px; }
.gauge-empty { font-size: 12px; color: var(--ink-faint); padding: 30px 0; text-align: center; }
table.stand { width: 100%; border-collapse: collapse; font-size: 12px; }
table.stand th { text-align: right; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-faint); padding: 3px 6px; border-bottom: 1px solid var(--line); }
table.stand th:first-child { text-align: left; }
table.stand td { text-align: right; padding: 4px 6px; border-bottom: 1px solid var(--line-soft); color: var(--ink-soft); }
table.stand td:first-child { text-align: left; color: var(--ink); }
table.stand tr.me td { background: #eef6ff; font-weight: 700; color: var(--ink); }
/* attendance: compact lead + right-aligned last-5 list (tabular figures line up) */
.tc-lead { font-size: 12px; color: var(--ink); font-weight: 600; margin: 2px 0 4px; }
.att-last5 { margin-top: 10px; }
.al-h { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint); font-weight: 700; margin-bottom: 5px; }
.al-grid { display: grid; grid-template-columns: auto 1fr; column-gap: 8px; font-size: 12px; }
.al-grid .ald, .al-grid .alv { padding: 3px 0; border-bottom: 1px solid var(--line-soft); }
.al-grid > :nth-last-child(-n+2) { border-bottom: 0; }
.al-grid .ald { color: var(--ink-soft); }
.al-grid .alv { text-align: right; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
/* division-standings performance KPIs, filling what was empty space */
.std-kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 10px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-soft); }
.skpi { display: flex; flex-direction: column; }
.skl { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-faint); font-weight: 700; }
.skv { font-size: 13px; font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }
.skv.good { color: var(--good); } .skv.bad { color: var(--bad); }
.team-news { margin-top: 0; }
.team-news a { display: block; padding: 6px 0; border-bottom: 1px solid var(--line-soft); font-size: 12.5px; color: var(--ink); text-decoration: none; }
.team-news a:hover { color: #1d4ed8; }
.team-news .nd { color: var(--ink-faint); font-size: 10.5px; margin-right: 6px; }
/* non-team venue events */
.venue-events { margin-top: 0; }
.venue-events .ve { display: flex; align-items: baseline; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line-soft); text-decoration: none; }
.venue-events .ve:hover .ve-name { color: #1d4ed8; }
.venue-events .ve-date { flex: 0 0 118px; font-size: 11px; color: var(--ink-faint); }
.venue-events .ve-name { font-size: 12.5px; color: var(--ink); font-weight: 600; }

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

/* ─── feed tab ─── */
.feed { max-width: 920px; }
.feed-empty {
  color: var(--ink-faint); font-size: 13px; text-align: center; padding: 32px 16px;
}
.feed-day-header {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-faint); font-weight: 700;
  margin: 20px 0 4px; padding-bottom: 5px; border-bottom: 1px solid var(--line);
}
.feed-day-header:first-child { margin-top: 0; }
.feed-event {
  display: flex; gap: 11px; align-items: flex-start;
  padding: 9px 4px; border-bottom: 1px dashed var(--line-soft);
}
.feed-event:last-child { border-bottom: none; }
.fe-icon {
  flex: none; width: 22px; height: 22px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; margin-top: 1px;
}
.fe-icon.good { background: #dcfce7; color: #166534; }
.fe-icon.bad  { background: #fee2e2; color: #991b1b; }
.fe-icon.mid  { background: #fef3c7; color: #92400e; }
.fe-main { flex: 1; min-width: 0; }
.fe-entity {
  font-size: 11px; font-weight: 700; color: var(--ink);
  text-transform: uppercase; letter-spacing: 0.03em;
}
.fe-text { font-size: 12.5px; color: var(--ink-soft); margin-top: 1px; line-height: 1.45; }
.fe-stars { color: #f59e0b; font-size: 10.5px; letter-spacing: 0.6px; }
.fe-stars .muted { color: #cbd5e1; }
.fe-quote { color: var(--ink); }
.fe-author { color: var(--ink-faint); }
.fe-when {
  flex: none; font-size: 10px; color: var(--ink-faint);
  white-space: nowrap; margin-top: 3px;
}

/* ── footer ── */
.foot {
  margin-top: 16px; font-size: 10.5px; color: var(--ink-faint);
  text-align: center;
}
</style></head>
<body><div class="wrap">
  <header class="header">
    <div class="brand"><span class="title">Sharp Alpha Venue &amp; App Review Dashboard</span><span class="live">Live</span></div>
    <div class="meta-right">
      <div><strong>Scraped:</strong> {{LAST_SCRAPE}}</div>
      <div><strong>Reloaded:</strong> <span id="now"></span> · refresh in 5min</div>
    </div>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab" data-tab="venues" role="tab">Venues<span class="ct">{{VENUES_COUNT}}</span></button>
    <button class="tab" data-tab="apps"   role="tab">Apps<span class="ct">{{APPS_COUNT}}</span></button>
    <button class="tab" data-tab="teams"  role="tab">Teams<span class="ct">{{TEAMS_COUNT}}</span></button>
    <button class="tab" data-tab="feed"   role="tab">Feed</button>
  </nav>

  <section id="panel-venues" class="panel">
    <div class="shot-counter">
      <div class="team-h">Poolhouse · Live Shot Counter</div>
      <iframe class="sc-frame" src="https://stats.ls100.london.uk.poolhouse.support/counter/"
              title="Poolhouse Live Shot Counter" scrolling="no" referrerpolicy="no-referrer"></iframe>
    </div>
    {{HERO_VENUES}}
    {{VENUES}}
  </section>

  <section id="panel-apps" class="panel">
    {{HERO_APPS}}
    {{APPS}}
  </section>

  <section id="panel-teams" class="panel">
    {{TEAMS}}
  </section>

  <section id="panel-feed" class="panel">
    {{FEED}}
  </section>

  <div class="foot">Standalone deployment · auto-reload 5min · scrape every 30min</div>
</div>
<script>
document.getElementById('now').textContent = new Date().toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
function setTab(name) {
  if (!['venues','apps','teams','feed'].includes(name)) name = 'venues';
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
