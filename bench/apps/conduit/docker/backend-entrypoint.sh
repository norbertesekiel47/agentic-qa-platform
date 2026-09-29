#!/bin/sh
# Start from the pristine seeded database on every container start (reset = restart).
set -eu
mkdir -p /data
cp /app/seed.db /data/conduit.db
exec bun /app/.output/server/index.mjs
