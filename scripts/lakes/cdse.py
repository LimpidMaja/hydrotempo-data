"""Copernicus Data Space Ecosystem (CDSE): the catalogue search and file download for the CLMS lake surface water
temperature (F25). The login comes from the environment (CDSE_USERNAME, CDSE_PASSWORD: GitHub secrets in Actions) or
from ~/.config/hydrotempo/cdse.env (written by scripts/lakes/cdse_login.sh). Never printed
or logged.

Catalogue: https://documentation.dataspace.copernicus.eu/APIs/OData.html (search needs no login, download does).
"""

import json
import os
import pathlib
import ssl
import urllib.parse
import urllib.request

import certifi

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD = "https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
TOKEN = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ENV_FILE = pathlib.Path.home() / ".config/hydrotempo/cdse.env"
CTX = ssl.create_default_context(cafile=certifi.where())
UA = {"User-Agent": "hydrotempo-data (https://github.com/LimpidMaja/hydrotempo-data)"}


def _login() -> tuple[str, str]:
    user, pw = os.environ.get("CDSE_USERNAME"), os.environ.get("CDSE_PASSWORD")
    if not (user and pw) and ENV_FILE.exists():
        vals = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if "=" in line)
        user, pw = vals.get("CDSE_USERNAME"), vals.get("CDSE_PASSWORD")
    if not (user and pw):
        raise RuntimeError("No CDSE login: run scripts/lakes/cdse_login.sh, or set CDSE_USERNAME and CDSE_PASSWORD")
    return user, pw


def token() -> str:
    user, pw = _login()
    body = urllib.parse.urlencode({"client_id": "cdse-public", "grant_type": "password", "username": user, "password": pw}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN, data=body, headers=UA), context=CTX, timeout=60) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        # The answer names the reason ("Invalid user credentials"), never the login itself.
        raise RuntimeError(f"CDSE refused the login: {json.loads(e.read() or b'{}').get('error_description', e.code)}") from None


def search(name_contains: str, suffix: str, top: int = 20) -> list[dict]:
    """Newest first: Id, Name, ContentLength, ContentDate {Start, End}, PublicationDate."""
    flt = f"Collection/Name eq 'CLMS' and contains(Name,'{name_contains}') and endswith(Name,'{suffix}')"
    q = urllib.parse.urlencode({"$filter": flt, "$orderby": "ContentDate/Start desc", "$top": top})
    with urllib.request.urlopen(urllib.request.Request(f"{CATALOGUE}?{q}", headers=UA), context=CTX, timeout=60) as r:
        return json.load(r)["value"]


def download(product: dict, out: pathlib.Path, tok: str | None = None) -> pathlib.Path:
    """The product as CDSE serves it (a zip of the product folder), kept under its own name."""
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(DOWNLOAD.format(id=product["Id"]), headers={**UA, "Authorization": f"Bearer {tok or token()}"})
    tmp = out.with_suffix(out.suffix + ".part")
    with urllib.request.urlopen(req, context=CTX, timeout=600) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.rename(out)
    return out
