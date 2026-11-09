#!/usr/bin/env python3
"""The daily data job (implementation plan phase 2 step 5, D18): the latest sea temperature and waves at every
sampled place, with each value's data kind and valid time, written to <out>/conditions.json. Moved here from the
site's repository (LimpidMaja/watertools) on 9 November 2026, so it runs on a public repository's free Actions minutes;
the site's build downloads what it publishes.

Lean on purpose (the owner's Actions budget, D18): one small regional subset per region and product, not global
fields, then sampled at each place's model cell from inputs/places.json (the site's place registry, copied here by its
scripts/dev/sync-data-inputs.mjs). The globe's global fields (sea temperature and wave height, D20)
are the exception, written as images under <out>/globe/.

Data kinds come from the products' own run schedules (product user manuals, read 3 November 2026), worked out from the
data itself, never from the clock of the machine that runs this:

  Ocean physics, daily means (CMEMS-GLO-PUM-001-024 issue 2.4, section e): one run a day with base time 00 UTC on the
  bulletin date B and a 10-day forecast (B to B+9). Each daily run adds one day of hindcast simulation (no data
  assimilation, -24 to 0 h); the near-real-time analysis (-192 to -24 h) replaces the simulations only once a week, in
  Thursday's run. So with T the latest Thursday on or before B: days up to T-2 are analysis, T-1 to B-1 simulation
  (one day on a Thursday, up to seven by the next Wednesday). B = the last valid date minus 9 days.
  Waves, 3-hourly instantaneous (CMEMS-GLO-PUM-001-027): runs at 00 and 12 UTC, analysis for the 24 hours before the
  base time J, forecast for the 240 hours after it. J = the last valid time minus 240 hours.

Validation (product plan section 10.1): every value finite and within physical bounds, valid times strictly
increasing, the run recent. A product that fails is left out of this run's file and marked failed; the previous
file's values for it are kept: the workflow starts from the last published files, with their real times.

Usage: python -I scripts/fetch.py <out-dir>    (needs the Copernicus logins, from the environment)
"""

import datetime as dt
import json
import math
import pathlib
import shutil
import sys
import http.client
import ssl
import time
import urllib.error
import urllib.request

import copernicusmarine as cm
import numpy as np

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".cache/live")
OCEAN = "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"
WAVES = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
DAYS_BACK = 2  # the simulation day and one analysis day, for "how has it changed"
SST_BOUNDS = (-2.5, 36.0)
HS_BOUNDS = (0.0, 25.0)
TP_BOUNDS = (0.0, 30.0)
NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

INPUTS = pathlib.Path(__file__).resolve().parent.parent / "inputs"
registry = json.loads((INPUTS / "places.json").read_text())


# The widest box one download may span, in degrees: coasts further apart go into separate boxes, so a worldwide list
# of coasts never asks for a box from Florida to Hawaii.
BOX_MAX_DEG = 6.0


def regions(key):
    """Download boxes for this product: the places' sampled cells grouped, region by region, into boxes of at most
    BOX_MAX_DEG on each side (a few tenths of a degree around a single place). Deterministic: places in registry
    order, each joining the first box of its region it fits in."""
    boxes = {}
    for p in registry["places"]:
        s = p["samples"][key]
        if "none" in s:
            continue
        lat, lon = s["lat"], s["lon"]
        n = 0
        while True:
            name = f"{p['region']}-{n}"
            b = boxes.get(name)
            if b is None:
                boxes[name] = [lat, lat, lon, lon, [(p["id"], lat, lon)]]
                break
            la0, la1, lo0, lo1 = min(b[0], lat), max(b[1], lat), min(b[2], lon), max(b[3], lon)
            if la1 - la0 <= BOX_MAX_DEG and lo1 - lo0 <= BOX_MAX_DEG:
                b[0], b[1], b[2], b[3] = la0, la1, lo0, lo1
                b[4].append((p["id"], lat, lon))
                break
            n += 1
    return boxes


def r(x, n=2):
    return None if x is None or not math.isfinite(float(x)) else round(float(x), n)


def subset(dataset, variables, box, start, end, **kw):
    la0, la1, lo0, lo1, _ = box
    return cm.open_dataset(dataset_id=dataset, variables=variables, minimum_latitude=la0 - 0.1, maximum_latitude=la1 + 0.1, minimum_longitude=lo0 - 0.1, maximum_longitude=lo1 + 0.1, start_datetime=start, end_datetime=end, **kw).load()


