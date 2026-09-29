import HttpException from "~/models/http-exception.model";
import {default as bcrypt} from 'bcryptjs';
import {loginUserSchema} from '~/schemas/user.schema';
import {validateBody} from '~/utils/validate';

export default defineEventHandler(async (event) => {
    const {user} = validateBody(loginUserSchema, await readBody(event));

    const {email, password} = user;

    const foundUser = await usePrisma().user.findUnique({
        where: {
            email,
        },
        select: {
            id: true,
            email: true,
            username: true,
            password: true,
            bio: true,
            image: true,
        },
    });

    if (foundUser) {
        const match = await bcrypt.compare(password, foundUser.password);

        if (match) {
            return {
                user: {
                    email: foundUser.email,
                    username: foundUser.username,
                    bio: foundUser.bio,
                    image: foundUser.image,
                    token: useGenerateToken(foundUser.id),
                }
            };
        }
    }

    throw new HttpException(401, {
        errors: {
            credentials: ['invalid'],
        },
    });
});
