# Hydrotempo live data

The live water data that [hydrotempo.com](https://hydrotempo.com/) reads, fetched once a day by
[`.github/workflows/data.yml`](.github/workflows/data.yml) and published at
<https://limpidmaja.github.io/hydrotempo-data/>:

- `live/conditions.json`: every product with its dataset, run, valid times and data kind (forecast, simulation,
  analysis, satellite or observed), and each product's status in this run.
- `live/globe/sst-<date>.webp`, `live/globe/hs-<date>.webp`: the whole ocean's sea temperature (daily mean) and wave
  height (12:00 UTC) on the 1/12 degree grid, as 16-bit values packed into two channels of a lossless WebP
  (the encoding is in `conditions.json`).
- `live/towns/<letter>.json`: each coastal town's daily sea temperature and wave height at its own model cells.
- `live.tar.gz`: all of the above, for the site's daily build.

| Product | Source | Licence and credit |
| --- | --- | --- |
| Sea temperature, places and globe | [Copernicus Marine global ocean physics analysis and forecast](https://doi.org/10.48670/moi-00016) | [Copernicus Marine Service licence](https://marine.copernicus.eu/user-corner/service-commitments-and-licence). Generated using E.U. Copernicus Marine Service Information |
| Waves, places and globe | [Copernicus Marine global ocean waves analysis and forecast](https://doi.org/10.48670/moi-00017) | As above |
| Air temperature and wind near each coast | [MET Norway Locationforecast 2.0](https://api.met.no/weatherapi/locationforecast/2.0/documentation) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Weather forecast from MET Norway |
| River flow, US gauges | [USGS Water Data OGC API](https://api.waterdata.usgs.gov/ogcapi/v0/) | U.S. public domain. Credit: U.S. Geological Survey |
| River flow, England | [Environment Agency Hydrology API](https://environment.data.gov.uk/hydrology/) | Contains public sector information licensed under the [Open Government Licence v3.0](http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). Environment Agency |
| Recent lake surface temperature | [CLMS lake surface water temperature, near real time, 1 km](https://land.copernicus.eu/en/products/water-bodies/lake-surface-water-temperature-near-real-time-v1-0-1km), via the [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) | [Copernicus data policy](https://land.copernicus.eu/en/data-policy). Generated using European Union's Copernicus Land Monitoring Service information |
| Town list in `inputs/towns/` | [GeoNames](https://www.geonames.org/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

How the job behaves:

- Every value is checked (finite, within physical bounds, valid times increasing, the run recent). A product that
  fails keeps its last good values with their own run and valid times, the run's status says so, and an issue is
  opened.
- Data kinds come from the products' documented run schedules, worked out from the data, never from the clock.
- An empty step at the very end of a wave run is left out (the forecast ends earlier), never filled.
- The places, gauges, lakes and towns to sample are the site's registries, copied into `inputs/` from the site's
  repository by its `scripts/dev/sync-data-inputs.mjs`.

Setting the logins: `python3 -I scripts/set-secrets.py` copies them from this Mac (the files `copernicusmarine login`
and `scripts/lakes/cdse_login.sh` write) into the `github-pages` environment's secrets, without showing them.

Safety: the job runs only on its schedule or by hand, never on pull requests, so code from a fork never runs with the
logins. The logins are repository secrets given only to the fetch step, and the scripts never print them.

Run locally: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then
`.venv/bin/python -I scripts/fetch.py live` with `COPERNICUSMARINE_SERVICE_USERNAME`, `COPERNICUSMARINE_SERVICE_PASSWORD`
(or a `copernicusmarine login`), `CDSE_USERNAME` and `CDSE_PASSWORD` set.
