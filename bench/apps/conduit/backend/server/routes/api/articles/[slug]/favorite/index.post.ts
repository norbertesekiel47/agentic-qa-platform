import HttpException from "~/models/http-exception.model";
import profileMapper from "~/utils/profile.utils";
import {Tag} from "~/models/tag.model";
import {definePrivateEventHandler} from "~/auth-event-handler";

export default definePrivateEventHandler(async (event, {auth}) => {
    const slug = getRouterParam(event, "slug");

    // conduit-bug-003 (ADR-0022 flag): reads the user id from an auth shape that no longer
    // exists, so every favorite fails with a TypeError (HTTP 500).
    const userId: number = benchFlag('7ozy') ? (auth as any).user.id : auth.id;

    const existing = await usePrisma().article.findUnique({ where: { slug } });
    if (!existing) {
        throw new HttpException(404, {errors: {article: ['not found']}});
    }

    const { _count, ...article } = await usePrisma().article.update({
        where: {
            slug,
        },
        data: {
            favoritedBy: {
                connect: {
                    id: userId,
                },
            },
        },
        include: {
            tagList: {
                select: {
                    name: true,
                },
            },
            author: {
                select: {
                    username: true,
                    bio: true,
                    image: true,
                    followedBy: true,
                },
            },
            favoritedBy: true,
            _count: {
                select: {
                    favoritedBy: true,
                },
            },
        },
    });

    const result = {
        ...article,
        author: profileMapper(article.author, auth.id),
        tagList: article?.tagList.map((tag: Tag) => tag.name),
        favorited: article.favoritedBy.some((favorited: any) => favorited.id === auth.id),
        favoritesCount: _count?.favoritedBy,
    };

    return {article: result};
});
