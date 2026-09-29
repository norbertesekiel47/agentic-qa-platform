// Benchmark probe (ADR-0022). Ours, not upstream: see bench/apps/conduit/README.md.
// GET /test-api/articles/count?author=<username> → {"count": n}, the number of
// articles that user has written. Read-only.
export default defineEventHandler(async event => {
    const author = getQuery(event).author;
    if (typeof author !== 'string' || author === '') {
        throw createError({statusCode: 400, data: {errors: {author: ['is required']}}});
    }
    const count = await usePrisma().article.count({where: {author: {username: author}}});
    return {count};
});
