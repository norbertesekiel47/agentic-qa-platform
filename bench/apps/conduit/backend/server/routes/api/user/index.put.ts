import {definePrivateEventHandler} from "~/auth-event-handler";
import {updateUserSchema} from '~/schemas/user.schema';
import {validateBody} from '~/utils/validate';
import {handleUniqueConstraintError} from '~/utils/prisma-errors';

export default definePrivateEventHandler(async (event, {auth}) => {
    const {user} = validateBody(updateUserSchema, await readBody(event));

    const {email, username, password, image, bio} = user;

    const data: any = {};
    if (email !== undefined) data.email = email;
    if (username !== undefined) data.username = username;
    if (password) data.password = await useHashPassword(password);
    if (image !== undefined) data.image = image;
    if (bio !== undefined) data.bio = bio;

    try {
        const updatedUser = await usePrisma().user.update({
            where: {
                id: auth.id,
            },
            data,
            select: {
                id: true,
                email: true,
                username: true,
                bio: true,
                image: true,
            },
        });

        return {
            user: {
                ...updatedUser,
                token: useGenerateToken(updatedUser.id),
            }
        };
    } catch (e) {
        handleUniqueConstraintError(e, {
            email: ['has already been taken'],
            username: ['has already been taken'],
        });
        throw e;
    }
});