def at(da, lat, lon):
    v = da.sel(latitude=lat, longitude=lon, method="nearest")
    if abs(float(v["latitude"]) - lat) > 1e-3 or abs(float(v["longitude"]) - lon) > 1e-3:
        raise ValueError(f"cell {lat},{lon} not on the grid")
    return v


def ocean_kind(day, bulletin):
    """The data kind of a daily mean of the ocean product (module docstring): forecast from the bulletin date on,
    simulation back to the day before the latest weekly (Thursday) analysis run, analysis before that."""
    if day >= bulletin:
        return "forecast"
    thursday = bulletin - dt.timedelta(days=(bulletin.weekday() - 3) % 7)
    return "simulation" if day >= thursday - dt.timedelta(days=1) else "analysis"


def ocean():
    start = (NOW - dt.timedelta(days=DAYS_BACK + 1)).strftime("%Y-%m-%d")
    end = (NOW + dt.timedelta(days=12)).strftime("%Y-%m-%d")
    out, last_dates = {}, set()
    for region, box in sorted(regions("ocean").items()):
        da = subset(OCEAN, ["thetao"], box, start, end, minimum_depth=0, maximum_depth=1)["thetao"].isel(depth=0)
        dates = [str(t)[:10] for t in da["time"].values]
        if dates != sorted(set(dates)):
            raise ValueError(f"{region}: valid dates not strictly increasing")
        last_dates.add(dates[-1])
        bulletin = dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=9)
        for pid, lat, lon in box[4]:
            series = []
            for d, v in zip(dates, at(da, lat, lon).values):
                day = dt.date.fromisoformat(d)
                if day < bulletin - dt.timedelta(days=DAYS_BACK):
                    continue
                if not (SST_BOUNDS[0] <= float(v) <= SST_BOUNDS[1]):
                    raise ValueError(f"{pid} {d}: {v} degC outside {SST_BOUNDS}")
                kind = ocean_kind(day, bulletin)
                series.append({"date": d, "value": r(v), "kind": kind})
            out[pid] = {"cell": {"lat": lat, "lon": lon}, "bulletinDate": bulletin.isoformat(), "series": series}
    if len(last_dates) != 1:
        raise ValueError(f"regions disagree on the last forecast day: {sorted(last_dates)}")
    bulletin = dt.date.fromisoformat(last_dates.pop()) - dt.timedelta(days=9)
    if (NOW.date() - bulletin).days > 2:
        raise ValueError(f"run of {bulletin} is more than two days old")
    return {"dataset": OCEAN, "variable": "thetao, first model level (0.49 m), daily mean", "unit": "degC", "bulletinDate": bulletin.isoformat(), "runBase": f"{bulletin}T00:00Z", "places": out}


def filled_steps(has, what):
    """How many steps of a run to keep: all of them, except empty ones at the very end (the wave product has published
    its last forecast step without values, the run of 8 November 2026). Those are left out, never filled, and the
    forecast simply ends earlier. An empty step before a filled one is an error, as before."""
    has = [bool(x) for x in has]
    keep = len(has)
    while keep and not has[keep - 1]:
        keep -= 1
    if not keep or not all(has[:keep]):
        raise ValueError(f"{what}: empty steps inside the run ({has.count(False)} of {len(has)})")
    if keep < len(has):
        print(f"{what}: left out {len(has) - keep} empty steps at the end of the run", file=sys.stderr)
    return keep


