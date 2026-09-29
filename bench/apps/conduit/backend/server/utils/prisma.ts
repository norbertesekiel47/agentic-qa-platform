import { PrismaLibSql } from '@prisma/adapter-libsql';
import { PrismaClient } from '../../generated/prisma/client';

let _prisma: InstanceType<typeof PrismaClient> | undefined;

export const usePrisma = () => {
    if (!_prisma) {
        const adapter = new PrismaLibSql({ url: process.env.DATABASE_URL ?? 'file:./dev.db' });
        _prisma = new PrismaClient({ adapter });
    }
    return _prisma;
}

// Not upstream: the benchmark reset (ADR-0022, bench/apps/conduit/README.md).
// Closes the client so the next usePrisma() opens the file /test-api/reset restored.
export const closePrisma = async () => {
    const client = _prisma;
    _prisma = undefined;
    await client?.$disconnect();
}
