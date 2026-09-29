/**
 * Deterministic seed for the Conduit benchmark app (ADR-0020).
 *
 * Every build produces the same users, articles, tags, comments, favorites and
 * follows: fixed slugs, fixed timestamps, and a fixed insertion order so ids
 * are stable too. Twelve articles give the global feed two pages (10 per page).
 *
 * The shared account password comes from CONDUIT_SEED_PASSWORD. It is a public
 * benchmark fixture for a local app, not a secret; specs receive it as the
 * TEST_PASSWORD secret so it never appears in model input.
 */
import { PrismaLibSql } from '@prisma/adapter-libsql';
import bcrypt from 'bcryptjs';
import { PrismaClient } from '../generated/prisma/client';

type SeedArticle = {
  slug: string;
  title: string;
  description: string;
  body: string;
  author: string;
  tags: string[];
  day: number;
};

/** A fixed instant in January 2026 (UTC). The app renders it in the viewer's time zone, so benchmark browsers run in UTC (TESTING.md §5). */
const at = (day: number, hour = 10): Date => new Date(Date.UTC(2026, 0, day, hour));

const USERS = [
  { username: 'jake', email: 'jake@conduit.test', bio: 'I write about dragons and training.' },
  { username: 'anna', email: 'anna@conduit.test', bio: 'Frontend engineer and testing enthusiast.' },
  { username: 'reader', email: 'reader@conduit.test', bio: null },
];

const ARTICLES: SeedArticle[] = [
  {
    slug: 'how-to-train-your-dragon',
    title: 'How to train your dragon',
    description: 'Ever wondered how?',
    body: 'It takes patience, a steady routine and a lot of fish.\n\nStart small: short sessions, clear signals, and a reward every time.',
    author: 'jake',
    tags: ['dragons', 'training'],
    day: 1,
  },
  {
    slug: 'how-to-train-your-dragon-2',
    title: 'How to train your dragon 2',
    description: 'So toothless',
    body: 'Part two covers flight training.\n\nNever train above water until the basics are solid.',
    author: 'jake',
    tags: ['dragons'],
    day: 2,
  },
  {
    slug: 'welcome-to-conduit',
    title: 'Welcome to Conduit',
    description: 'A place to share your knowledge.',
    body: 'Conduit is a community of writers.\n\nFollow authors you like and your feed fills up with their articles.',
    author: 'anna',
    tags: ['welcome'],
    day: 3,
  },
  {
    slug: 'testing-without-flakes',
    title: 'Testing without flakes',
    description: 'Deterministic data first, clever retries never.',
    body: 'Most flaky tests are really flaky data.\n\nSeed everything, fix the clock, and make every run start from the same state.',
    author: 'anna',
    tags: ['testing'],
    day: 4,
  },
  {
    slug: 'angular-signals-in-practice',
    title: 'Angular signals in practice',
    description: 'What changed when we moved our state to signals.',
    body: 'Signals made change detection predictable.\n\nComputed values replaced most of our manual subscriptions.',
    author: 'jake',
    tags: ['angular'],
    day: 5,
  },
  {
    slug: 'writing-accessible-forms',
    title: 'Writing accessible forms',
    description: 'Labels, errors and focus order.',
    body: 'Every input needs a visible label.\n\nAnnounce errors next to the field that caused them, and move focus there.',
    author: 'anna',
    tags: ['accessibility', 'css'],
    day: 6,
  },
  {
    slug: 'a-readers-first-post',
    title: "A reader's first post",
    description: 'Hello from a longtime reader.',
    body: 'I have been reading Conduit for years.\n\nThis is my first article.',
    author: 'reader',
    tags: ['welcome'],
    day: 7,
  },
  {
    slug: 'measuring-page-performance',
    title: 'Measuring page performance',
    description: 'Numbers you can trust.',
    body: 'Measure on real devices.\n\nReport the median and the 95th percentile, never a single run.',
    author: 'jake',
    tags: ['performance'],
    day: 8,
  },
  {
    slug: 'css-grid-for-layouts',
    title: 'CSS grid for layouts',
    description: 'Two-dimensional layout without the hacks.',
    body: 'Grid handles rows and columns together.\n\nUse flexbox inside the cells, grid for the page.',
    author: 'anna',
    tags: ['css'],
    day: 9,
  },
  {
    slug: 'dragons-of-the-north',
    title: 'Dragons of the north',
    description: 'A field guide.',
    body: 'Northern dragons prefer cold caves.\n\nThey are shy, but curious about visitors who bring fish.',
    author: 'jake',
    tags: ['dragons'],
    day: 10,
  },
  {
    slug: 'flaky-tests-are-bugs',
    title: 'Flaky tests are bugs',
    description: 'Treat them like any other defect.',
    body: 'A test that fails one run in fifty is telling you something.\n\nFind the race; do not add a retry.',
    author: 'anna',
    tags: ['testing'],
    day: 11,
  },
  {
    slug: 'notes-on-reading-lists',
    title: 'Notes on reading lists',
    description: 'How I keep track of what to read next.',
    body: 'One list, sorted by how much I want to read it.\n\nAnything older than a year gets deleted.',
    author: 'reader',
    tags: [],
    day: 12,
  },
];

