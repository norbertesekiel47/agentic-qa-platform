import { PrismaClientKnownRequestError } from '@prisma/client/runtime/client';
import HttpException from '~/models/http-exception.model';

/**
 * If the error is a Prisma unique constraint violation (P2002),
 * throws an HttpException 409 with the provided error messages.
 * Works across all databases supported by Prisma.
 *
 * @param e - The caught error
 * @param fieldErrors - Map of DB column name to user-facing error messages.
 *                      If the violated field is not in the map, all messages are returned.
 */
export function handleUniqueConstraintError(
    e: unknown,
    fieldErrors: Record<string, string[]>,
): void {
    if (!(e instanceof PrismaClientKnownRequestError) || e.code !== 'P2002') {
        return;
    }

    const target = e.meta?.target as string[] | undefined;
    const errors: Record<string, string[]> = {};

    if (target) {
        for (const [field, messages] of Object.entries(fieldErrors)) {
            if (target.includes(field)) {
                errors[field] = messages;
            }
        }
    }

    // If no specific field matched, return all provided errors
    throw new HttpException(409, {
        errors: Object.keys(errors).length > 0 ? errors : fieldErrors,
    });
}
