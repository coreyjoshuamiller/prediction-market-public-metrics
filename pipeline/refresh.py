"""Decide what the scheduled refresh should do, so it's safe to trigger it many times a day.

GitHub's cron is best-effort, so the daily workflow is triggered from several cron slots and
from the continuous order-book chain. Each trigger runs this, which picks one mode:

  full   today's full refresh hasn't happened yet          (~30-35 Dune credits)
  light  it has, but a source is still missing yesterday   (~1 credit: Kalshi + Polymarket US)
  skip   data is current, or today's light retries are used up

The decision uses data/refresh_state.json (committed by the workflow) and the data files
themselves, not git history or the cron slot, and the run date is fixed once at the start.

  python -m pipeline.refresh --check            print the mode; exit 0 if a run is needed, 1 if not
  python -m pipeline.refresh --auto [--force M] decide (or force) and run; writes mode to $GITHUB_OUTPUT
"""
import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "refresh_state.json")
MAX_LIGHT_PER_DAY = 4


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"last_full": None, "attempts": {}}


def save_state(state):
    # keep a week of attempt history
    keep = sorted(state.get("attempts", {}))[-7:]
    state["attempts"] = {d: state["attempts"][d] for d in keep}
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


def _days(path, col, filt=lambda r: True):
    p = os.path.join(DATA, path)
    if not os.path.exists(p):
        return set()
    return {r[col] for r in csv.DictReader(open(p)) if filt(r)}


def missing_sources(today):
    """Sources that don't yet have yesterday's (UTC) data."""
    y = (today - timedelta(days=1)).isoformat()
    missing = []
    if y not in _days("poly_totals_daily.csv", "d"):
        missing.append("polymarket")
    if y not in _days("kalshi_series_daily.csv", "d", lambda r: r["series"] == "__TOTAL__"):
        missing.append("kalshi")
    # Polymarket US session file YYYYMMDD covers up to 21:00 UTC that day; yesterday's file
    # (published ~00:10 UTC today) is the one we expect
    if f"{y.replace('-', '')}-time-and-sales.csv" not in _days("pmus_daily.csv", "file"):
        missing.append("polymarket_us")
    return missing


def decide(today, state=None):
    state = state if state is not None else load_state()
    if state.get("last_full") != today.isoformat():
        return "full", []
    missing = missing_sources(today)
    if not missing:
        return "skip", []
    if state.get("attempts", {}).get(today.isoformat(), 0) >= MAX_LIGHT_PER_DAY:
        return "skip", missing
    return "light", missing


def run(mode, today):
    from . import pmus
    from .build_site import build
    from .update import daily, kalshi_window, poly_fees_window, poly_totals_window

    if mode == "full":
        daily(today=today)
    elif mode == "light":
        end = today
        kalshi_window(end - timedelta(days=10), end)
        pmus.update(last_n=3)
        if "polymarket" in missing_sources(today):
            poly_totals_window(end - timedelta(days=3), end)
            poly_fees_window(end - timedelta(days=3), end)
    if mode != "skip":
        build()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--force", choices=["full", "light", "skip"])
    ap.add_argument("--today", help="override the run date (testing)")
    args = ap.parse_args()
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()

    state = load_state()
    mode, missing = decide(today, state)
    if args.force:
        mode = args.force
    print(f"refresh {today}: mode={mode}" + (f" (missing: {', '.join(missing)})" if missing else "")
          + f" last_full={state.get('last_full')} attempts_today={state.get('attempts', {}).get(today.isoformat(), 0)}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"mode={mode}\nrun_date={today}\n")
    if args.check:
        sys.exit(0 if mode != "skip" else 1)
    if args.auto and mode != "skip":
        run(mode, today)
        state = load_state()
        if mode == "full":
            state["last_full"] = today.isoformat()
        state.setdefault("attempts", {})[today.isoformat()] = state.get("attempts", {}).get(today.isoformat(), 0) + 1
        save_state(state)
        left = missing_sources(today)
        print(f"refresh {today}: done ({mode})" + (f"; still missing: {', '.join(left)}" if left else "; all sources current"))


if __name__ == "__main__":
    main()