def waves():
    start = (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00")
    end = (NOW + dt.timedelta(days=11)).strftime("%Y-%m-%dT00:00")
    out, bases = {}, set()
    for region, box in sorted(regions("waves").items()):
        ds = subset(WAVES, ["VHM0", "VTPK", "VMDR"], box, start, end)
        times = [np.datetime64(t, "s") for t in ds["time"].values]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError(f"{region}: valid times not strictly increasing")
        # The run from the whole time axis, before any empty steps at its end are left out.
        base = times[-1] - np.timedelta64(240, "h")
        bases.add(str(base))
        keep = filled_steps(ds["VHM0"].notnull().any(dim=("latitude", "longitude")).values, region)
        for pid, lat, lon in box[4]:
            hs, tp, dr = (at(ds[k], lat, lon).values for k in ("VHM0", "VTPK", "VMDR"))
            series = []
            for t, h, p, d in list(zip(times, hs, tp, dr))[:keep]:
                if not (HS_BOUNDS[0] <= float(h) <= HS_BOUNDS[1] and TP_BOUNDS[0] <= float(p) <= TP_BOUNDS[1] and 0 <= float(d) <= 360):
                    raise ValueError(f"{pid} {t}: Hs {h}, Tp {p}, dir {d} outside bounds")
                series.append({"time": f"{t}Z", "hs": r(h), "tp": r(p, 1), "dirFrom": r(d, 0), "kind": "forecast" if t >= base else "analysis"})
            out[pid] = {"cell": {"lat": lat, "lon": lon}, "series": series}
    if len(bases) != 1:
        raise ValueError(f"regions disagree on the run: {sorted(bases)}")
    return {"dataset": WAVES, "variables": {"hs": "significant wave height VHM0 (m)", "tp": "peak period VTPK (s)", "dirFrom": "mean direction the waves come from, VMDR (degrees from north)"}, "runBase": bases.pop() + "Z", "places": out}


# The globe's daily fields (D20): the whole ocean's daily mean at the first model level, every day of the run, as
# 16-bit values in steps of 0.02 degC packed into two 8-bit channels of a lossless WebP (browsers decode PNG and WebP
# to 8 bits, so a single 16-bit channel would lose its low byte). Grid-registered: column j is longitude -180 + j/12,
# row i is latitude 90 - i/12 (2161 rows, 90 N to 90 S; the model stops at 80 S, below that is no data).
GLOBE_STEP, GLOBE_OFFSET, GLOBE_NODATA = 0.02, -2.5, 65535


def pack(a, sea, offset, step, path):
    """A north-first field on the 1/12 degree grid into the 16-bit, two-channel lossless WebP the globe decodes."""
    from PIL import Image

    v = np.full((2161, 4320), GLOBE_NODATA, dtype=np.uint16)
    v[: a.shape[0]] = np.where(sea, np.round((a - offset) / step), GLOBE_NODATA).astype(np.uint16)
    rgb = np.stack([v >> 8, v & 255, np.zeros_like(v)], -1).astype(np.uint8)
    Image.fromarray(rgb).save(path, "WEBP", lossless=True, method=4)


# The coastal towns the globe and My places search (scripts/data/places/towns.py): their sea and wave cells, read from
# the daily fields while those are in memory, so a town's card needs one small file instead of the whole-ocean images.
TOWNS_DIR = INPUTS / "towns"
TOWN_VALUES = {"sst": None, "hs": None}


_TOWN_CELLS = None


def town_cells():
    """{shard: (geonameids, sst rows, sst cols, wave mask, wave rows, wave cols)} as numpy arrays, rows from the north as
    `a`, read once."""
    global _TOWN_CELLS
    if _TOWN_CELLS is not None:
        return _TOWN_CELLS
    out = {}
    for f in sorted(TOWNS_DIR.glob("*.json")) if TOWNS_DIR.exists() else []:
        if f.stem in ("admin1", "manifest", "near"):
            continue
        rows = json.loads(f.read_text())
        lat = np.array([x[4] for x in rows]); lon = np.array([x[5] for x in rows])
        wl = np.array([x[7] if x[7] is not None else np.nan for x in rows]); wn = np.array([x[8] if x[8] is not None else np.nan for x in rows])
        has_w = np.isfinite(wl)
        row = lambda la: np.rint((90 - la) * 12).astype(int)  # noqa: E731
        col = lambda lo: np.rint((lo + 180) * 12).astype(int) % 4320  # noqa: E731
        out[f.stem] = ([x[0] for x in rows], row(lat), col(lon), has_w, row(np.where(has_w, wl, 0)), col(np.where(has_w, wn, 0)))
    _TOWN_CELLS = out
    return out


def sample_towns(a, wave=False):
    """This field's values at every town's cell, rounded to 0.1, per shard (None where the cell has no value)."""
    out = {}
    for shard, (_, si, sj, has_w, wi, wj) in town_cells().items():
        v = a[wi, wj] if wave else a[si, sj]
        out[shard] = [None if (wave and not h) or not np.isfinite(x) else round(float(x), 1) for x, h in zip(v, has_w)]
    return out


def swap_in(folder, prefix, staged):
    """Replace the folder's <prefix>-*.webp with the staged files, only once every day of the run has passed its checks:
    a run that fails keeps the previous files, which the previous conditions.json (kept for a failed product) lists."""
    folder.mkdir(parents=True, exist_ok=True)
    for f in folder.glob(f"{prefix}-*.webp"):
        f.unlink()
    for f in staged.glob(f"{prefix}-*.webp"):
        f.rename(folder / f.name)
    staged.rmdir()


def globe():
    start = (NOW - dt.timedelta(days=DAYS_BACK + 1)).strftime("%Y-%m-%d")
    end = (NOW + dt.timedelta(days=12)).strftime("%Y-%m-%d")
    da = cm.open_dataset(dataset_id=OCEAN, variables=["thetao"], minimum_depth=0, maximum_depth=1, start_datetime=start, end_datetime=end)["thetao"].isel(depth=0)
    dates = [str(t)[:10] for t in da["time"].values]
    if dates != sorted(set(dates)):
        raise ValueError("globe: valid dates not strictly increasing")
    lats, lons = da["latitude"].values, da["longitude"].values
    if len(lons) != 4320 or abs(lons[0] + 180) > 1e-4 or abs(lats[-1] - 90) > 1e-4 or abs(lats[1] - lats[0] - 1 / 12) > 1e-4:  # float32 coordinates
        raise ValueError(f"globe: unexpected grid {lats[0]}..{lats[-1]} x {lons[0]}..{lons[-1]}")
    bulletin = dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=9)
    folder = OUT / "globe"
    staged = OUT / ".globe-sst"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    days = []
    sst_days = []
    for k, d in enumerate(dates):
        day = dt.date.fromisoformat(d)
        if day < bulletin - dt.timedelta(days=DAYS_BACK):
            continue
        a = da.isel(time=k).load().values[::-1]  # north first
        sea = np.isfinite(a)
        if sea.sum() < 0.5 * a.size or np.nanmin(a) < SST_BOUNDS[0] or np.nanmax(a) > SST_BOUNDS[1]:
            raise ValueError(f"globe {d}: {sea.sum()} sea cells, range {np.nanmin(a)}..{np.nanmax(a)}")
        name = f"sst-{d}.webp"
        pack(a, sea, GLOBE_OFFSET, GLOBE_STEP, staged / name)
        kind = ocean_kind(day, bulletin)
        days.append({"date": d, "kind": kind, "file": f"globe/{name}"})
        sst_days.append((d, kind, sample_towns(a)))
    swap_in(folder, "sst", staged)
    TOWN_VALUES["sst"] = {"dates": [x[0] for x in sst_days], "kinds": [x[1] for x in sst_days], "runBase": f"{bulletin.isoformat()}T00:00Z", "fields": [x[2] for x in sst_days]}
    return {
        "dataset": OCEAN,
        "variable": "thetao, first model level (0.49 m), daily mean",
        "bulletinDate": bulletin.isoformat(),
        "runBase": f"{bulletin.isoformat()}T00:00Z",
        "encoding": {"offset": GLOBE_OFFSET, "step": GLOBE_STEP, "nodata": GLOBE_NODATA, "packing": "R high byte, G low byte", "width": 4320, "height": 2161, "grid": "column j: lon -180 + j/12; row i: lat 90 - i/12"},
        "days": days,
    }


