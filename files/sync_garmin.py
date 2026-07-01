#!/usr/bin/env python3
"""
sync_garmin.py - pull your own Garmin data into a folder your AI coach can read,
or POST it to your own ingest endpoint.

This is a thin wrapper around cyberjunky's python-garminconnect library:
    https://github.com/cyberjunky/python-garminconnect

It is read-only. It never writes anything back to your Garmin account.

Typical use:

    # one-time login (asks for password / 2FA, saves a token, prints a token bundle)
    export GARMIN_EMAIL="you@example.com"
    export GARMIN_PASSWORD="your-password"
    python sync_garmin.py --login

    # see the last 3 days without writing anything
    python sync_garmin.py --days 3 --dry-run

    # write markdown notes + data.json into ./garmin
    python sync_garmin.py --days 3 --sink files --out ./garmin

    # POST {activities, wellness} to your own endpoint
    export GARMIN_INGEST_URL="https://yoursite.com/api/garmin/ingest"
    export GARMIN_INGEST_SECRET="your-shared-secret"
    python sync_garmin.py --days 3 --sink supabase
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from garminconnect import Garmin


def _import_garmin():
    """Import the Garmin class lazily so --help works without the library."""
    try:
        from garminconnect import Garmin

        return Garmin
    except ImportError:
        sys.exit(
            "Missing dependency. Run:  pip install -r requirements.txt\n"
            "(installs garminconnect by cyberjunky and requests)"
        )


# Where the long-lived login token is stored on this machine.
TOKENSTORE = os.environ.get(
    "GARMINTOKENS", os.path.expanduser("~/.garminconnect")
)
# Env var name the cloud workflow uses to pass the token bundle.
TOKEN_B64_ENV = "GARMIN_TOKEN_B64"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _login_interactive() -> "Garmin":
    """Full login with email/password (and 2FA if Garmin asks). Saves a token."""
    Garmin = _import_garmin()
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit(
            "Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables first.\n"
            '  export GARMIN_EMAIL="you@example.com"\n'
            '  export GARMIN_PASSWORD="your-password"'
        )

    # garminconnect handles MFA via an interactive prompt by default, and its
    # newer login() tries several strategies (mobile, then the widget/portal
    # web flow that bypasses Garmin's 429 rate limiting).
    garmin = Garmin(email=email, password=password)
    try:
        garmin.login()
    except Exception as exc:  # noqa: BLE001 - upstream raises many error types
        _explain_login_error(exc)

    # Persist the token so future runs skip the password.
    # garminconnect >=0.3 keeps auth on garmin.client (older versions used
    # garmin.garth). Support whichever this install exposes.
    try:
        _auth_holder(garmin).dump(TOKENSTORE)
    except Exception as exc:  # noqa: BLE001
        _explain_login_error(f"logged in but could not save the token: {exc}")
    print(f"Login OK. Token saved to {TOKENSTORE}")
    return garmin


def _auth_holder(garmin):
    """Return the object that serializes auth tokens (.client on >=0.3, else .garth)."""
    holder = getattr(garmin, "client", None) or getattr(garmin, "garth", None)
    if holder is None:
        _explain_login_error("login did not complete (no session/token was created)")
    return holder


def _explain_login_error(exc) -> None:
    """Turn a raw login failure into a plain-English message, then exit."""
    msg = str(exc)
    if "429" in msg or "rate" in msg.lower():
        sys.exit(
            "\nGarmin rate-limited this network (HTTP 429).\n"
            "Your password was NOT the problem — Garmin just refused the "
            "request because of too many login attempts from your IP.\n\n"
            "What to do:\n"
            "  1. STOP retrying — each attempt extends the cooldown.\n"
            "  2. Wait ~1-3 hours, then run --login once more, OR\n"
            "  3. Try right now from a different network (e.g. your phone's\n"
            "     hotspot) — the limit is tied to your internet IP.\n"
        )
    if "401" in msg or "credential" in msg.lower() or "invalid" in msg.lower():
        sys.exit(
            "\nGarmin rejected the email or password (HTTP 401).\n"
            "Double-check GARMIN_EMAIL / GARMIN_PASSWORD and try again.\n"
        )
    sys.exit(f"\nLogin failed: {exc}\n")


def _print_token_bundle(garmin) -> None:
    """Print the saved token as one base64 string (for the Path A secret)."""
    token_b64 = base64.b64encode(
        _token_string(garmin).encode("utf-8")
    ).decode("utf-8")
    print("\n--- GARMIN_TOKEN_B64 (copy everything on the next line) ---")
    print(token_b64)
    print("--- end ---\n")
    print(
        "Store this as the GARMIN_TOKEN_B64 secret if you use the GitHub Actions "
        "path. It lasts about a year. Keep it private: it is a login credential."
    )


def _token_string(garmin) -> str:
    """Serialize the current auth tokens to a string."""
    raw = _auth_holder(garmin).dumps()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return raw


def _pad_b64(s: str) -> str:
    """Strip whitespace and restore any '=' padding stripped in transit (e.g. by
    a secrets store or a copy-paste) so base64 decoding does not fail."""
    s = "".join(s.split())
    return s + "=" * (-len(s) % 4)


def _resume() -> "Garmin":
    """Log in using a saved token. Tries the base64 env var, then the token dir."""
    Garmin = _import_garmin()
    garmin = Garmin()

    token_b64 = os.environ.get(TOKEN_B64_ENV)
    if token_b64:
        raw = base64.b64decode(_pad_b64(token_b64)).decode("utf-8")
        # login(token_string) loads the token AND fetches the profile, which
        # sets display_name. get_user_summary (resting HR, steps, stress, body
        # battery) needs it; a bare client.loads() skips that and those fields
        # come back empty.
        garmin.login(raw)
        return garmin

    if not Path(TOKENSTORE).expanduser().exists():
        sys.exit(
            "No saved login found. Run a one-time login first:\n"
            "  python sync_garmin.py --login"
        )

    garmin.login(TOKENSTORE)
    return garmin


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
def _safe(fn, *args, default=None):
    """Call a Garmin getter, swallow errors and missing-data responses."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - upstream raises many error types
        print(f"  (skip {getattr(fn, '__name__', fn)}: {exc})", file=sys.stderr)
        return default


