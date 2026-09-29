---
id: login
goal: A returning reader signs in and sees the navigation for signed-in users.
preconditions:
  start_url: /login
  account: { email: reader@conduit.test, password: { secret: TEST_PASSWORD } }
  reset: { http: "POST /test-api/reset?fixture=seed" }
steps:
  - Sign in with the account's email and password
expect:
  - The home page is shown after signing in
  - A "Your Feed" tab is shown above the article list
  - The header has links to "New Article", "Settings" and the reader's profile, labelled "reader"
  - The header no longer has "Sign in" or "Sign up" links
  - text: The header's "New Article", "Settings" and "reader" links are visible and not covered by any other element
    visual: deterministic
invariants: { inherit: true }
tags: [auth, navigation]
---

Benchmark pilot spec (M0). The seeded accounts are listed in `bench/apps/conduit/README.md`.
