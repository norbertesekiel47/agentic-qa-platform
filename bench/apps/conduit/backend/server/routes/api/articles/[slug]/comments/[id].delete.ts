import HttpException from "~/models/http-exception.model";
import {definePrivateEventHandler} from "~/auth-event-handler";

export default definePrivateEventHandler(async (event, {auth}) => {
    const slug = getRouterParam(event, 'slug');
    const id = Number(getRouterParam(event, 'id'));

    const article = await usePrisma().article.findUnique({
        where: { slug },
        select: { id: true },
    });

    if (!article) {
        throw new HttpException(404, {errors: {article: ['not found']}});
    }

    const comment = await usePrisma().comment.findFirst({
        where: {
            id,
            articleId: article.id,
        },
        select: {
            author: {
                select: {
                    id: true,
                },
            },
        },
    });

    if (!comment) {
        throw new HttpException(404, {errors: {comment: ['not found']}});
    }

    if (comment.author.id !== auth.id) {
        throw new HttpException(403, {errors: {comment: ['forbidden']}});
    }

    await usePrisma().comment.delete({
        where: {
            id,
        },
    });

    setResponseStatus(event, 204);
    return null;
});