def _num(value):
    """Round floats to one decimal, pass ints/None through."""
    if isinstance(value, float):
        return round(value, 1)
    return value


def collect_wellness(garmin: "Garmin", day: date) -> dict:
    """Pull one day of wellness metrics into a flat dict."""
    cdate = day.isoformat()

    summary = _safe(garmin.get_user_summary, cdate, default={}) or {}
    sleep = _safe(garmin.get_sleep_data, cdate, default={}) or {}
    hrv = _safe(garmin.get_hrv_data, cdate, default={}) or {}
    rhr = _safe(garmin.get_rhr_day, cdate, default={}) or {}
    readiness = _safe(garmin.get_training_readiness, cdate, default=[]) or []

    sleep_dto = (sleep or {}).get("dailySleepDTO", {}) or {}
    hrv_summary = (hrv or {}).get("hrvSummary", {}) or {}

    # resting HR can live in a couple of places depending on the endpoint
    resting_hr = summary.get("restingHeartRate")
    if resting_hr is None:
        metrics = (rhr or {}).get("allMetrics", {}).get("metricsMap", {})
        rhr_list = metrics.get("WELLNESS_RESTING_HEART_RATE", [])
        if rhr_list:
            resting_hr = rhr_list[0].get("value")

    sleep_seconds = sleep_dto.get("sleepTimeSeconds")
    sleep_hours = round(sleep_seconds / 3600, 1) if sleep_seconds else None
    sleep_score = (sleep_dto.get("sleepScores", {}) or {}).get(
        "overall", {}
    ).get("value")

    readiness_score = None
    if isinstance(readiness, list) and readiness:
        readiness_score = readiness[0].get("score")

    return {
        "date": cdate,
        "resting_hr": resting_hr,
        "hrv_overnight": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
        "sleep_hours": sleep_hours,
        "sleep_score": sleep_score,
        "body_battery_low": summary.get("bodyBatteryLowestValue"),
        "body_battery_high": summary.get("bodyBatteryHighestValue"),
        "stress_avg": summary.get("averageStressLevel"),
        "steps": summary.get("totalSteps"),
        "training_readiness": readiness_score,
    }


def collect_activities(garmin: "Garmin", start: date, end: date) -> list[dict]:
    """Pull activities between start and end (inclusive) into flat dicts."""
    raw = _safe(
        garmin.get_activities_by_date,
        start.isoformat(),
        end.isoformat(),
        default=[],
    ) or []

    activities = []
    for act in raw:
        duration_s = act.get("duration") or 0
        distance_m = act.get("distance") or 0
        activities.append(
            {
                "id": act.get("activityId"),
                "name": act.get("activityName"),
                "type": (act.get("activityType") or {}).get("typeKey"),
                "start": act.get("startTimeLocal"),
                "duration_min": _num(duration_s / 60) if duration_s else None,
                "distance_km": _num(distance_m / 1000) if distance_m else None,
                "avg_hr": act.get("averageHR"),
                "max_hr": act.get("maxHR"),
                "calories": act.get("calories"),
                "elevation_gain_m": _num(act.get("elevationGain")),
                "avg_speed_kmh": _num((act.get("averageSpeed") or 0) * 3.6)
                if act.get("averageSpeed")
                else None,
                "training_load": act.get("activityTrainingLoad"),
            }
        )
    return activities


