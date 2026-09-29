# Dry compile-rules review: Conduit pilot specs

The dry review for M0 (ADR-0023). The compiler doesn't exist until M1, so this table records, for every `expect` clause, the check(s) from DATA_MODEL §7 that **establish** it, and the weaker proxy it must not compile to. At M1, `aqa explore` must compile every spec here, and its compiled assertions are compared with this table. Any disagreement is an M1 finding about the spec or the compiler.

This file holds no ground truth: which case breaks which clause lives only in `bench/manifest.v1.json`.

## Rules applied
- Every clause maps to at least one check that establishes it. There are no proxies, and every clause below has an establishing check.
- **Assertions run after the last step,** so each UI clause describes the final page. Persistence is shown by the final state: a reload step, or a probe.
- **A count claim needs a probe.** The §7 checks can't count elements on a page, so "exactly N" uses a read-only probe on the app origin.
- Clauses with no establishing check were left out rather than weakened. For example, "the comment box is empty again" needs an input-value check, and §7 has none.

## Specs

### `login`
| # | Clause | Establishing check(s) | Not acceptable |
|---|---|---|---|
| 0 | The home page is shown after signing in | `url_matches` `^/$` (path) | A "Global Feed" heading: signed-out visitors see it too |
| 1 | A "Your Feed" tab is shown above the article list | `text_visible` "Your Feed" (only signed-in users see it) | — |
| 2 | The header has links to "New Article", "Settings" and "reader" | `text_in_target` on the header nav, one per link | `text_visible` anywhere on the page |
| 3 | The header no longer has "Sign in" or "Sign up" links | `not_visible` for each, scoped to the header nav | — |
| 4 | *(visual: deterministic)* Those three links are visible and not covered | `visible_unoccluded` for each link (hit test at its center, in viewport, non-zero size) | Clause 2 again: text in the DOM says nothing about what covers it |

### `read-article`
| # | Clause | Establishing check(s) | Not acceptable |
|---|---|---|---|
| 0 | Title "Testing without flakes" | `text_in_target` on the banner `h1` | — |
| 1 | Author shown as anna | `text_in_target` on the banner's author link | — |
| 2 | Publication date January 4, 2026 | `text_in_target` on the **banner's** date | **`text_visible` "January 4, 2026"**: reader's comment on this article shows the same date, so that check passes even when the article's own date is wrong |
| 3 | Body includes "Most flaky tests are really flaky data." | `text_in_target` on the article body | — |
| 4 | Tagged "testing" | `text_in_target` on the article's tag list | `text_visible` "testing": the title "Testing without flakes" matches it |
| 5 | reader's comment "Deterministic data helped us most." is shown | `text_in_target` on the comment card that holds that text, which must also contain "reader" | — |
| 6 | Visitors are invited to sign in or sign up to comment | `text_visible` "to add comments on this article" | — |

### `post-comment`
| # | Clause | Establishing check(s) | Not acceptable |
|---|---|---|---|
| 0 | The comment "Thanks for the warm welcome!" is shown | `text_in_target` on the comment list | — |
| 1 | The new comment shows reader as its author | `text_in_target` on that comment card's author link | `text_visible` "reader": the header shows it too |
| 2 | jake's comment "Glad to be here." is still shown | `text_visible` | — |
| 3 | Saved once: the article now has exactly 2 comments | `probe_equals` `comment_count` = 2 | Clause 0: the UI inserts the new comment without reloading, so seeing it proves neither that it was saved nor that it was saved only once |

### `favorite-article`
| # | Clause | Establishing check(s) | Not acceptable |
|---|---|---|---|
| 0 | The favorite button reads "Unfavorite Article" (after the reload step) | `text_in_target` on the banner's favorite button | The same check without the reload: the UI updates the button optimistically before the server confirms |
| 1 | The favorites count shows 1 (after the reload step) | `text_in_target` on the banner's favorites count | Same as clause 0 |

### `publish-article`
| # | Clause | Establishing check(s) | Not acceptable |
|---|---|---|---|
| 0 | The new article's page shows the title "Benchmarks we trust" | `url_matches` `^/article/benchmarks-we-trust-` (**case-insensitive**) and `text_in_target` on the banner `h1` | A lowercase or exact slug pattern (see findings) |
| 1 | Body reads "Every number cites the commit that produced it." | `text_in_target` on the article body | — |
| 2 | Tagged "testing" and "benchmarks" | `text_in_target` on the article's tag list, for each tag | — |
| 3 | Shown with jake as its author | `text_in_target` on the banner's author link | `text_visible` "jake": the header shows it too |
| 4 | Exactly one article is created: jake now has 6 | `probe_equals` `jake_article_count` = 6 (5 seeded) | `network_seen` `POST /api/articles`: it proves one request happened, not that only one article exists |

## Findings for M1 (locators and patterns)
- **Icon fonts add a character to accessible names.** Header links and several buttons begin with an Ionicons glyph: a private-use character from CSS `::before`, which Chromium includes in the name (`" New Article"`). Exact or anchored name locators match nothing. Match names as substrings, or strip private-use characters first.
- **Slugs keep the title's case and add a random suffix** (`/article/Benchmarks-we-trust-5d7dc78d`). URL checks are case-insensitive and never assert the suffix (README: content created during a run).
- **The article page renders the author meta twice** (banner and footer), and the favorite button twice. Targets must resolve to one element, so bind to the banner.
- **Order is not asserted anywhere.** Upstream breaks ties in creation time arbitrarily (LAB_NOTES, 2026-09-28).

## Evidence
On the clean app, a Playwright 1.63.0 walk-through of every spec (a throwaway script, not committed) evaluated each clause above with its listed check. It also recorded all four invariants (`console_errors`, `js_exceptions`, `http_5xx`, `broken_images`). **44 of 44 checks held:** 24 clause checks, including the date trap in `read-article` clause 2, and 20 invariant checks. The SHA and the date are in the pull request that added this file.
