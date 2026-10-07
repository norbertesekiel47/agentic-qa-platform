---
id: publish-article
goal: A signed-in author publishes a new article with tags and lands on its page, whose path starts with /article/benchmarks-we-trust- in any letter case and ends in a generated suffix.
preconditions:
  start_url: /login
  account: { email: jake@conduit.test, password: { secret: TEST_PASSWORD } }
  reset: { http: "POST /test-api/reset?fixture=seed" }
  probes:
    jake_article_count: "GET /test-api/articles/count?author=jake"
steps:
  - Sign in
  - Open the editor with "New Article"
  - Enter the title "Benchmarks we trust", the description "How we measure agents.", the body "Every number cites the commit that produced it." and the tags "testing" and "benchmarks"
  - Publish the article
expect:
  - The new article's page shows the title "Benchmarks we trust"
  - The article body reads "Every number cites the commit that produced it."
  - The article is tagged "testing" and "benchmarks"
  - The article is shown with jake as its author
  - Exactly one article is created, so jake now has 6 articles
invariants: { inherit: true }
tags: [articles, editor]
---

Benchmark pilot spec (M0).