# The globe's waves layer: significant wave height over the whole ocean once a day. The product is instantaneous
# every 3 hours, so each day shows its 12:00 UTC field (labelled as such, never as a daily mean), from the analysis
# day before the run to the end of the forecast. Same grid and packing as the sea temperature, in steps of 1 cm.
WAVE_STEP, WAVE_OFFSET = 0.01, 0.0


def globe_waves():
    start = (NOW - dt.timedelta(days=DAYS_BACK + 1)).strftime("%Y-%m-%dT00:00")
    end = (NOW + dt.timedelta(days=12)).strftime("%Y-%m-%dT00:00")
    da = cm.open_dataset(dataset_id=WAVES, variables=["VHM0"], start_datetime=start, end_datetime=end)["VHM0"]
    times = [np.datetime64(t, "s") for t in da["time"].values]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("globe waves: valid times not strictly increasing")
    lats, lons = da["latitude"].values, da["longitude"].values
    if len(lons) != 4320 or abs(lons[0] + 180) > 1e-4 or abs(lats[-1] - 90) > 1e-4 or abs(lats[1] - lats[0] - 1 / 12) > 1e-4:
        raise ValueError(f"globe waves: unexpected grid {lats[0]}..{lats[-1]} x {lons[0]}..{lons[-1]}")
    base = times[-1] - np.timedelta64(240, "h")
    folder = OUT / "globe"
    staged = OUT / ".globe-hs"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    first = (base - np.timedelta64(DAYS_BACK, "D")).astype("datetime64[D]")
    days = []
    hs_days = []
    empty = []
    for k, t in enumerate(times):
        if str(t)[11:] != "12:00:00" or t.astype("datetime64[D]") < first:
            continue
        d = str(t)[:10]
        a = da.isel(time=k).load().values[::-1]
        sea = np.isfinite(a)
        if not sea.any():
            # An empty field: allowed only at the end of the run (filled_steps), and left out.
            empty.append(d)
            continue
        if empty:
            raise ValueError(f"globe waves: empty field on {empty[0]} before a filled one on {d}")
        if sea.sum() < 0.5 * a.size or np.nanmin(a) < HS_BOUNDS[0] or np.nanmax(a) > HS_BOUNDS[1]:
            raise ValueError(f"globe waves {d}: {sea.sum()} sea cells, range {np.nanmin(a)}..{np.nanmax(a)}")
        name = f"hs-{d}.webp"
        pack(a, sea, WAVE_OFFSET, WAVE_STEP, staged / name)
        days.append({"date": d, "time": f"{t}Z", "kind": "forecast" if t >= base else "analysis", "file": f"globe/{name}"})
        hs_days.append((d, f"{t}Z", days[-1]["kind"], sample_towns(a, wave=True)))
    if not days:
        raise ValueError("globe waves: no 12:00 UTC fields")
    if empty:
        print(f"globe waves: left out the empty field of {', '.join(empty)} at the end of the run", file=sys.stderr)
    swap_in(folder, "hs", staged)
    TOWN_VALUES["hs"] = {"dates": [x[0] for x in hs_days], "times": [x[1] for x in hs_days], "kinds": [x[2] for x in hs_days], "runBase": f"{base}Z", "fields": [x[3] for x in hs_days]}
    return {
        "dataset": WAVES,
        "variable": "significant wave height VHM0 (m), instantaneous at 12:00 UTC",
        "runBase": f"{base}Z",
        "encoding": {"offset": WAVE_OFFSET, "step": WAVE_STEP, "nodata": GLOBE_NODATA, "packing": "R high byte, G low byte", "width": 4320, "height": 2161, "grid": "column j: lon -180 + j/12; row i: lat 90 - i/12"},
        "days": days,
        # Empty fields at the end of the run, left out (the forecast ends earlier).
        **({"leftOut": empty} if empty else {}),
    }

