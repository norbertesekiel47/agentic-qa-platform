import {describe, test, expect} from 'bun:test';
import {PrismaClientKnownRequestError} from '@prisma/client/runtime/client';
import {handleUniqueConstraintError} from './prisma-errors';
import HttpException from '~/models/http-exception.model';

const makeP2002 = (target: string[]) =>  {
    const e = new PrismaClientKnownRequestError('Unique constraint failed', {code: 'P2002', clientVersion: '7.0.0'});
    e.meta = {target};
    return e;
};

describe('handleUniqueConstraintError', () => {
    test('does nothing for non-Prisma errors', () => {
        handleUniqueConstraintError(new Error('random'), {email: ['taken']});
    });

    test('does nothing for non-P2002 Prisma errors', () => {
        const e = new PrismaClientKnownRequestError('Foreign key failed', {code: 'P2003', clientVersion: '7.0.0'});
        handleUniqueConstraintError(e, {email: ['taken']});
    });

    test('throws 409 with matched field when target matches', () => {
        const e = makeP2002(['email']);
        expect(() => handleUniqueConstraintError(e, {
            email: ['has already been taken'],
            username: ['has already been taken'],
        })).toThrow(expect.objectContaining({
            errorCode: 409,
            message: {errors: {email: ['has already been taken']}},
        }));
    });

    test('throws 409 with all fields when target does not match any key', () => {
        const e = makeP2002(['slug']);
        expect(() => handleUniqueConstraintError(e, {
            title: ['has already been taken'],
        })).toThrow(expect.objectContaining({
            errorCode: 409,
            message: {errors: {title: ['has already been taken']}},
        }));
    });

    test('throws 409 with all fields when target is undefined', () => {
        const e = new PrismaClientKnownRequestError('Unique constraint failed', {code: 'P2002', clientVersion: '7.0.0'});
        expect(() => handleUniqueConstraintError(e, {
            email: ['taken'],
            username: ['taken'],
        })).toThrow(expect.objectContaining({
            errorCode: 409,
            message: {errors: {email: ['taken'], username: ['taken']}},
        }));
    });
});
