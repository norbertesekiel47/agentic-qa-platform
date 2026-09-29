import {useVerifyToken} from '~/utils/verify-token';

export interface PrivateContext {
    auth: {
        id: number;
    }
}

export function definePrivateEventHandler<T>(
    handler: (event: H3Event, cxt: PrivateContext) => T,
    options: { requireAuth: boolean } = {requireAuth: true}
) {
    return defineEventHandler(async (event) => {
        const header = getHeader(event, 'authorization');
        let token;

        if (
            (header && header.split(' ')[0] === 'Token') ||
            (header && header.split(' ')[0] === 'Bearer')
        ) {
            token = header.split(' ')[1];
        }

        if (options.requireAuth && !token) {
            throw createError({
                status: 401,
                statusMessage: 'Unauthorized',
                data: {errors: {token: ['is missing']}},
            });
        }

        if (token) {
            const auth = useVerifyToken(token);

            return handler(event, {auth});
        } else {
            return handler(event, {
                auth: null,
            })
        }
    })
}
