"""
Review-trend metrics, computed by comparing the current scrape to a rolling
history persisted at data/history.jsonl.

Each scrape cycle:
  1. After the scrape produces the dashboard data dict, call `enrich_with_trends(data)`.
     It reads recent history, computes deltas per venue, attaches them under
     each venue's "trends" key.
  2. Call `append_history(data)` to log the current snapshot.

History format (one JSON object per line):
  {"ts": "2026-05-01T20:00:00+00:00",
   "venues": {"poolhouse": {"rating": 4.8, "count": 97}, ...}}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).parent / "data" / "history.jsonl"
WINDOWS = [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))]


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _read_history(max_age=timedelta(days=45)) -> list[dict]:
    """Return parsed history entries newer than max_age, oldest first."""
    if not HISTORY_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - max_age
    out = []
    try:
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts >= cutoff:
                    e["_ts"] = ts
                    out.append(e)
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    except Exception as e:
        logger.warning(f"history read failed: {e}")
    out.sort(key=lambda e: e["_ts"])
    return out


def _find_snapshot_at_or_before(history: list[dict], target_time: datetime) -> Optional[dict]:
    """Closest snapshot at or before target_time (within ±10% of window)."""
    best = None
    for e in history:
        if e["_ts"] <= target_time:
            best = e
        else:
            break
    return best


def _section_block(entry: dict, section: str) -> dict:
    """Pull the per-entity {rating, count} block out of a history entry, by section."""
    return entry.get(section) or {}


# Backwards-compat alias used by older callers
def _venues_block(entry: dict) -> dict:
    return _section_block(entry, "venues")


def _delta(prev: dict, cur_count, cur_rating) -> dict:
    prev_count = _to_int(prev.get("count"))
    prev_rating = _to_float(prev.get("rating"))
    entry = {}
    if cur_count is not None and prev_count is not None:
        entry["count_delta"] = cur_count - prev_count
    if cur_rating is not None and prev_rating is not None:
        entry["rating_delta"] = round(cur_rating - prev_rating, 2)
    return entry


def _format_elapsed(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 60:    return "just now"
    if secs < 3600:  return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _compute_entity_trends(
    section: str, key: str, current_block: dict, history: list[dict]
) -> dict:
    """
    Compute count + rating deltas for a single tracked entity (venue or app).
    Tries standard windows (24h/7d/30d) first; falls back to "since X" using
    the earliest available snapshot when the standard windows are empty.
    """
    cur_count = _to_int(current_block.get("count"))
    cur_rating = _to_float(current_block.get("rating"))
    if (cur_count is None and cur_rating is None) or not history:
        return {}

    now = datetime.now(timezone.utc)
    earliest = history[0]
    out = {}

    for label, delta in WINDOWS:
        target = now - delta
        if earliest["_ts"] > target:
            continue
        snap = _find_snapshot_at_or_before(history, target)
        if not snap:
            continue
        entry = _delta(_section_block(snap, section).get(key) or {}, cur_count, cur_rating)
        if entry:
            out[label] = entry

    if not out:
        elapsed = now - earliest["_ts"]
        if elapsed.total_seconds() >= 60:
            entry = _delta(
                _section_block(earliest, section).get(key) or {}, cur_count, cur_rating
            )
            if entry:
                out[_format_elapsed(elapsed)] = entry

    return out


def _sparkline_series(history: list[dict], section: str, key: str, max_points: int = 30) -> list[dict]:
    """Return the most recent N {ts, rating, count} points for an entity's sparkline."""
    series = []
    for entry in history:
        e = (entry.get(section) or {}).get(key) or {}
        if e.get("count") is None and e.get("rating") is None:
            continue
        series.append({
            "ts": entry["_ts"].isoformat(),
            "rating": _to_float(e.get("rating")),
            "count": _to_int(e.get("count")),
        })
    return series[-max_points:]


def enrich_with_trends(data: dict) -> dict:
    """
    Mutate `data` in place: add a "trends" sub-dict + "sparkline" series
    to each venue and app, and a "summary" block per section.
    """
    history = _read_history()
    venues = data.get("venues") or {}
    apps = data.get("apps") or {}

    for key, v in venues.items():
        google_block = v.get("google") or {}
        v["trends"] = _compute_entity_trends("venues", key, google_block, history) if history else {}
        v["sparkline"] = _sparkline_series(history, "venues", key) if history else []

    for key, a in apps.items():
        combined = a.get("combined") or {}
        a["trends"] = _compute_entity_trends("apps", key, combined, history) if history else {}
        a["sparkline"] = _sparkline_series(history, "apps", key) if history else []

    data["summary"] = {
        "venues": _section_summary(venues, history, "venues", "google"),
        "apps":   _section_summary(apps, history, "apps", "combined"),
    }
    return data


def _section_summary(entities: dict, history: list[dict], section: str, score_key: str) -> dict:
    """Aggregate summary across all entities in a section."""
    if not entities:
        return {}

    counts = []
    ratings = []
    for v in entities.values():
        block = v.get(score_key) or v.get("google") or {}
        c = _to_int(block.get("count"))
        r = _to_float(block.get("rating"))
        if c is not None: counts.append(c)
        if r is not None: ratings.append(r)

    total_count = sum(counts) if counts else 0
    # Weighted average rating: weight each rating by its count.
    avg_rating = None
    if ratings and counts and len(ratings) == len(counts):
        total_w = sum(counts)
        if total_w > 0:
            avg_rating = round(sum(r * c for r, c in zip(ratings, counts)) / total_w, 2)
    elif ratings:
        avg_rating = round(sum(ratings) / len(ratings), 2)

    # Top mover: largest count delta vs ~24h ago (or earliest available).
    top_mover_key = None
    top_mover_delta = 0
    weekly_growth = 0
    now = datetime.now(timezone.utc)

    if history:
        # 24h or earliest reference snapshot
        ref_24h = _find_snapshot_at_or_before(history, now - timedelta(hours=24))
        if not ref_24h:
            ref_24h = history[0]
        ref_7d = _find_snapshot_at_or_before(history, now - timedelta(days=7)) or history[0]

        for key, v in entities.items():
            block = v.get(score_key) or v.get("google") or {}
            cur = _to_int(block.get("count"))
            if cur is None:
                continue
            prev_24h = _to_int((_section_block(ref_24h, section).get(key) or {}).get("count"))
            if prev_24h is not None:
                delta = cur - prev_24h
                if abs(delta) > abs(top_mover_delta):
                    top_mover_delta = delta
                    top_mover_key = key
            prev_7d = _to_int((_section_block(ref_7d, section).get(key) or {}).get("count"))
            if prev_7d is not None:
                weekly_growth += max(0, cur - prev_7d)

    return {
        "total_count": total_count,
        "avg_rating": avg_rating,
        "top_mover_key": top_mover_key,
        "top_mover_delta": top_mover_delta,
        "weekly_growth": weekly_growth,
        "entity_count": len(entities),
    }


def append_history(data: dict) -> None:
    """Append a compact snapshot to the history log (covers venues + apps)."""
    venues = data.get("venues") or {}
    apps = data.get("apps") or {}
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "venues": {
            key: {
                "rating": _to_float((v.get("google") or {}).get("rating")),
                "count":  _to_int((v.get("google") or {}).get("count")),
            }
            for key, v in venues.items()
        },
        "apps": {
            key: {
                "rating": _to_float((a.get("combined") or {}).get("rating")),
                "count":  _to_int((a.get("combined") or {}).get("count")),
            }
            for key, a in apps.items()
        },
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")
    except Exception as e:
        logger.warning(f"history append failed: {e}")
