#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
test_dir="$root/tests/s3-storage-e2e"
project="mediagent-s3-test-$(date +%s)-$$"
cleanup() {
  docker compose -p "$project" -f "$test_dir/compose.yml" down --volumes --remove-orphans
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
docker compose -f "$root/docker-compose.yml" build api
docker compose -p "$project" -f "$test_dir/compose.yml" up -d
docker compose -p "$project" -f "$test_dir/compose.yml" exec -T test python - < "$test_dir/check.py"
