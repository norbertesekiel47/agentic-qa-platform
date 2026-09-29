---
id: post-comment
goal: A signed-in reader adds a comment to an article and sees it posted under their name.
preconditions:
  start_url: /login
  account: { email: reader@conduit.test, password: { secret: TEST_PASSWORD } }
  reset: { http: "POST /test-api/reset?fixture=seed" }
  probes:
    comment_count: "GET /test-api/comments/count?article=welcome-to-conduit"
steps:
  - Sign in
  - Open "Welcome to Conduit" from the global feed
  - Write the comment "Thanks for the warm welcome!" and post it
expect:
  - The comment "Thanks for the warm welcome!" is shown under the article
  - The new comment is shown with reader as its author
  - jake's earlier comment "Glad to be here." is still shown
  - The comment is saved once, so the article now has exactly 2 comments
invariants: { inherit: true }
tags: [comments]
---

Benchmark pilot spec (M0).
