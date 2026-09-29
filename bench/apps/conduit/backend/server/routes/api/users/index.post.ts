import {registerUserSchema} from '~/schemas/user.schema';
import {validateBody} from '~/utils/validate';
import {handleUniqueConstraintError} from '~/utils/prisma-errors';

export default defineEventHandler(async (event) => {
    const {user} = validateBody(registerUserSchema, await readBody(event));

    const {email, username, password, image, bio} = user;

    const hashedPassword = await useHashPassword(password);

    try {
        const createdUser = await usePrisma().user.create({
            data: {
                username,
                email,
                password: hashedPassword,
                ...(image ? {image} : {}),
                ...(bio ? {bio} : {}),
            },
            select: {
                id: true,
                email: true,
                username: true,
                bio: true,
                image: true,
            },
        });

        setResponseStatus(event, 201);
        return {
            user: {
                ...createdUser,
                token: useGenerateToken(createdUser.id),
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
