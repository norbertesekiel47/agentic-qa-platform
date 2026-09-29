import { ZodSchema, ZodError } from 'zod';
import HttpException from '~/models/http-exception.model';

export const validateBody = <T>(schema: ZodSchema<T>, body: unknown): T => {
    try {
        return schema.parse(body);
    } catch (error) {
        if (error instanceof ZodError) {
            const errors: Record<string, string[]> = {};
            for (const issue of error.issues) {
                const field = issue.path[issue.path.length - 1]?.toString() ?? 'body';
                if (!errors[field]) errors[field] = [];
                errors[field].push(issue.message);
            }
            throw new HttpException(422, { errors });
        }
        throw error;
    }
};
