import bcrypt from 'bcryptjs';

const SALT_ROUNDS = Number(process.env.BCRYPT_SALT_ROUNDS) || 10;

export const useHashPassword = (password: string) => {
    return bcrypt.hash(password, SALT_ROUNDS);
}

export const useDecrypt = (input: string, password: string) => {
    return bcrypt.compare(input, password);
}
