# ![RealWorld Example App](logo.png)

> ### Nitro + Prisma + Zod codebase containing real world examples (CRUD, auth, advanced patterns, etc) that adheres to the [RealWorld](https://github.com/realworld-apps/realworld) spec and API.


### [Demo (of another codebase)](https://demo.realworld.show/)&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;[RealWorld](https://github.com/realworld-apps/realworld)

This codebase was created to demonstrate a fully fledged fullstack application built with [Nitro](https://nitro.build), [Prisma](https://www.prisma.io), and [Zod](https://zod.dev) including CRUD operations, authentication, routing, pagination, and more.

For more information on how this works with other [frontends/backends](https://en.wikipedia.org/wiki/Frontend_and_backend), head over to the [RealWorld](https://github.com/realworld-apps/realworld) repo.

#### About [RealWorld](https://codebase.show/projects/realworld)
The [RealWorld Demo App](https://codebase.show/projects/realworld) includes many implementations of the same project ([a Medium clone](https://demo.realworld.io/#/)), for which all [frontends](https://codebase.show/projects/realworld?category=frontend) and [backends](https://codebase.show/projects/realworld?category=backend) are supposed to be switchable from one another [as they all follow the same API](https://github.com/realworld-apps/realworld/tree/main/api).

It is supposed to [`reflect something similar to an early-stage startup's MVP`](https://realworld-docs.netlify.app/docs/implementation-creation/expectations), contrarily to some demo apps that are either too little or too much complex, and provide a good way to assert differences between frameworks.

#### About the tech stack

| Layer | Technology |
|-------|-----------|
| Runtime | [Bun](https://bun.sh) |
| Framework | [Nitro](https://nitro.build) (file-based routing) |
| Database | [SQLite](https://www.sqlite.org) via [libsql](https://turso.tech/libsql) |
| ORM | [Prisma](https://www.prisma.io) |
| Validation | [Zod](https://zod.dev) |
| Auth | JWT ([jsonwebtoken](https://github.com/auth0/node-jsonwebtoken)) + [bcryptjs](https://github.com/nicolo-ribaudo/bcryptjs) |

[Nitro](https://nitro.build) is a lightweight meta-framework for building server-side applications with file-based routing, auto-imports, and zero-config TypeScript support. [Prisma](https://www.prisma.io) provides type-safe database access, and [Zod](https://zod.dev) handles runtime schema validation.

As this repository only covers the API part, you may then [connect a frontend to it](#connect-a-frontend) after running the server.

## Usage

1. Clone the Git repository

```shell
git clone --recurse-submodules https://github.com/c4ffein/nitro-prisma-zod-realworld-example-app.git
cd nitro-prisma-zod-realworld-example-app
```

If you already cloned without `--recurse-submodules`, run `make submodule` to fetch the [test specs](https://github.com/realworld-apps/realworld).

2. Install [Bun](https://bun.sh) if not already installed

```shell
curl -fsSL https://bun.sh/install | bash
```

3. Setup (install dependencies, generate Prisma client, push database schema)

```shell
make setup
```

4. Run Application

```shell
# Run in development mode with hot reload
make run
# OR
JWT_SECRET=your-secret-here bun run dev
```

The API is then available at [http://localhost:3000/api](http://localhost:3000/api).

### Testing

- `make unit-test`: Bun unit tests.
- `make test-with-hurl`: API integration tests using [Hurl](https://hurl.dev) (manages the server automatically).
- `make test-with-hurl-and-already-launched-server`: Same, but expects the server to be already running.
- `make test-with-bruno`: API tests using [Bruno](https://www.usebruno.com) (manages the server automatically).

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `JWT_SECRET` | Secret key for JWT token signing | *required* |
| `DATABASE_URL` | Database connection URL | `file:./dev.db` |
| `BCRYPT_SALT_ROUNDS` | Number of bcrypt salt rounds | `10` |

### Connect a frontend
Choose a frontend from [codebase.show](https://codebase.show/projects/realworld?category=frontend) and configure it to point to `http://localhost:3000`.

## Contributing
If you would like to contribute to the project, please follow these guidelines:
- Fork the repository and create a new branch for your feature or bug fix.
- Make the necessary changes and commit them.
- Push your changes to your forked repository.
- Submit a pull request to the main repository, explaining the changes you made and any additional information that might be helpful for review.

## License
This project is released under the [MIT License](LICENSE).
