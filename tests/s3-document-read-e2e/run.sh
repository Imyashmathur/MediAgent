#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
test_dir="$root/tests/s3-document-read-e2e"
project="mediagent-s3-read-$(date +%s)-$$"
cleanup() { docker compose -p "$project" -f "$test_dir/compose.yml" down --volumes --remove-orphans; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
docker compose -f "$root/docker-compose.yml" build api db
docker compose -p "$project" -f "$test_dir/compose.yml" up -d
docker compose -p "$project" -f "$test_dir/compose.yml" exec -T api python - < "$test_dir/check.py"