def _parse_race(d) -> dict:
    """Extract the four race-time predictions (seconds) from one Garmin record."""
    if not isinstance(d, dict):
        return {}
    return {
        "sec_5k": d.get("time5K"),
        "sec_10k": d.get("time10K"),
        "sec_half": d.get("timeHalfMarathon"),
        "sec_marathon": d.get("timeMarathon"),
    }


def collect_fitness(garmin: "Garmin", start: date, end: date) -> dict:
    """Pull Garmin's race predictions (latest + daily history) and VO2max."""
    cdate = end.isoformat()
    latest = _safe(garmin.get_race_predictions, default=None)
    daily = _safe(
        garmin.get_race_predictions,
        start.isoformat(),
        end.isoformat(),
        "daily",
        default=None,
    )
    maxm = _safe(garmin.get_max_metrics, cdate, default=None)

    src = latest[-1] if isinstance(latest, list) and latest else latest
    rp = _parse_race(src)

    # Daily history: {calendarDate: {sec_5k, sec_10k, sec_half, sec_marathon}}.
    history = {}
    items = daily if isinstance(daily, list) else ([daily] if daily else [])
    for it in items:
        if isinstance(it, dict) and it.get("calendarDate"):
            pr = _parse_race(it)
            if pr.get("sec_marathon"):
                history[it["calendarDate"]] = pr

    # VO2max: a list whose first item has "generic" (running) and "cycling".
    mm = maxm[0] if isinstance(maxm, list) and maxm else maxm
    vo2_run = vo2_bike = None
    if isinstance(mm, dict):
        generic = mm.get("generic") or {}
        cycling = mm.get("cycling") or {}
        vo2_run = generic.get("vo2MaxValue") or generic.get("vo2MaxPreciseValue")
        vo2_bike = cycling.get("vo2MaxValue") or cycling.get("vo2MaxPreciseValue")

    return {
        "date": cdate,
        "vo2max_run": _num(vo2_run),
        "vo2max_bike": _num(vo2_bike),
        "race_pred": rp,
        "history": history,
        # Keep the raw payloads so the dashboard can recover if key names differ.
        "race_pred_raw": latest,
        "max_metrics_raw": maxm,
    }


# --------------------------------------------------------------------------- #
# Rendering / output
# --------------------------------------------------------------------------- #
def render_daily_md(w: dict) -> str:
    lines = [f"# Garmin wellness {w['date']}"]

    def add(label, value, suffix=""):
        if value is not None:
            lines.append(f"- {label}: {value}{suffix}")

    add("Resting HR", w["resting_hr"], " bpm")
    if w["hrv_overnight"] is not None:
        status = f" ({w['hrv_status'].lower()})" if w.get("hrv_status") else ""
        lines.append(f"- HRV (overnight): {w['hrv_overnight']} ms{status}")
    if w["sleep_hours"] is not None:
        score = f" (score {w['sleep_score']})" if w["sleep_score"] else ""
        lines.append(f"- Sleep: {w['sleep_hours']} h{score}")
    if w["body_battery_low"] is not None and w["body_battery_high"] is not None:
        lines.append(
            f"- Body battery: {w['body_battery_low']} -> {w['body_battery_high']}"
        )
    add("Stress (avg)", w["stress_avg"])
    add("Steps", w["steps"])
    add("Training readiness", w["training_readiness"])
    return "\n".join(lines) + "\n"


def render_activity_md(a: dict) -> str:
    lines = [f"# {a['name'] or a['type'] or 'Activity'}"]

    def add(label, value, suffix=""):
        if value is not None:
            lines.append(f"- {label}: {value}{suffix}")

    add("Type", a["type"])
    add("Start", a["start"])
    add("Duration", a["duration_min"], " min")
    add("Distance", a["distance_km"], " km")
    add("Avg pace/speed", a["avg_speed_kmh"], " km/h")
    add("Avg HR", a["avg_hr"], " bpm")
    add("Max HR", a["max_hr"], " bpm")
    add("Elevation gain", a["elevation_gain_m"], " m")
    add("Calories", a["calories"])
    add("Training load", a["training_load"])
    return "\n".join(lines) + "\n"


