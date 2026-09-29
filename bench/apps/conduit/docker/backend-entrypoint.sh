#!/bin/sh
# Start from the pristine seeded database on every container start (reset = restart).
set -eu
bench-flags >/dev/null # refuse to start with an invalid BENCH_FLAGS (ADR-0022)
mkdir -p /data
cp /app/fixtures/seed.db /data/conduit.db
exec bun /app/.output/server/index.mjs
