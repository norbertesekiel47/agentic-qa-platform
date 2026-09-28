# ADR-0010: Clerk Organizations for identity and tenancy

- Status: Accepted
- Date: 2026-09-27

## Context
A multi-tenant SaaS needs sign-in, organizations, invites, roles, and a path toward enterprise features. Building organization management by hand is not where the portfolio's value lies.

## Options
1. **Amazon Cognito** — AWS-native, cheap; no organization concept; rough developer experience.
2. **WorkOS AuthKit** — B2B-oriented, strong SSO/SCIM path.
3. **Clerk** — built-in Organizations, excellent Next.js integration, prebuilt components.

## Decision
Clerk (author's choice). The API verifies Clerk session JWTs against Clerk's JWKS and maps the active organization claim to the Postgres RLS context. Clerk webhooks (Svix-signed) mirror organizations, users, and memberships into our database for joins and auditing.

## Consequences
- Identity lives outside AWS; the org-claim shape (session token v2) must be confirmed at implementation.
- Webhook processing must be idempotent (delivery-ID dedupe) and tolerate reordering.
- Roles in v1: `admin`, `member`, `viewer`; enterprise SSO/SCIM deferred to Clerk's enterprise features.
