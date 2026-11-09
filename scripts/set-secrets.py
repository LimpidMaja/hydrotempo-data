#!/usr/bin/env python3
"""Copies the four logins the daily job needs from this Mac into the repository's `github-pages` environment secrets,
so they are never typed or shown: the Copernicus Marine login from the file `copernicusmarine login` writes, the
Copernicus Data Space login from the file scripts/lakes/cdse_login.sh writes. Each value goes to `gh secret set` on
its standard input, never on a command line, in a log or on the screen. Run it again after changing a password.

Usage: python3 -I scripts/set-secrets.py    (needs the GitHub CLI, signed in as the repository's owner)
"""

import base64
import binascii
import configparser
import pathlib
import subprocess
import sys

REPO = "LimpidMaja/hydrotempo-data"
ENV = "github-pages"
CMEMS = pathlib.Path.home() / ".copernicusmarine/.copernicusmarine-credentials"
CDSE = pathlib.Path.home() / ".config/hydrotempo/cdse.env"


def cmems_login() -> tuple[str, str]:
    raw = CMEMS.read_text().strip()
    # The toolbox stores the file base64-encoded (an encoding, not encryption); older versions wrote it as plain text.
    try:
        text = base64.b64decode(raw, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        text = raw
    c = configparser.ConfigParser(interpolation=None)
    c.read_string(text)
    return c["credentials"]["username"], c["credentials"]["password"]


def cdse_login() -> tuple[str, str]:
    vals = dict(line.split("=", 1) for line in CDSE.read_text().splitlines() if "=" in line)
    return vals["CDSE_USERNAME"], vals["CDSE_PASSWORD"]


def main():
    missing = [str(p) for p in (CMEMS, CDSE) if not p.exists()]
    if missing:
        sys.exit(f"Not found: {', '.join(missing)}. Run `copernicusmarine login` and scripts/lakes/cdse_login.sh first.")
    (mu, mp), (du, dp) = cmems_login(), cdse_login()
    secrets = {
        "COPERNICUSMARINE_SERVICE_USERNAME": mu,
        "COPERNICUSMARINE_SERVICE_PASSWORD": mp,
        "CDSE_USERNAME": du,
        "CDSE_PASSWORD": dp,
    }
    for name, value in secrets.items():
        if not value:
            sys.exit(f"{name} is empty in the local file, nothing was changed for it")
        r = subprocess.run(["gh", "secret", "set", name, "-R", REPO, "--env", ENV], input=value, text=True, capture_output=True)
        # gh's own messages name the secret, never its value.
        print(f"{name}: {'set' if r.returncode == 0 else 'failed: ' + r.stderr.strip()}")


if __name__ == "__main__":
    main()
