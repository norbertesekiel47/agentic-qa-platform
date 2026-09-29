// Benchmark feature flags (ADR-0022). Ours, not upstream: see bench/apps/conduit/README.md.
// docker/backend-entrypoint.sh validates BENCH_FLAGS before the server starts.
// The value is read once: switching cases recreates the container.

export function parseBenchFlags(raw: string | undefined): ReadonlySet<string> {
    return new Set((raw ?? '').split(',').map(id => id.trim()).filter(id => id !== ''));
}

const active = parseBenchFlags(process.env.BENCH_FLAGS);

/** Whether a planted change is on. Cite the case id in a comment at each call site. */
export function benchFlag(id: string): boolean {
    return active.has(id);
}
