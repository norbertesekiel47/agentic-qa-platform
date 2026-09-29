// Benchmark feature flags (ADR-0022). Ours, not upstream: see bench/apps/conduit/README.md.
// The frontend container writes the active flag ids into index.html at start
// (docker/frontend-flags.sh) as <script id="app-flags" type="application/json">.
// bench/harness/flags.py checks the served list after every switch.

function readFlagIds(): string[] {
  const ids: unknown = JSON.parse(document.getElementById('app-flags')?.textContent ?? '[]');
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : [];
}

const active: ReadonlySet<string> = new Set(readFlagIds());

/** Whether a planted change is on. Cite the case id in a comment at each call site. */
export function benchFlag(id: string): boolean {
  return active.has(id);
}
