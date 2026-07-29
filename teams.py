"""
Sports teams data source for the dashboard's Teams tab.

Pulls live team info from ESPN's public (unofficial, free, no-auth) API:
  • teams/{abbr}                     — record + division standing summary
  • teams/{abbr}/schedule            — full season; we slice prev-3 / next-3
  • standings?level=3                — division table + model playoff %
  • seasons/{yr}/futures             — WS / league / division betting odds
  • news                             — recent stories (filtered to the team)

Config-driven via TEAMS so adding another team is just a dict entry.
Betting futures come from ESPN's odds feed (ESPN BET preferred, else first
book that prices the team). "Make playoffs" has no betting market in the feed,
so we surface ESPN's model-based playoff probability (a %, not a moneyline).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Times are shown in US Eastern — matches how MLB schedules are published.
_DISPLAY_TZ = ZoneInfo("America/New_York")
_HTTP_TIMEOUT = 15

# Which season's futures to read. Kept overridable so a rollover doesn't need
# a code change mid-crisis, but defaults to the current calendar year.
_SEASON = datetime.now(timezone.utc).year


TEAMS = [
    {
        "key": "texas_rangers",
        "name": "Texas Rangers",
        "league": "MLB",
        "sport_path": "baseball/mlb",   # ESPN URL segment
        "espn_abbr": "tex",
        "espn_id": "13",
        "division_name": "American League West",
        # ESPN futures market names for THIS team's league/division.
        "pennant_future": "MLB - American League - Winner",
        "pennant_label": "Win American League",
        "division_future": "MLB - American League West",
        "worldseries_future": "MLB  - World Series - Winner",  # note: ESPN has 2 spaces
    },
]


def _get(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": "venue-reception/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"teams: GET failed {url}: {e}")
        return None


def _fmt_game_datetime(iso_utc: str) -> tuple[str, str]:
    """(date, time) in US Eastern, e.g. ('Wed, Jul 29', '7:05 PM ET')."""
    if not iso_utc:
        return ("", "")
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(_DISPLAY_TZ)
        return (dt.strftime("%a, %b %-d"), dt.strftime("%-I:%M %p ET"))
    except Exception:
        return (iso_utc[:10], "")


def _parse_event(ev: dict, abbr: str) -> Optional[dict]:
    """Flatten one ESPN schedule event into our game shape."""
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    us = next((c for c in competitors if (c.get("team") or {}).get("abbreviation", "").upper() == abbr.upper()), None)
    them = next((c for c in competitors if c is not us), None)
    if not us or not them:
        return None
    state = ((comp.get("status") or {}).get("type") or {}).get("state")  # pre / in / post
    date_str, time_str = _fmt_game_datetime(ev.get("date", ""))
    home_away = us.get("homeAway")
    opp_name = (them.get("team") or {}).get("displayName") or (them.get("team") or {}).get("abbreviation") or "TBD"

    def _score(c):
        s = c.get("score")
        if isinstance(s, dict):
            return s.get("value")
        try:
            return float(s) if s not in (None, "") else None
        except (TypeError, ValueError):
            return None

    us_score, them_score = _score(us), _score(them)
    result = None
    if state == "post" and us_score is not None and them_score is not None:
        wl = "W" if us.get("winner") else ("L" if them.get("winner") else "—")
        result = f"{wl} {int(us_score)}–{int(them_score)}"

    return {
        "state": state,
        "date": date_str,
        "time": time_str,
        "opponent": opp_name,
        "home_away": "vs" if home_away == "home" else "@",
        "result": result,
    }


def _fetch_record_and_standing(sport_path: str, abbr: str) -> dict:
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{abbr}")
    team = (d or {}).get("team") or {}
    overall = ""
    for it in ((team.get("record") or {}).get("items") or []):
        if it.get("description") == "Overall Record" or it.get("type") == "total":
            overall = it.get("summary") or ""
            break
    return {
        "name": team.get("displayName"),
        "record": overall,
        "standing_summary": team.get("standingSummary"),
        "logo": (team.get("logos") or [{}])[0].get("href") if team.get("logos") else None,
    }


def _fetch_schedule(sport_path: str, abbr: str) -> dict:
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{abbr}/schedule")
    events = (d or {}).get("events") or []
    games = [g for g in (_parse_event(e, abbr) for e in events) if g]
    played = [g for g in games if g["state"] == "post"]
    upcoming = [g for g in games if g["state"] == "pre"]
    return {
        "previous": played[-3:][::-1],   # most-recent first
        "next": upcoming[:3],
    }


def _walk_find_team(node, abbr):
    """DFS a standings blob for the entry dict whose team matches abbr."""
    if isinstance(node, dict):
        t = node.get("team")
        if isinstance(t, dict) and t.get("abbreviation", "").upper() == abbr.upper() and "stats" in node:
            return node
        for v in node.values():
            r = _walk_find_team(v, abbr)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _walk_find_team(v, abbr)
            if r:
                return r
    return None


def _fetch_division_and_playoff(sport_path: str, division_name: str, abbr: str) -> dict:
    """AL West table (ordered) + ESPN model playoff % for our team."""
    league = sport_path.split("/")[-1]  # 'mlb'
    d = _get(f"https://site.api.espn.com/apis/v2/sports/baseball/{league}/standings?level=3&season={_SEASON}")
    if not d:
        return {"table": [], "playoff_pct": None}

    # Find the division group by name.
    group = None

    def _find_group(node):
        nonlocal group
        if group is not None:
            return
        if isinstance(node, dict):
            if node.get("name") == division_name and (node.get("standings") or node.get("entries")):
                group = node
                return
            for v in node.values():
                _find_group(v)
        elif isinstance(node, list):
            for v in node:
                _find_group(v)

    _find_group(d)

    def _stat(entry, name):
        for s in (entry.get("stats") or []):
            if s.get("name") == name:
                return s
        return {}

    table, playoff_pct = [], None
    entries = []
    if group:
        entries = (group.get("standings") or {}).get("entries") or group.get("entries") or []
    for e in entries:
        team = e.get("team") or {}
        row = {
            "name": team.get("displayName") or team.get("name"),
            "abbr": team.get("abbreviation"),
            "wins": _stat(e, "wins").get("displayValue"),
            "losses": _stat(e, "losses").get("displayValue"),
            "pct": _stat(e, "winPercent").get("displayValue"),
            "gb": _stat(e, "gamesBehind").get("displayValue"),
            "is_team": team.get("abbreviation", "").upper() == abbr.upper(),
        }
        table.append(row)
        if row["is_team"]:
            pp = _stat(e, "playoffPercent")
            playoff_pct = pp.get("displayValue") or (f"{pp['value']:.1f}%" if pp.get("value") is not None else None)
    # Order the table by wins desc so it reads like a real standings block.
    table.sort(key=lambda r: float(r["pct"] or 0), reverse=True)
    return {"table": table, "playoff_pct": playoff_pct}


def _extract_team_odds(future: dict, espn_id: str) -> Optional[str]:
    """Team's moneyline from one ESPN future — prefer ESPN BET, else first book."""
    providers = future.get("futures") or []
    ordered = sorted(providers, key=lambda p: 0 if (p.get("provider") or {}).get("name") == "ESPN BET" else 1)
    for prov in ordered:
        for book in (prov.get("books") or []):
            ref = (book.get("team") or {}).get("$ref", "")
            tid = ref.split("/teams/")[-1].split("?")[0] if "/teams/" in ref else None
            if tid == espn_id:
                return book.get("value")
    return None


