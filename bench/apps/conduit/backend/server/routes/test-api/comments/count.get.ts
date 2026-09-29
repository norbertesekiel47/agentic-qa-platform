// Benchmark probe (ADR-0022). Ours, not upstream: see bench/apps/conduit/README.md.
// GET /test-api/comments/count?article=<slug> → {"count": n}, the number of
// comments on that article. Read-only.
export default defineEventHandler(async event => {
    const article = getQuery(event).article;
    if (typeof article !== 'string' || article === '') {
        throw createError({statusCode: 400, data: {errors: {article: ['is required']}}});
    }
    const count = await usePrisma().comment.count({where: {article: {slug: article}}});
    return {count};
});
