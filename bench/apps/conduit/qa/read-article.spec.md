---
id: read-article
goal: A visitor who is not signed in opens an article from the global feed and reads it with its comments.
preconditions:
  start_url: /
  reset: { http: "POST /test-api/reset?fixture=seed" }
steps:
  - Open "Testing without flakes" from the global feed
expect:
  - The article page shows the title "Testing without flakes"
  - The article's author is shown as anna
  - The article's publication date is shown as January 4, 2026
  - The article body includes "Most flaky tests are really flaky data."
  - The article is tagged "testing"
  - reader's comment "Deterministic data helped us most." is shown under the article
  - A prompt invites the visitor to sign in or sign up to add comments
invariants: { inherit: true }
tags: [articles, comments]
---

Benchmark pilot spec (M0).