def _fetch_futures(cfg: dict) -> dict:
    d = _get(f"https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/seasons/{_SEASON}/futures?lang=en&region=us")
    items = (d or {}).get("items") or []
    by_name = {i.get("name"): i for i in items}
    espn_id = cfg["espn_id"]

    def odds(name):
        fut = by_name.get(name)
        return _extract_team_odds(fut, espn_id) if fut else None

    return {
        "world_series": odds(cfg["worldseries_future"]),
        "pennant": odds(cfg["pennant_future"]),
        "division": odds(cfg["division_future"]),
    }


def _fetch_news(sport_path: str, team_name: str, espn_id: str, limit: int = 5) -> list[dict]:
    """Recent stories, preferring ones about this team, falling back to league news."""
    d = _get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/news?limit=50")
    articles = (d or {}).get("articles") or []
    short = team_name.split()[-1]  # 'Rangers'

    def _link(a):
        links = (a.get("links") or {}).get("web") or {}
        return links.get("href", "")

    def _is_team(a):
        blob = f"{a.get('headline','')} {a.get('description','')}".lower()
        if short.lower() in blob:
            return True
        for c in (a.get("categories") or []):
            if str(c.get("teamId")) == espn_id or (c.get("description") or "") == team_name:
                return True
        return False

    team_arts = [a for a in articles if _is_team(a)]
    chosen = (team_arts or articles)[:limit]
    out = []
    for a in chosen:
        pub = a.get("published") or ""
        out.append({
            "headline": a.get("headline") or "",
            "url": _link(a),
            "published": pub[:10],
            "about_team": a in team_arts,
        })
    return out


def _build_team(cfg: dict) -> dict:
    core = _fetch_record_and_standing(cfg["sport_path"], cfg["espn_abbr"])
    schedule = _fetch_schedule(cfg["sport_path"], cfg["espn_abbr"])
    div = _fetch_division_and_playoff(cfg["sport_path"], cfg["division_name"], cfg["espn_abbr"])
    futures = _fetch_futures(cfg)
    news = _fetch_news(cfg["sport_path"], cfg["name"], cfg["espn_id"])
    return {
        "name": core.get("name") or cfg["name"],
        "league": cfg["league"],
        "logo": core.get("logo"),
        "record": core.get("record"),
        "standing_summary": core.get("standing_summary"),
        "division_name": cfg["division_name"],
        "previous_games": schedule["previous"],
        "next_games": schedule["next"],
        "division_table": div["table"],
        "odds": {
            "playoff_pct": div["playoff_pct"],       # model probability, not a line
            "pennant": futures["pennant"],
            "pennant_label": cfg["pennant_label"],
            "world_series": futures["world_series"],
            "division": futures["division"],
        },
        "news": news,
    }


def scrape_all_teams() -> dict:
    """Top-level entry, mirrors scrape_all_apps(). Returns {'teams': {key: {...}}}."""
    out = {}
    for cfg in TEAMS:
        try:
            out[cfg["key"]] = _build_team(cfg)
            logger.info(f"team {cfg['key']}: record={out[cfg['key']].get('record')} "
                        f"playoff%={out[cfg['key']]['odds'].get('playoff_pct')}")
        except Exception:
            logger.exception(f"team {cfg['key']}: build failed")
    return {
        "last_scrape": datetime.now(_DISPLAY_TZ).strftime("%b %-d, %Y · %-I:%M %p"),
        "teams": out,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(scrape_all_teams(), indent=2, default=str))
