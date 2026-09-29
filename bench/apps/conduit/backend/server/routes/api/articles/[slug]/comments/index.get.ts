import HttpException from "~/models/http-exception.model";
import {definePrivateEventHandler} from "~/auth-event-handler";

export default definePrivateEventHandler(async (event, {auth}) => {
    const slug = getRouterParam(event, 'slug');

    const article = await usePrisma().article.findUnique({
        where: {
            slug,
        },
        include: {
            comments: {
                select: {
                    id: true,
                    createdAt: true,
                    updatedAt: true,
                    body: true,
                    author: {
                        select: {
                            username: true,
                            bio: true,
                            image: true,
                            followedBy: true,
                        },
                    },
                },
            },
        },
    });

    if (!article) {
        throw new HttpException(404, {errors: {article: ['not found']}});
    }

    const result = article.comments.map((comment: any) => ({
        ...comment,
        author: {
            username: comment.author.username,
            bio: comment.author.bio,
            image: comment.author.image,
            following: comment.author.followedBy.some((follow: any) => follow.id === auth?.id),
        },
    }));

    return {comments: result};
}, {requireAuth: false});
