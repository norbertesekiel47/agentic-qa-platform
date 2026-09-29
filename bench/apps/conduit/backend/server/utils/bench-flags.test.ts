import {describe, test, expect} from 'bun:test';
import {parseBenchFlags} from './bench-flags';

describe('parseBenchFlags', () => {
    test('unset or empty means the clean app', () => {
        expect(parseBenchFlags(undefined).size).toBe(0);
        expect(parseBenchFlags('').size).toBe(0);
    });

    test('reads comma-separated flag ids', () => {
        const flags = parseBenchFlags('k3q9,h3k8');
        expect([...flags]).toEqual(['k3q9', 'h3k8']);
        expect(flags.has('zzzz')).toBe(false);
    });
});
