#!/usr/bin/env bash
# Scripted walkthrough for the video (mock LLM, zero cost). Run from the repo root: make demo
# Each step pauses so you can narrate; press Enter to continue.
set -euo pipefail

API=${API_URL:-http://localhost:8000}
LAB=${LAB_URL:-http://localhost:8100}
ADMIN="Authorization: Bearer ${ADMIN_TOKEN:-replace-with-long-random-admin-token}"
RUN="docker compose run --rm -T tests"
PSQL="docker compose exec -T postgres psql -U ${POSTGRES_USER:-app} -d ${POSTGRES_DB:-gateway} -P pager=off -c"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$1"; read -r -p "(enter) " _ || true; }

step "1. Stack is up: api, 2 workers, mocklab, postgres"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
curl -s -X POST -H "$ADMIN" "$API/admin/reset-mock-data" >/dev/null
curl -s -X POST -H "$ADMIN" "$LAB/admin/reset" >/dev/null

step "2. Signed webhook, then the SAME message id again (Meta retry)"
$RUN python -m scripts.wa --api http://api:8000 --phone 919999950001 --id wamid.demo.1 "Hi, I want a CBC test tomorrow"
$RUN python -m scripts.wa --api http://api:8000 --phone 919999950001 --id wamid.demo.1 "Hi, I want a CBC test tomorrow"
sleep 4
$PSQL "SELECT wa_message_id, status FROM inbound_messages; SELECT count(*) AS duplicates_dropped FROM duplicate_deliveries;"

step "3. Forged signature is rejected before parsing"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$API/webhook" -H 'X-Hub-Signature-256: sha256=deadbeef' -d '{}'

step "4. Book a slot; flaky lab shows retries and reconciliation"
$RUN python -m scripts.wa --api http://api:8000 --phone 919999950001 "Slot 2, my name is Asha Rao"
sleep 8
$PSQL "SELECT body FROM outbound_messages ORDER BY id;"
$PSQL "SELECT endpoint, attempt, outcome, latency_ms FROM lab_calls ORDER BY id;"

step "5. Kill a worker mid-flight: lease expires, the other worker finishes the job"
for i in $(seq 1 6); do
  $RUN python -m scripts.wa --api http://api:8000 --phone "9199999600$i" "Book a LIPID test tomorrow" >/dev/null &
done
wait
docker compose kill worker >/dev/null 2>&1 || true   # both replicas die hard (no graceful shutdown)
$PSQL "SELECT count(*) FILTER (WHERE lease_token IS NOT NULL) AS leased, count(*) FILTER (WHERE has_pending) AS pending FROM conversations;"
docker compose up -d worker >/dev/null
echo "waiting for leases (60s visibility timeout) to expire and work to resume..."
sleep 70
$PSQL "SELECT status, count(*) FROM inbound_messages GROUP BY 1;"

step "6. Lab outage: circuit opens, conversations are handed to humans"
curl -s -X POST -H "$ADMIN" -H 'Content-Type: application/json' "$LAB/admin/chaos" -d '{"failure_rate":1.0}'; echo
for i in 1 2 3; do $RUN python -m scripts.wa --api http://api:8000 --phone "9199999700$i" "CBC test tomorrow" >/dev/null; done
sleep 25
$PSQL "SELECT kind, count(*) FROM events GROUP BY 1; SELECT reason, count(*) FROM handoffs GROUP BY 1;"
curl -s -X POST -H "$ADMIN" -H 'Content-Type: application/json' "$LAB/admin/chaos" -d '{"failure_rate":0.2}'; echo
echo "lab restored; waiting 35s for the workers' circuit breakers to cool down (CB_OPEN_SECONDS=30)..."
sleep 35

step "7. 1,000-message load test with duplicates and bursts (writes LOAD_REPORT.md)"
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose --profile load run --rm loadtest

step "8. /metrics (per-conversation tokens and cost, latency, retries, dedupe, rate limits)"
curl -s "$API/metrics" | grep -E '^(webhook_ack_recent|dedupe_hits|rate_limited|queue_depth|lab_retries|double_bookings|llm_cost_inr_total|handoffs_total|bookings_total)' | head -30 || true
curl -s "$API/metrics" | grep '^conversation_llm_cost_inr' | head -5 || true

step "Done. Open COST.md for the 1M-messages/month estimate."
