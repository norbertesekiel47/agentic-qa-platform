import jwt from 'jsonwebtoken';
import {getJwtSecret} from './jwt-secret';

export const useVerifyToken = (token: string): {id: number} => {
    try {
        const decoded = jwt.verify(token, getJwtSecret()) as {user: {id: number}};
        return {id: Number(decoded.user.id)};
    } catch (error) {
        if (error instanceof jwt.TokenExpiredError) {
            throw createError({
                status: 401,
                statusMessage: 'Unauthorized',
                data: {errors: {token: ['has expired']}},
            });
        }
        if (error instanceof jwt.JsonWebTokenError) {
            throw createError({
                status: 401,
                statusMessage: 'Unauthorized',
                data: {errors: {token: ['is invalid']}},
            });
        }
        throw error;
    }
};