def _activity_slug(a: dict) -> str:
    """A filesystem-safe date+name slug for an activity note."""
    day = (a.get("start") or "")[:10] or "undated"
    name = (a.get("name") or a.get("type") or "activity").lower()
    safe = "".join(c if c.isalnum() else "-" for c in name)
    safe = "-".join(filter(None, safe.split("-")))[:40] or "activity"
    return f"{day}-{safe}"


def write_files(
    out: Path,
    wellness: list[dict],
    activities: list[dict],
    fitness: dict | None = None,
) -> None:
    daily_dir = out / "daily"
    act_dir = out / "activities"
    daily_dir.mkdir(parents=True, exist_ok=True)
    act_dir.mkdir(parents=True, exist_ok=True)

    for w in wellness:
        (daily_dir / f"{w['date']}.md").write_text(
            render_daily_md(w), encoding="utf-8"
        )
    for a in activities:
        (act_dir / f"{_activity_slug(a)}.md").write_text(
            render_activity_md(a), encoding="utf-8"
        )

    # Merge into data.json so the store grows instead of being overwritten.
    store_path = out / "data.json"
    store = {"wellness": {}, "activities": {}}
    if store_path.exists():
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
            store.setdefault("wellness", {})
            store.setdefault("activities", {})
        except json.JSONDecodeError:
            pass
    for w in wellness:
        store["wellness"][w["date"]] = w
    for a in activities:
        store["activities"][str(a["id"])] = a
    if fitness:
        has_pred = bool(fitness.get("race_pred", {}).get("sec_marathon"))
        if has_pred or fitness.get("vo2max_run"):
            store["fitness"] = {k: v for k, v in fitness.items() if k != "history"}
        # Accumulate the prediction history so we can chart it over time.
        hist = dict(fitness.get("history") or {})
        if has_pred:
            hist[fitness["date"]] = fitness["race_pred"]
        if hist:
            store.setdefault("fitness_history", {}).update(hist)
    store["updated_at"] = datetime.now().astimezone().isoformat()
    store_path.write_text(
        json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(
        f"Wrote {len(wellness)} daily note(s) and {len(activities)} "
        f"activity note(s) to {out}/"
    )


def post_supabase(wellness: list[dict], activities: list[dict]) -> None:
    import requests

    url = os.environ.get("GARMIN_INGEST_URL")
    secret = os.environ.get("GARMIN_INGEST_SECRET") or os.environ.get(
        "SESSION_LOG_SECRET"
    )
    if not url:
        sys.exit("Set GARMIN_INGEST_URL to use --sink supabase.")

    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    resp = requests.post(
        url,
        json={"activities": activities, "wellness": wellness},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"POSTed {len(activities)} activities + {len(wellness)} wellness "
          f"days to {url} ({resp.status_code})")


def print_dry_run(wellness: list[dict], activities: list[dict]) -> None:
    print("\n===== WELLNESS =====")
    for w in wellness:
        print(render_daily_md(w))
    print("===== ACTIVITIES =====")
    if not activities:
        print("(no activities in this range)")
    for a in activities:
        print(render_activity_md(a))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--login",
        action="store_true",
        help="one-time login; saves a token and prints a base64 token bundle",
    )
    p.add_argument(
        "--days",
        type=int,
        default=3,
        help="how many days back to pull (default: 3)",
    )
    p.add_argument(
        "--sink",
        choices=["files", "supabase"],
        default="files",
        help="where to send the data (default: files)",
    )
    p.add_argument(
        "--out",
        default="./garmin",
        help="output folder for --sink files (default: ./garmin)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print everything, write nothing",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.login:
        garmin = _login_interactive()
        _print_token_bundle(garmin)
        return 0

    garmin = _resume()

    end = date.today()
    start = end - timedelta(days=max(args.days - 1, 0))

    wellness = []
    day = start
    while day <= end:
        wellness.append(collect_wellness(garmin, day))
        day += timedelta(days=1)
    activities = collect_activities(garmin, start, end)
    fitness = collect_fitness(garmin, start, end)

    if args.dry_run:
        print_dry_run(wellness, activities)
        return 0

    if args.sink == "files":
        write_files(Path(args.out).expanduser(), wellness, activities, fitness)
    elif args.sink == "supabase":
        post_supabase(wellness, activities)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