def towns():
    """Each coastal town's daily sea temperature and 12:00 UTC wave height at its cells, from this run's fields, in one
    file per shard of the town index (live/towns/<letter>.json). A town with no wave cell has no wave values. Both
    fields must have passed their checks in this run, or the previous files are kept."""
    sst, hs = TOWN_VALUES["sst"], TOWN_VALUES["hs"]
    if sst is None or hs is None:
        raise ValueError("the sea temperature or wave fields did not pass this run")
    cells = town_cells()
    if not cells:
        raise ValueError(f"no town index under {TOWNS_DIR}")
    folder = OUT / "towns"
    staged = OUT / ".towns"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    n = 0
    for shard, (gids, *_rest) in cells.items():
        v = {}
        for k, gid in enumerate(gids):
            s_ = [f[shard][k] for f in sst["fields"]]
            h_ = [f[shard][k] for f in hs["fields"]]
            if all(x is None for x in s_):
                continue
            v[str(gid)] = [s_, None if all(x is None for x in h_) else h_]
        (staged / f"{shard}.json").write_text(json.dumps({"v": v}, separators=(",", ":")) + "\n")
        n += len(v)
    if n < 0.9 * sum(len(x[0]) for x in cells.values()):
        raise ValueError(f"only {n} towns have a value")
    shutil.rmtree(folder, ignore_errors=True)
    staged.rename(folder)
    meta = {k: {kk: vv for kk, vv in d.items() if kk != "fields"} for k, d in (("sst", sst), ("hs", hs))}
    return {"towns": n, "dir": "towns/", **meta, "units": {"sst": "degC", "hs": "m"}}


