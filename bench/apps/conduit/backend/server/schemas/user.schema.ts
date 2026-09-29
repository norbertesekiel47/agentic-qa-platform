import { z } from 'zod';

export const registerUserSchema = z.object({
    user: z.object({
        email: z.string().trim().min(1, "can't be blank"),
        username: z.string().trim().min(1, "can't be blank"),
        password: z.string().trim().min(1, "can't be blank"),
        image: z.string().optional(),
        bio: z.string().optional(),
    }),
});

export const loginUserSchema = z.object({
    user: z.object({
        email: z.string().trim().min(1, "can't be blank"),
        password: z.string().trim().min(1, "can't be blank"),
    }),
});

export const updateUserSchema = z.object({
    user: z.object({
        email: z.string().trim().min(1, "can't be blank").optional(),
        username: z.string().trim().min(1, "can't be blank").optional(),
        // NIST 800-63B section 5.1.1.2: memorized secrets must be at least 8 chars.
        password: z.string().trim().min(8, 'must be at least 8 characters long').optional(),
        image: z.string().nullable().optional().transform(v => v === '' ? null : v),
        bio: z.string().nullable().optional().transform(v => v === '' ? null : v),
    }),
});
