"""Recent lake surface temperature (F25, implementation plan phase 8 step 3): the newest 10-day composite of the CLMS
lake surface water temperature (near real time, global, 1 km, version 1, Sentinel-3 SLSTR), read inside each curated
lake's HydroLAKES outline (inputs/lakes.json). Called by the daily job (scripts/fetch.py) as its "lakes" product. It downloads
only when a newer composite is out (one every 10 days, about three days after its window ends), otherwise it keeps the
previous values as they were.

Rules (decision of 6 November 2026 in docs/DECISIONS.md):
- Only pixels inside the lake's outline whose quality level is "acceptable" or "best" (4 and 5 in the product's
  quality_level flags, "no_data bad_data worst_quality low_quality acceptable_quality best_quality").
- A lake gets a value when at least MIN_PIXELS such pixels cover at least MIN_SHARE of the lake's pixels: a few clear
  pixels at one end of a cloudy lake do not speak for the lake.
- The value is the median of those pixels, with the 10th and 90th percentiles as the spread across the lake, the median
  of the product's own uncertainty, and the number of days in the window with at least one clear view.
- Skin temperature in kelvin, converted to degrees C. Never compared with LakeTEMP's monthly averages: different
  satellites (Landsat 8 against Sentinel-3), times of day and sampling (one central point against the whole lake).

Usage, on its own: python -I scripts/lakes/recent.py [out.json]   (needs the CDSE login, see cdse.py)
"""

import json
import pathlib
import sys
import tempfile
import zipfile

import numpy as np
import shapely
import xarray as xr
from shapely.geometry import shape

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cdse  # noqa: E402

CACHE = pathlib.Path(".cache/clms-lswt")
NAME, SUFFIX = "c_gls_LSWT_", "_nc"
DATASET = "CLMS lake surface water temperature, near real time, global, 1 km, 10-daily, version 1"
MIN_PIXELS = 10
MIN_SHARE = 0.2
BOUNDS = (-2.0, 40.0)


def sample(nc: pathlib.Path, lakes: list[dict]) -> tuple[dict, dict]:
    d = xr.open_dataset(nc, mask_and_scale=True)
    lat, lon = d["lat"].values, d["lon"].values
    day_bits = int(np.sum(d["time_obs"].attrs["flag_masks"]))
    out, none = {}, {}
    for lake in lakes:
        g = shape(lake["outline"])
        x0, y0, x1, y1 = g.bounds
        sub = d.isel(time=0, lat=slice(np.searchsorted(lat, y0), np.searchsorted(lat, y1) + 1), lon=slice(np.searchsorted(lon, x0), np.searchsorted(lon, x1) + 1))
        la, lo = np.meshgrid(sub["lat"].values, sub["lon"].values, indexing="ij")
        inside = shapely.contains_xy(g, lo, la)
        t = sub["lake_surface_water_temperature"].values
        good = inside & np.isfinite(t) & (sub["quality_level"].values >= 4)
        n, total = int(good.sum()), int(inside.sum())
        if total == 0 or n < MIN_PIXELS or n < MIN_SHARE * total:
            # Small lakes fall between the 1 km pixels or are not among the product's 2,000 lakes; large ones were
            # under cloud or ice for most of the window.
            none[lake["id"]] = (
                "too small for the 1 km grid" if total < MIN_PIXELS
                # Not one pixel with a value: under cloud all window, or not among the lakes the product covers (one
                # file cannot tell the two apart).
                else "no pixel with a value" if not (inside & np.isfinite(t)).any()
                else "too few clear pixels in this window"
            )
            continue
        v = t[good] - 273.15
        med = float(np.median(v))
        if not BOUNDS[0] <= med <= BOUNDS[1]:
            raise ValueError(f"{lake['id']}: {med:.2f} degC is outside {BOUNDS}")
        # Only the day flags the file documents (flag_masks 1 to 1024, obs_day1 to obs_day11). Some pixels also carry an
        # undocumented 2048, which is not counted.
        days = np.bitwise_or.reduce(np.nan_to_num(sub["time_obs"].values[good], nan=0).astype(np.int32)) & day_bits
        out[lake["id"]] = {
            "value": round(med, 1),
            "p10": round(float(np.percentile(v, 10)), 1),
            "p90": round(float(np.percentile(v, 90)), 1),
            "uncertainty": round(float(np.nanmedian(sub["lswt_uncertainty"].values[good])), 2),
            "pixels": n,
            "share": round(n / total, 2),
            "days": int(bin(int(days)).count("1")),
        }
    return out, none


def recent(prev: dict | None = None) -> dict:
    newest = cdse.search(NAME, SUFFIX, 1)[0]
    lakes = json.loads((pathlib.Path(__file__).resolve().parents[2] / "inputs/lakes.json").read_text())["lakes"]
    # The same composite is kept as it was, unless lakes were added since: every lake needs a value or a reason.
    if prev and prev.get("product") == newest["Name"] and {l["id"] for l in lakes} <= set(prev.get("lakes", {})) | set(prev.get("none", {})):
        return prev
    z = cdse.download(newest, CACHE / f"{newest['Name']}.zip")
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(z) as zf:
        member = next(m for m in zf.namelist() if m.endswith(".nc"))
        nc = pathlib.Path(zf.extract(member, tmp))
        attrs = xr.open_dataset(nc).attrs
        values, none = sample(nc, lakes)
    if len(values) < 0.3 * len(lakes):
        raise ValueError(f"only {len(values)} of {len(lakes)} lakes have a value: {none}")
    start, end = attrs["time_coverage_start"], attrs["time_coverage_end"]
    return {
        "dataset": DATASET,
        "product": newest["Name"],
        "version": attrs.get("product_version"),
        # The window's first and last day (the composite's time_coverage_end is the last day at 00 UTC).
        "window": {"start": f"{start[:4]}-{start[4:6]}-{start[6:8]}", "end": f"{end[:4]}-{end[4:6]}-{end[6:8]}"},
        "published": newest["PublicationDate"][:19] + "Z",
        "unit": "degC",
        "kind": "satellite",
        "lakes": values,
        "none": none,
    }


if __name__ == "__main__":
    res = recent()
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = json.dumps(res, ensure_ascii=False, indent=1)
    out.write_text(text + "\n") if out else print(text)