const COMMENTS = [
  { article: 'how-to-train-your-dragon', author: 'anna', body: 'Great article, thanks!', day: 1, hour: 12 },
  { article: 'how-to-train-your-dragon', author: 'reader', body: 'Looking forward to part two.', day: 1, hour: 13 },
  { article: 'welcome-to-conduit', author: 'jake', body: 'Glad to be here.', day: 3, hour: 12 },
  { article: 'testing-without-flakes', author: 'reader', body: 'Deterministic data helped us most.', day: 4, hour: 15 },
];

const FAVORITES = [
  { user: 'reader', article: 'welcome-to-conduit' },
  { user: 'reader', article: 'testing-without-flakes' },
  { user: 'anna', article: 'how-to-train-your-dragon' },
  { user: 'jake', article: 'css-grid-for-layouts' },
];

const FOLLOWS = [
  { follower: 'reader', followed: 'jake' },
  { follower: 'anna', followed: 'jake' },
];

async function main(): Promise<void> {
  const password = process.env.CONDUIT_SEED_PASSWORD;
  const url = process.env.DATABASE_URL;
  if (!password || !url) {
    throw new Error('CONDUIT_SEED_PASSWORD and DATABASE_URL are required');
  }
  const prisma = new PrismaClient({ adapter: new PrismaLibSql({ url }) });
  const hashed = await bcrypt.hash(password, Number(process.env.BCRYPT_SALT_ROUNDS) || 10);

  for (const user of USERS) {
    await prisma.user.create({ data: { ...user, password: hashed } });
  }
  const tagNames = [...new Set(ARTICLES.flatMap((article) => article.tags))].sort();
  for (const name of tagNames) {
    await prisma.tag.create({ data: { name } });
  }
  for (const article of ARTICLES) {
    await prisma.article.create({
      data: {
        slug: article.slug,
        title: article.title,
        description: article.description,
        body: article.body,
        createdAt: at(article.day),
        updatedAt: at(article.day),
        author: { connect: { username: article.author } },
        tagList: { connect: article.tags.map((name) => ({ name })) },
      },
    });
  }
  for (const comment of COMMENTS) {
    await prisma.comment.create({
      data: {
        body: comment.body,
        createdAt: at(comment.day, comment.hour),
        updatedAt: at(comment.day, comment.hour),
        article: { connect: { slug: comment.article } },
        author: { connect: { username: comment.author } },
      },
    });
  }
  for (const favorite of FAVORITES) {
    await prisma.article.update({
      where: { slug: favorite.article },
      data: { favoritedBy: { connect: { username: favorite.user } } },
    });
  }
  for (const follow of FOLLOWS) {
    await prisma.user.update({
      where: { username: follow.follower },
      data: { following: { connect: { username: follow.followed } } },
    });
  }
  await prisma.$disconnect();
  console.log(
    `seeded ${USERS.length} users, ${ARTICLES.length} articles, ${tagNames.length} tags, ` +
      `${COMMENTS.length} comments, ${FAVORITES.length} favorites, ${FOLLOWS.length} follows`,
  );
}

await main();
