#!/usr/bin/env bash
# ============================================================================
#  Authorize.net API test
#
#  Tests the /an gateway end to end -- either through the running server
#  (HTTP) or straight against the Python checker (direct).
#
#  Usage:
#    ./test_authnet_api.sh                      # direct python test
#    ./test_authnet_api.sh http://host:5000     # through the API
#    ./test_authnet_api.sh http://host:5000 cookies.txt
# ============================================================================
set -u

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${1:-}"
COOKIE_FILE="${2:-}"

if [ -n "$BASE_URL" ]; then
  echo "=== Testing API at $BASE_URL ==="
  echo ""

  for CARD in \
    "4111111111111111|12|29|123" \
    "4111111111111112|12|29|123" \
    "5500000000000004|01|20|123"
  do
    printf "  %-30s -> " "$CARD"
    if [ -n "$COOKIE_FILE" ]; then
      RESP=$(curl -s -X POST "$BASE_URL/api/authnet/check" \
        -H "Content-Type: application/json" \
        -b "$COOKIE_FILE" \
        -d "{\"card\":\"$CARD\"}" \
        -w "\n%{http_code}" --max-time 130)
    else
      RESP=$(curl -s -X POST "$BASE_URL/api/authnet/check" \
        -H "Content-Type: application/json" \
        -d "{\"card\":\"$CARD\"}" \
        -w "\n%{http_code}" --max-time 130)
    fi
    CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | sed '$d')
    STATUS=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
    MSG=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response','?'))" 2>/dev/null || echo "$BODY")
    echo "HTTP $CODE | $STATUS | $MSG"
  done
  exit 0
fi

echo "=== Testing the checker directly ==="
echo ""
cd "$BOT_DIR" || exit 1

for CARD in \
  "4111111111111111|12|29|123" \
  "4111111111111112|12|29|123" \
  "5500000000000004|01|20|123" \
  "5187251978425315|08|2029|746"
do
  IFS='|' read -r CC MM YY CVV <<< "$CARD"
  printf "  %-30s -> " "$CARD"
  python3 web_authnet_check.py "$CC" "$MM" "$YY" "$CVV" 2>/dev/null | tail -1
done
