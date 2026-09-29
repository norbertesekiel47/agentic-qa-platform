import { z } from 'zod';

export const createArticleSchema = z.object({
    article: z.object({
        title: z.string().trim().min(1, "can't be blank"),
        description: z.string().trim().min(1, "can't be blank"),
        body: z.string().trim().min(1, "can't be blank"),
        tagList: z.array(z.string()).optional().default([]),
    }),
});

export const updateArticleSchema = z.object({
    article: z.object({
        title: z.string().trim().min(1).optional(),
        description: z.string().trim().min(1).optional(),
        body: z.string().trim().min(1).optional(),
        tagList: z.array(z.string()).optional(),
    }),
});
