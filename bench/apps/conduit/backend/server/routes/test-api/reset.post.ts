// Benchmark test-only endpoint (ADR-0022). Ours, not upstream: see bench/apps/conduit/README.md.
// POST /test-api/reset?fixture=<name> restores that fixture's pristine database:
// close the Prisma client, copy /app/fixtures/<name>.db over the live file, and
// let the next usePrisma() reopen it. Flags are untouched. Resets run one at a
// time. A request in flight during a reset may fail, so callers reset between
// attempts, never during one.
import {existsSync} from 'node:fs';
import {copyFile} from 'node:fs/promises';

const FIXTURES = '/app/fixtures';
const FIXTURE_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;

let queue: Promise<void> = Promise.resolve();

function liveDatabasePath(): string {
    const url = process.env.DATABASE_URL ?? '';
    if (!url.startsWith('file:')) {
        throw new Error(`reset needs a file: DATABASE_URL, got '${url}'`);
    }
    return url.slice('file:'.length);
}

async function restore(source: string): Promise<void> {
    await closePrisma();
    await copyFile(source, liveDatabasePath());
}

export default defineEventHandler(async event => {
    const fixture = getQuery(event).fixture;
    const source = `${FIXTURES}/${fixture}.db`;
    if (typeof fixture !== 'string' || !FIXTURE_NAME.test(fixture) || !existsSync(source)) {
        throw createError({statusCode: 404, data: {errors: {fixture: ['unknown fixture']}}});
    }
    const run = queue.then(() => restore(source));
    queue = run.catch(() => undefined);
    await run;
    return sendNoContent(event);
});
