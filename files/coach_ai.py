#!/usr/bin/env python3
"""
coach_ai.py - optional AI coach note for the Garmin dashboard.

Reads garmin/data.json, summarizes the recent training + recovery picture and
the Berlin goal, asks Claude for a short coaching interpretation (in Spanish),
and writes it back into data.json under "coach". The dashboard shows it as the
"Que dicen tus datos" analysis.

It is a no-op (exit 0) when ANTHROPIC_API_KEY is not set, so the daily sync keeps
working without it. Uses the official Anthropic Python SDK.

    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."
    python coach_ai.py --out ./garmin
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Berlin Marathon goal, kept in sync with the dashboard.
GOAL = {"race": "Maraton de Berlin", "date": "2026-09-27", "sec": 10200, "pace_km": 241}
DEFAULT_MODEL = os.environ.get("COACH_MODEL", "claude-opus-4-8")


def _fmt_time(sec):
    if not sec:
        return "-"
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace(sec_per_km):
    if not sec_per_km:
        return "-"
    m, s = int(sec_per_km // 60), int(round(sec_per_km % 60))
    return f"{m}:{s:02d}/km"


def _monday(dstr):
    d = datetime.strptime(dstr[:10], "%Y-%m-%d").date()
    return d - timedelta(days=d.weekday())


def _sport(t):
    t = (t or "").lower()
    if "run" in t or "treadmill" in t:
        return "run"
    if "cycl" in t or "bik" in t or "ride" in t:
        return "bike"
    if "swim" in t:
        return "swim"
    return "other"


def _avg(rows, key):
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def build_summary(store: dict) -> str:
    """Turn data.json into a compact, human-readable brief for the model."""
    wellness = sorted(store.get("wellness", {}).values(), key=lambda w: w["date"])
    acts = list(store.get("activities", {}).values())
    fit = store.get("fitness", {}) or {}
    hist = store.get("fitness_history", {}) or {}

    lines = []
    today = date.today()
    days = max(0, (date.fromisoformat(GOAL["date"]) - today).days)
    lines.append(f"- Meta: {GOAL['race']} el {GOAL['date']} (faltan {days} dias). "
                 f"Objetivo {_fmt_time(GOAL['sec'])} a {_fmt_pace(GOAL['pace_km'])}.")

    rp = fit.get("race_pred", {}) or {}
    cur = rp.get("sec_marathon")
    if cur:
        lines.append(f"- Prediccion de maraton actual (Garmin): {_fmt_time(cur)} "
                     f"({_fmt_pace(cur / 42.195)}). Faltan {_fmt_time(cur - GOAL['sec'])} "
                     f"para la meta.")
    hitems = sorted(
        ((d, p.get("sec_marathon")) for d, p in hist.items() if p.get("sec_marathon")),
        key=lambda x: x[0],
    )
    if len(hitems) >= 2 and cur:
        delta = hitems[0][1] - hitems[-1][1]
        word = "mejorando" if delta > 0 else "empeorando" if delta < 0 else "estable"
        lines.append(f"- Tendencia de la prediccion: {word} "
                     f"({_fmt_time(abs(delta))} en {len(hitems)} registros).")
    if fit.get("vo2max_run"):
        lines.append(f"- VO2max carrera: {fit['vo2max_run']}.")

    # weekly running km (last 2 weeks)
    weeks = {}
    for a in acts:
        if not a.get("start") or _sport(a.get("type")) != "run":
            continue
        weeks.setdefault(_monday(a["start"]), 0.0)
        weeks[_monday(a["start"])] += a.get("distance_km") or 0
    wk = sorted(weeks)
    if wk:
        this_wk = weeks[wk[-1]]
        prev = weeks[wk[-2]] if len(wk) >= 2 else None
        chg = f" (vs {prev:.0f} km la semana previa)" if prev else ""
        lines.append(f"- Km de carrera esta semana: {this_wk:.0f} km{chg}.")

    last7 = wellness[-7:]
    sl = _avg(last7, "sleep_hours")
    if sl is not None:
        lines.append(f"- Sueno promedio (7 dias): {sl:.1f} h.")
    h7 = _avg(wellness[-7:], "hrv_overnight")
    h14 = _avg(wellness[-14:-7], "hrv_overnight")
    if h7 and h14:
        lines.append(f"- HRV nocturna: {h7:.0f} ms (7 dias) vs {h14:.0f} ms (semana previa).")
    if last7:
        last = last7[-1]
        extra = []
        if last.get("resting_hr"):
            extra.append(f"HR reposo {last['resting_hr']} bpm")
        if last.get("training_readiness"):
            extra.append(f"readiness {last['training_readiness']}/100")
        if last.get("stress_avg"):
            extra.append(f"estres {last['stress_avg']}")
        if extra:
            lines.append("- Hoy: " + ", ".join(extra) + ".")

    # acute:chronic workload ratio
    dayload = {}
    for a in acts:
        if a.get("start") and a.get("training_load"):
            k = a["start"][:10]
            dayload[k] = dayload.get(k, 0) + a["training_load"]
    acute = chronic = 0.0
    for i in range(28):
        d = (today - timedelta(days=i)).isoformat()
        v = dayload.get(d, 0)
        chronic += v
        if i < 7:
            acute += v
    if chronic > 0:
        acwr = acute / (chronic / 4)
        lines.append(f"- Ratio carga aguda:cronica (ACWR): {acwr:.2f} "
                     f"(0.8-1.3 optimo, >1.5 riesgo).")

    return "\n".join(lines)


SYSTEM = (
    "Eres un entrenador de resistencia experto, honesto y motivador. Analizas los "
    "datos de Garmin de un atleta que entrena para el Maraton de Berlin con meta "
    "sub-2:50. Escribe en espanol, en segunda persona (tu), una interpretacion "
    "breve y accionable de 130-190 palabras. Cubre: si esta mejorando o estancado, "
    "que tan lejos esta de la meta, y 2-3 acciones concretas para esta semana "
    "(volumen, ritmo objetivo, sueno, recuperacion, carga). Usa solo los datos "
    "dados; no inventes numeros. Tono directo y alentador, sin listas con vinetas, "
    "en 2-3 parrafos cortos."
)


def generate_note(summary: str, model: str) -> str:
    try:
        import anthropic
    except ImportError:
        sys.exit("Missing dependency. Run: pip install anthropic")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    resp = client.messages.create(
        model=model,
        max_tokens=700,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": "Datos del atleta:\n" + summary +
                       "\n\nEscribe el analisis del entrenador.",
        }],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="./garmin", help="folder containing data.json")
    args = p.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set - skipping AI coach note.")
        return 0

    store_path = Path(args.out).expanduser() / "data.json"
    if not store_path.exists():
        print(f"No data.json at {store_path} - skipping AI coach note.")
        return 0

    store = json.loads(store_path.read_text(encoding="utf-8"))
    summary = build_summary(store)
    try:
        text = generate_note(summary, DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001 - never fail the sync over the coach note
        print(f"AI coach note failed ({exc}); leaving data.json unchanged.", file=sys.stderr)
        return 0

    if text:
        store["coach"] = {
            "text": text,
            "generated_at": datetime.now().astimezone().isoformat(),
            "model": DEFAULT_MODEL,
        }
        store_path.write_text(
            json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"Wrote AI coach note ({len(text)} chars) using {DEFAULT_MODEL}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
