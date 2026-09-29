# Conduit frontend for the benchmark (ADR-0020). Build context: bench/apps/conduit.
FROM oven/bun:1.4.2@sha256:9114c058aeae42162ee16dd5084b95fe9473970bb6bcb5b232ab1630f0546895 AS build
WORKDIR /app
COPY frontend/ ./
# --ignore-scripts: upstream's "prepare" script installs husky git hooks, and a
# vendored copy has no git repository of its own.
RUN bun install --frozen-lockfile --ignore-scripts \
 && bun run build

FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/angular-conduit/browser /usr/share/nginx/html