MET = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# MET Norway's terms ask every client to identify itself with its site and a contact (docs: TermsOfService). This is
# the site's own address from src/lib/site.ts, never a person's.
MET_UA = "hydrotempo.com hello@hydrotempo.com"
try:  # python.org builds on macOS ship without the system certificates; certifi comes with the toolbox's dependencies
    import certifi

    TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    TLS = ssl.create_default_context()
AIR_BOUNDS, WIND_BOUNDS = (-60.0, 60.0), (0.0, 80.0)
WEATHER_HOURS = 48


def met_get(req, tries=4):
    """One request, retried with backoff on a server error (5xx), a timeout or a cut-off body, never on a client error."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=TLS) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ValueError, http.client.IncompleteRead):
            # A cut-off or malformed body is retried like a timeout.
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


def weather():
    """Air temperature and wind near each coast from MET Norway's Locationforecast (F07, implementation plan 1.4 rule 5):
    context for the water values, never mixed with them. The town's public coordinates (4 decimals, as the API asks),
    one request per place, gently spaced. Every value is a forecast from the model run MET Norway names."""
    out, updated, missing = {}, set(), {}
    for p in registry["places"]:
        if "none" in p["samples"]["ocean"]:
            continue
        req = urllib.request.Request(f"{MET}?lat={p['lat']:.4f}&lon={p['lon']:.4f}", headers={"User-Agent": MET_UA})
        try:
            d = met_get(req)
        except Exception as e:  # noqa: BLE001 - one place's bad answer leaves that place without weather this run
            missing[p["id"]] = str(e)[:120]
            continue
        updated.add(d["properties"]["meta"]["updated_at"])
        series = []
        start, end = NOW - dt.timedelta(hours=1), NOW + dt.timedelta(hours=WEATHER_HOURS)
        for ts in d["properties"]["timeseries"]:
            t = dt.datetime.fromisoformat(ts["time"].replace("Z", "+00:00")).replace(tzinfo=None)
            if not (start <= t <= end):
                continue
            x = ts["data"]["instant"]["details"]
            a, w, wd = x.get("air_temperature"), x.get("wind_speed"), x.get("wind_from_direction")
            if a is None or not (AIR_BOUNDS[0] <= a <= AIR_BOUNDS[1]) or w is None or not (WIND_BOUNDS[0] <= w <= WIND_BOUNDS[1]) or (wd is not None and not 0 <= wd <= 360):
                raise ValueError(f"{p['id']} {ts['time']}: air {a}, wind {w}, dir {wd} outside bounds")
            series.append({"time": t.isoformat(timespec="minutes") + "Z", "air": r(a, 1), "wind": r(w, 1), "windFrom": None if wd is None else r(wd, 0), "kind": "forecast"})
        if not series:
            raise ValueError(f"{p['id']}: no forecast times in the next {WEATHER_HOURS} hours")
        out[p["id"]] = {"series": series, "updatedAt": d["properties"]["meta"]["updated_at"]}
        time.sleep(0.2)
    # A few bad answers (MET Norway has served cut-off bodies for single places) cost those places their weather; more
    # than a fifth means something is wrong with the service or this job, and the product fails.
    if len(missing) > 0.2 * (len(out) + len(missing)):
        raise ValueError(f"{len(missing)} places failed: {missing}")
    return {"provider": "MET Norway Locationforecast 2.0 (compact)", "missing": missing, "variables": {"air": "air temperature at 2 m (degC)", "wind": "wind speed at 10 m (m/s)", "windFrom": "direction the wind comes from (degrees from north)"}, "runBase": max(updated), "places": out}


STATIONS = json.loads((INPUTS / "stations.json").read_text())["stations"]
STATION_DAYS = 60
USGS_API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items"
EA_API = "https://environment.data.gov.uk/hydrology/id/measures"
DATA_UA = "hydrotempo-data (https://github.com/LimpidMaja/hydrotempo-data)"
# Discharge bounds per unit: never negative, and below the largest flows ever measured (the Amazon, about 340,000 m3/s).
Q_BOUNDS = {"ft3/s": (0.0, 1.3e7), "m3/s": (0.0, 3.7e5)}


def stations():
    """Daily mean discharge at the curated gauge stations (phase 8 step 7), observed values with each provider's own
    status: USGS approval status (Provisional or Approved) and qualifiers, and the Environment Agency's quality flag
    (Good, Unchecked, Estimated, Suspect and so on) and completeness. Units as the provider publishes them. A station
    that fails this run keeps no values; more than a quarter failing fails the product."""
    since = (NOW - dt.timedelta(days=STATION_DAYS)).date().isoformat()
    out, missing = {}, {}
    for st in STATIONS:
        s = st["series"]
        try:
            if st["provider"] == "usgs":
                url = f"{USGS_API}?f=json&monitoring_location_id={st['providerId']}&parameter_code={s['parameter']}&statistic_id={s['statistic']}&time={since}/..&limit=200&sortby=time"
                rows = [f["properties"] for f in met_get(urllib.request.Request(url, headers={"User-Agent": DATA_UA}))["features"]]
                series = []
                for x in rows:
                    # USGS writes cubic feet per second as "ft^3/s"; the registry's unit is "ft3/s".
                    if x["unit_of_measure"].replace("^", "") != s["unit"]:
                        raise ValueError(f"unit {x['unit_of_measure']} instead of {s['unit']}")
                    v = None if x["value"] in (None, "") else float(x["value"])
                    series.append({"date": x["time"], "value": v, "status": x["approval_status"], "qualifier": x.get("qualifier")})
            else:
                url = f"{EA_API}/{s['measure']}/readings.json?mineq-date={since}&_limit=200&_sort=date"
                rows = met_get(urllib.request.Request(url, headers={"User-Agent": DATA_UA}))["items"]
                series = [{"date": x["date"], "value": x.get("value"), "status": x.get("quality"), "completeness": x.get("completeness")} for x in rows]
            lo, hi = Q_BOUNDS[s["unit"]]
            dates = [x["date"] for x in series]
            if dates != sorted(set(dates)):
                raise ValueError("dates not strictly increasing")
            for x in series:
                if x["value"] is not None and not (math.isfinite(x["value"]) and lo <= x["value"] <= hi):
                    raise ValueError(f"{x['date']}: {x['value']} {s['unit']} outside bounds")
            if not series:
                raise ValueError(f"no values since {since}")
            out[st["id"]] = {"unit": s["unit"], "kind": "observed", "series": series}
        except Exception as e:  # noqa: BLE001 - one station's bad answer leaves that station without values this run
            missing[st["id"]] = str(e)[:160]
        time.sleep(0.3)
    if len(missing) > 0.25 * len(STATIONS):
        raise ValueError(f"{len(missing)} stations failed: {missing}")
    return {"providers": {"usgs": "USGS Water Data OGC API, daily values", "ea": "Environment Agency Hydrology API"}, "variable": "discharge, daily mean", "missing": missing, "stations": out}


def lakes():
    # Recent lake surface temperature (F25), from the Copernicus Data Space: a new 10-day composite about every 10 days,
    # otherwise the previous values unchanged (scripts/data/lakes/recent.py).
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "lakes"))
    import recent  # noqa: E402

    return recent.recent(prev.get("products", {}).get("lakes"))


# The previous run's file (restored from the last published data by the workflow): a failed product keeps its last good values,
# with their own run and valid times, so a page can say how old they are (never "updated" by a failed retry).
prev_file = OUT / "conditions.json"
prev = json.loads(prev_file.read_text()) if prev_file.exists() else {"products": {}}
result = {"retrievedAt": NOW.isoformat(timespec="seconds") + "Z", "products": {}, "status": {}}
for name, fn in (("ocean", ocean), ("waves", waves), ("weather", weather), ("stations", stations), ("globe", globe), ("globeWaves", globe_waves), ("towns", towns), ("lakes", lakes)):
    try:
        result["products"][name] = fn()
        result["status"][name] = "ok"
    except Exception as e:  # noqa: BLE001 - one failed product must not lose the other
        print(f"{name} failed: {e}", file=sys.stderr)
        if name in prev.get("products", {}):
            result["products"][name] = prev["products"][name]
            result["status"][name] = f"failed, kept the previous values: {e}"
        else:
            result["status"][name] = f"failed: {e}"
OUT.mkdir(parents=True, exist_ok=True)
# A failed globe leaves its half-written staging folder: never publish it.
for staged in [*OUT.glob(".globe-*"), OUT / ".towns"]:
    shutil.rmtree(staged, ignore_errors=True)
(OUT / "conditions.json").write_text(json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n")
print(json.dumps(result["status"]), f"{(OUT / 'conditions.json').stat().st_size} bytes")
sys.exit(0 if all(v == "ok" for v in result["status"].values()) else 1)
