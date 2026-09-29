import { z } from 'zod';

export const createCommentSchema = z.object({
    comment: z.object({
        body: z.string().trim().min(1, "can't be blank"),
    }),
});
