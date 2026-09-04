#!/usr/bin/env bash
# Create an owner account + organization on a storeguard cloud instance.
#
# Usage:
#   scripts/create_user.sh <email> <password> <org_name> [full_name]
#
# The target server defaults to http://127.0.0.1:8000; override with:
#   STOREGUARD_URL=https://your-cloud-host scripts/create_user.sh ...
set -euo pipefail

SERVER="${STOREGUARD_URL:-http://127.0.0.1:8000}"

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <email> <password> <org_name> [full_name]" >&2
  echo "  password must be at least 8 characters" >&2
  echo "  server: \$STOREGUARD_URL (default http://127.0.0.1:8000)" >&2
  exit 1
fi

EMAIL="$1"
PASSWORD="$2"
ORG_NAME="$3"
FULL_NAME="${4:-}"

body=$(python3 -c '
import json, sys
email, password, org_name, full_name = sys.argv[1:5]
print(json.dumps({
    "email": email,
    "password": password,
    "org_name": org_name,
    "full_name": full_name,
}))
' "$EMAIL" "$PASSWORD" "$ORG_NAME" "$FULL_NAME")

response=$(curl -sS -w '\n%{http_code}' -X POST "$SERVER/api/auth/signup" \
  -H "Content-Type: application/json" \
  -d "$body")

http_code=$(echo "$response" | tail -n1)
resp_body=$(echo "$response" | sed '$d')

if [ "$http_code" != "201" ]; then
  echo "Signup failed (HTTP $http_code):" >&2
  echo "$resp_body" >&2
  exit 1
fi

echo "$resp_body" | python3 -m json.tool
echo
echo "Created on $SERVER — log in with:"
echo "  email:    $EMAIL"
echo "  password: $PASSWORD"
