#!/usr/bin/env bash
# Saves the owner's Copernicus Data Space Ecosystem login for the lake temperature job (F25, CLMS lake surface water
# temperature) in ~/.config/hydrotempo/cdse.env, readable only by this user, and checks it by asking CDSE for an
# access token. Run it in your own terminal, not through an assistant: the password is typed without being shown and
# never leaves this machine except to CDSE's own sign-in server.
#
# Usage: bash scripts/lakes/cdse_login.sh
set -euo pipefail

DIR="$HOME/.config/hydrotempo"
FILE="$DIR/cdse.env"
TOKEN_URL="https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

umask 077
mkdir -p "$DIR"

if [[ -f "$FILE" ]]; then
  read -r -p "$FILE already exists. Replace it? [y/N] " yn
  [[ "$yn" == [yY]* ]] || { echo "Left as it was."; exit 0; }
fi

read -r -p "CDSE login (email): " user
read -r -s -p "CDSE password (not shown): " pass
echo
[[ -n "$user" && -n "$pass" ]] || { echo "Login and password are both needed." >&2; exit 1; }

# Ask for a token. The password goes to curl on its standard input, so it never shows in the process list.
check() {
  printf '%s' "$pass" | curl -s -X POST "$TOKEN_URL" \
    --data-urlencode "client_id=cdse-public" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "username=$user" \
    --data-urlencode "password@-" \
    "$@"
}

resp="$(check)"
if [[ "$resp" != *'"access_token"'* ]]; then
  # With two-factor sign-in on, CDSE also wants the current one-time code.
  echo "CDSE did not accept the login alone: $(printf '%s' "$resp" | sed -n 's/.*"error_description":"\([^"]*\)".*/\1/p')"
  read -r -p "If you use two-factor sign-in, type the current 6-digit code (or press Enter to stop): " totp
  [[ -n "$totp" ]] || { echo "Nothing saved." >&2; exit 1; }
  resp="$(check --data-urlencode "totp=$totp")"
  if [[ "$resp" != *'"access_token"'* ]]; then
    echo "CDSE still refused it: $(printf '%s' "$resp" | sed -n 's/.*"error_description":"\([^"]*\)".*/\1/p')" >&2
    echo "Nothing saved. With two-factor sign-in, S3 keys work better: https://eodata-s3keysmanager.dataspace.copernicus.eu" >&2
    exit 1
  fi
  echo "Note: two-factor sign-in is on, so a scheduled job cannot sign in with the password alone. Tell Claude: it will use S3 keys instead."
fi

{
  printf 'CDSE_USERNAME=%s\n' "$user"
  printf 'CDSE_PASSWORD=%s\n' "$pass"
} > "$FILE"
chmod 600 "$FILE"
unset pass

echo "Signed in to CDSE and saved the login in $FILE (readable only by you)."
