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


def enrich_with_trends(data: dict) -> dict:
    """
    Mutate `data` in place: add a "trends" sub-dict to each venue and app.
    For venues, the comparison is against the Google `rating`/`count`.
    For apps, the comparison is against the `combined` (iOS+Android) block.
    """
    history = _read_history()
    venues = data.get("venues") or {}
    apps = data.get("apps") or {}

    for key, v in venues.items():
        if not history:
            v["trends"] = {}
            continue
        google_block = v.get("google") or {}
        v["trends"] = _compute_entity_trends("venues", key, google_block, history)

    for key, a in apps.items():
        if not history:
            a["trends"] = {}
            continue
        combined = a.get("combined") or {}
        a["trends"] = _compute_entity_trends("apps", key, combined, history)

    return data


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
