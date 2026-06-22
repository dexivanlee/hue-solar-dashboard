"""
Daily DeyeCloud capture for Hue Hotel Siargao solar dashboard.

Runs in GitHub Actions every morning. Reads the current solar_data.json,
fetches each missing day from DeyeCloud's API using the bearer token in
DEYE_TOKEN, computes the day's stats, generates a templated commentary,
and writes the merged file back. The workflow step then commits and
pushes any changes.

Designed to be safe and idempotent:
- If yesterday is already present, nothing is written.
- Never captures more than 14 days at a time.
- On a 401 (expired token), exits cleanly with a clear log message
  so the workflow surfaces it without crashing.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

STATION_ID = "61801841"
DATA_FILE = "solar_data.json"
DIESEL_LPH = 43.0          # observed average burn rate, L/hr
DIESEL_PRICE_PHP = 86.50   # PHP per litre


def fetch_day(token, year, month, day):
    url = (
        f"https://www.deyecloud.com/maintain-s/history/batteryPower/"
        f"{STATION_ID}/stats/daily?year={year}&month={month}&day={day}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def hhmm(idx):
    minutes = idx * 5
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def make_commentary(pv, gen, load, min_soc, max_soc, hours, runs):
    total = pv + gen
    share = (gen / total * 100) if total > 0 else 0
    dod = max_soc - min_soc
    litres = hours * DIESEL_LPH
    cost = litres * DIESEL_PRICE_PHP

    p1 = (
        f"Solar produced {pv:.0f} kWh and the diesel genset added {gen:.0f} kWh, "
        f"putting genset at {share:.0f} percent of system energy against a load of "
        f"{load:.0f} kWh."
    )

    if min_soc < 25:
        p2 = (
            f"CRITICAL: Battery SOC dropped to {min_soc:.1f} percent, near the "
            f"20 percent hard floor. This is a near-shutdown event - the evening "
            f"genset should have started earlier. Depth of discharge was {dod:.0f} points."
        )
    elif min_soc < 28:
        p2 = (
            f"Battery min of {min_soc:.1f} percent landed below the 28-35 percent "
            f"manual-start band - the genset was triggered later than the rule "
            f"suggests. Depth of discharge was {dod:.0f} points."
        )
    elif min_soc < 36:
        p2 = (
            f"Battery min of {min_soc:.1f} percent sat within the 28-35 percent "
            f"manual-start band - timing was on target. Depth of discharge was "
            f"{dod:.0f} points; max SOC reached {max_soc:.0f} percent."
        )
    else:
        p2 = (
            f"Battery stayed comfortably above the manual-start band; min SOC "
            f"{min_soc:.1f} percent, max {max_soc:.0f} percent, depth of discharge "
            f"{dod:.0f} points."
        )

    run_word = "window" if len(runs) == 1 else "windows"
    p3 = (
        f"Genset ran {hours:.2f} hours across {len(runs)} {run_word} for an "
        f"estimated PHP {cost:,.0f} in diesel ({litres:.0f} L)."
    )
    if share > 45:
        p3 += (
            " Genset share above 45 percent indicates weak solar or elevated load - "
            "worth investigating which."
        )
    elif share < 25 and hours > 0:
        p3 += " This is a clean operating day - the pattern to replicate."
    return f"{p1}\n\n{p2}\n\n{p3}"


def compute_entry(data, iso):
    rec = data.get("records") or []
    if not rec:
        return None

    step_h = 5 / 60
    pv_kwh = gen_kwh = 0.0
    peak_pv = peak_load = 0.0
    min_soc, max_soc = 999.0, -1.0

    for r in rec:
        p = float(r.get("pvPower") or 0)
        g = float(r.get("generatorPower") or 0)
        u = float(r.get("usePower") or 0)
        s = r.get("batterySoc")
        pv_kwh += p * step_h / 1000.0
        gen_kwh += g * step_h / 1000.0
        if p > peak_pv:
            peak_pv = p
        if u > peak_load:
            peak_load = u
        if s is not None:
            sv = float(s)
            if sv < min_soc:
                min_soc = sv
            if sv > max_soc:
                max_soc = sv

    # contiguous genset runs (generatorPower > 500W)
    runs = []
    cur = None
    for i, r in enumerate(rec):
        g = float(r.get("generatorPower") or 0)
        if g > 500:
            if cur is None:
                cur = {"s": i, "ss": r.get("batterySoc")}
            cur["e"] = i
            cur["es"] = r.get("batterySoc")
        else:
            if cur is not None:
                runs.append(cur)
                cur = None
    if cur is not None:
        runs.append(cur)

    genset_hours = sum((r["e"] - r["s"] + 1) * 5 / 60 for r in runs)
    stats = data.get("statistics") or {}
    load_val = stats.get("useValue")
    sunset_soc = None
    if len(rec) > 210:
        s210 = rec[210].get("batterySoc")
        if s210 is not None:
            sunset_soc = round(float(s210), 1)

    entry = {
        "date": iso,
        "source": "auto",
        "pv": round(pv_kwh, 1),
        "load": load_val,
        "gensetKwh": round(gen_kwh, 1),
        "gensetHours": round(genset_hours, 2),
        "peakPv": round(peak_pv / 1000.0, 2),
        "peakLoad": round(peak_load / 1000.0, 2),
        "minSoc": round(min_soc, 1) if min_soc < 999 else None,
        "maxSoc": round(max_soc, 1) if max_soc > -1 else None,
        "sunsetSoc": sunset_soc,
        "rooms": None,
        "alarms": "None surfaced by DeyeCloud.",
        "notes": "Auto-captured from DeyeCloud station 61801841 via GitHub Actions.",
        "commentary": make_commentary(
            pv_kwh, gen_kwh, load_val or 0, min_soc, max_soc, genset_hours, runs
        ),
        "events": [
            {
                "startTime": hhmm(r["s"]),
                "startSoc": round(float(r["ss"])) if r["ss"] is not None else None,
                "stopTime": hhmm(r["e"] + 1),
                "stopSoc": round(float(r["es"])) if r["es"] is not None else None,
                "reason": "",
            }
            for r in runs
        ],
    }
    return entry


def main():
    token = os.environ.get("DEYE_TOKEN", "").strip()
    if not token:
        print("ERROR: DEYE_TOKEN env var is not set. Add it as a repo secret.")
        sys.exit(1)

    with open(DATA_FILE) as f:
        current = json.load(f)

    days = current.get("days") or {}
    faults = current.get("faults") or []

    # Yesterday in Philippine Standard Time (UTC+8)
    pht_now = datetime.now(timezone.utc) + timedelta(hours=8)
    yesterday = (pht_now - timedelta(days=1)).date()

    if days:
        latest = max(datetime.strptime(d, "%Y-%m-%d").date() for d in days.keys())
    else:
        latest = yesterday - timedelta(days=14)

    if latest >= yesterday:
        print(f"Dashboard already up to date (latest: {latest}, yesterday PHT: {yesterday}). Nothing to do.")
        return

    captured = []
    skipped = []
    d = latest + timedelta(days=1)
    while d <= yesterday and len(captured) < 14:
        iso = d.isoformat()
        try:
            data = fetch_day(token, d.year, d.month, d.day)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            if e.code == 401:
                print(
                    f"AUTH FAILED ({e.code}) on {iso} - DEYE_TOKEN appears expired. "
                    f"Capture a fresh token from a logged-in browser (localStorage.deyeTokenKey) "
                    f"and update the DEYE_TOKEN repo secret. Body: {body}"
                )
                sys.exit(2)
            print(f"HTTP {e.code} for {iso}: {body}")
            break
        except Exception as e:
            print(f"Error fetching {iso}: {e}")
            break

        entry = compute_entry(data, iso)
        if entry is None:
            print(f"No records for {iso}, skipping")
            skipped.append(iso)
        else:
            days[iso] = entry
            captured.append(iso)
            print(
                f"Captured {iso}: pv={entry['pv']} gen={entry['gensetKwh']} "
                f"hours={entry['gensetHours']} minSoc={entry['minSoc']}"
            )
        d += timedelta(days=1)

    if captured:
        merged = {"days": days, "faults": faults}
        with open(DATA_FILE, "w") as f:
            json.dump(merged, f, indent=2)
        print(f"Wrote {DATA_FILE} with {len(captured)} new day(s): {captured}")
    else:
        print(f"Nothing captured. Skipped: {skipped}")


if __name__ == "__main__":
    main()
