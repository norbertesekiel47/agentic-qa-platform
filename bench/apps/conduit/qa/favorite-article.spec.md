---
id: favorite-article
goal: A signed-in reader favorites an article, and the favorite is still there after the page reloads.
preconditions:
  start_url: /login
  account: { email: reader@conduit.test, password: { secret: TEST_PASSWORD } }
  reset: { http: "POST /test-api/reset?fixture=seed" }
steps:
  - Sign in
  - Open "Flaky tests are bugs" from the global feed
  - Favorite the article
  - Reload the article page
expect:
  - The favorite button reads "Unfavorite Article"
  - The article's favorites count shows 1
invariants: { inherit: true }
tags: [favorites]
---

Benchmark pilot spec (M0).
