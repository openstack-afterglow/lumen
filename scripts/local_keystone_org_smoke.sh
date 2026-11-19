#!/usr/bin/env bash
# Organization-connection route smoke against a running `scripts/local_keystone_smoke.sh up` stack (synthetic Keystone).
# Exits non-zero on the first unexpected status. Contacts public OAuth metadata of one catalog template (GET only).
set -u
API="${LUMEN_SMOKE_API:-http://127.0.0.1:18012}/v1"
KS="${LUMEN_SMOKE_KEYSTONE:-http://127.0.0.1:15000}"
H=(-H "X-Auth-Token: synthetic-owner-token" -H "X-Project-Id: system-project" -H 'content-type: application/json')
fail=0
cfg() { curl -s -o /dev/null -X POST "$KS/_control/configure" -H "X-Test-Control-Token: synthetic-directory-control" -H 'content-type: application/json' -d "$1"; }
expect() { local want="$1" label="$2"; shift 2; local got; got=$(curl -s -o /tmp/org-smoke.json -w "%{http_code}" "${H[@]}" "$@")
  if [ "$got" = "$want" ]; then echo "ok   $label ($got)"; else echo "FAIL $label: want $want got $got"; fail=1; fi; }
jq_() { python3 -c "import json,sys;d=json.load(open('/tmp/org-smoke.json'));print($1)"; }
tpl() { curl -s "${H[@]}" "$API/mcp-settings?access=$1&limit=1" | python3 -c "import sys,json;print(json.load(sys.stdin)['entries'][0]['id'])"; }
cfg '{"owner_roles":["member"],"default_managers":["system-owner"]}'
OPEN=$(tpl open); OA=$(tpl oauth)
expect 201 "manager create org" -X POST "$API/domains/default/mcp-connections" -d "{\"template_id\":\"$OPEN\",\"label\":\"smoke1\"}"; ID1=$(jq_ "d['server']['id']")
expect 200 "manager patch org" -X PATCH "$API/domains/default/mcp-connections/$ID1" -d '{"name":"Smoke org"}'
expect 204 "manager delete org" -X DELETE "$API/domains/default/mcp-connections/$ID1"
expect 201 "manager create oauth org" -X POST "$API/domains/default/mcp-connections" -d "{\"template_id\":\"$OA\",\"label\":\"smoke2\"}"; ID2=$(jq_ "d['server']['id']")
expect 200 "manager static oauth client" -X PATCH "$API/domains/default/mcp-connections/$ID2" -d '{"oauth_client_id":"synthetic-client"}'
expect 200 "manager org oauth/start" -X POST -H 'Origin: http://127.0.0.1:3080' "$API/mcp-servers/$ID2/oauth/start"
cfg '{"default_managers":[]}'
expect 200 "member list org" "$API/domains/default/mcp-connections"
expect 403 "member create org" -X POST "$API/domains/default/mcp-connections" -d "{\"template_id\":\"$OPEN\",\"label\":\"smoke3\"}"
expect 403 "member patch org" -X PATCH "$API/domains/default/mcp-connections/$ID2" -d '{"name":"x"}'
expect 403 "member delete org" -X DELETE "$API/domains/default/mcp-connections/$ID2"
expect 403 "member org oauth/start" -X POST -H 'Origin: http://127.0.0.1:3080' "$API/mcp-servers/$ID2/oauth/start"
expect 403 "member org oauth disconnect" -X DELETE "$API/mcp-servers/$ID2/oauth"
expect 403 "foreign domain list" "$API/domains/other/mcp-connections"
cfg '{"default_managers":["system-owner"]}'
expect 204 "manager org oauth disconnect" -X DELETE "$API/mcp-servers/$ID2/oauth"
expect 204 "manager delete oauth org" -X DELETE "$API/domains/default/mcp-connections/$ID2"
exit $fail
