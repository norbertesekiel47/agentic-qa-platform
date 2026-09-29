#!/bin/sh
# Benchmark feature flags (ADR-0022). Ours, not upstream.
# Runs from nginx's /docker-entrypoint.d on every container start. It renders
# index.html from the pristine build output, adding the active flag ids as an
# inline, non-executed JSON element that frontend/src/app/bench/flags.ts reads.
# Rendering from the template means a restart never stacks a second element.
set -eu
html=/usr/share/nginx/html/index.html
rm -f "$html" # an invalid flag set leaves no page, so the health check fails too
flags=$(bench-flags)
sed "s#</head>#<script id=\"app-flags\" type=\"application/json\">$flags</script></head>#" \
  /usr/share/nginx/index.template.html >"$html"
