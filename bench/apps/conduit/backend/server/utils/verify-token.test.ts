import {describe, test, expect, beforeAll, afterAll} from 'bun:test';
import jwt from 'jsonwebtoken';

// verify-token.ts uses Nitro's auto-imported createError, so we need to provide it globally
globalThis.createError = (input: any) => {
    const err = new Error(input.statusMessage) as any;
    err.status = input.status;
    err.statusMessage = input.statusMessage;
    err.data = input.data;
    return err;
};

const TEST_SECRET = 'test-secret-for-unit-tests';

describe('useVerifyToken', () => {
    let useVerifyToken: typeof import('./verify-token').useVerifyToken;
    let originalSecret: string | undefined;

    beforeAll(async () => {
        originalSecret = process.env.JWT_SECRET;
        process.env.JWT_SECRET = TEST_SECRET;
        ({useVerifyToken} = await import('./verify-token'));
    });

    afterAll(() => {
        if (originalSecret !== undefined) {
            process.env.JWT_SECRET = originalSecret;
        } else {
            delete process.env.JWT_SECRET;
        }
    });

    test('returns user id for a valid token', () => {
        const token = jwt.sign({user: {id: 42}}, TEST_SECRET, {expiresIn: '1h'});
        const result = useVerifyToken(token);
        expect(result).toEqual({id: 42});
    });

    test('throws 401 for an expired token', () => {
        const token = jwt.sign({user: {id: 1}}, TEST_SECRET, {expiresIn: '-1s'});
        try {
            useVerifyToken(token);
            throw new Error('should have thrown');
        } catch (e: any) {
            expect(e.status).toBe(401);
            expect(e.data.errors.token).toEqual(['has expired']);
        }
    });

    test('throws 401 for a malformed token', () => {
        try {
            useVerifyToken('not-a-real-token');
            throw new Error('should have thrown');
        } catch (e: any) {
            expect(e.status).toBe(401);
            expect(e.data.errors.token).toEqual(['is invalid']);
        }
    });

    test('throws 401 for a token signed with wrong secret', () => {
        const token = jwt.sign({user: {id: 1}}, 'wrong-secret', {expiresIn: '1h'});
        try {
            useVerifyToken(token);
            throw new Error('should have thrown');
        } catch (e: any) {
            expect(e.status).toBe(401);
            expect(e.data.errors.token).toEqual(['is invalid']);
        }
    });
});

describe('getJwtSecret', () => {
    let getJwtSecret: typeof import('./jwt-secret').getJwtSecret;

    beforeAll(async () => {
        ({getJwtSecret} = await import('./jwt-secret'));
    });

    test('throws when JWT_SECRET is not set', () => {
        const original = process.env.JWT_SECRET;
        delete process.env.JWT_SECRET;
        try {
            expect(() => getJwtSecret()).toThrow('JWT_SECRET environment variable is not set');
        } finally {
            if (original !== undefined) {
                process.env.JWT_SECRET = original;
            }
        }
    });

    test('returns the secret when set', () => {
        process.env.JWT_SECRET = 'my-secret';
        try {
            expect(getJwtSecret()).toBe('my-secret');
        } finally {
            // restore
        }
    });
});
