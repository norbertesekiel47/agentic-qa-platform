# Conduit backend for the benchmark (ADR-0020). Build context: bench/apps/conduit.
FROM oven/bun:1.4.2@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895
WORKDIR /app

COPY backend/ ./
RUN bun install --frozen-lockfile \
 && bun x prisma generate \
 && bun run build

# Seed a pristine database once, at build time. Every container start copies it
# into /data (a tmpfs), so restarting the backend resets the app state.
COPY seed/ ./bench-seed/
ARG CONDUIT_SEED_PASSWORD
RUN test -n "$CONDUIT_SEED_PASSWORD" \
 && DATABASE_URL=file:/app/seed.db bun x prisma db push \
 && DATABASE_URL=file:/app/seed.db bun bench-seed/seed.ts

COPY --chmod=0755 docker/bench-flags.sh /usr/local/bin/bench-flags
COPY docker/backend-entrypoint.sh /usr/local/bin/conduit-backend
ENV DATABASE_URL=file:/data/conduit.db HOST=0.0.0.0 PORT=3000
EXPOSE 3000
ENTRYPOINT ["/usr/local/bin/conduit-backend"]
