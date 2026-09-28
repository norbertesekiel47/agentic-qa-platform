# ADR-0011: BYOK only, with KMS envelope encryption

- Status: Accepted
- Date: 2026-09-27

## Context
Hosted runs spend LLM tokens. With a model-agnostic product and multi-tenancy, someone must pay providers, and keys must be protected.

## Options
1. **Bring your own key (BYOK) only** — orgs store their provider keys; CI runs use keys from GitHub secrets.
2. Platform-paid — we pay providers, meter usage, bill via Stripe; financial and abuse exposure.
3. Both — BYOK default plus platform credits.

## Decision
Option 1 for v1; Stripe billing is a later phase. Keys are stored with **envelope encryption**: KMS `GenerateDataKey` produces a per-key AES-256-GCM data key; we store ciphertext, nonce, and the KMS-wrapped data key. Decryption occurs in the API only when a hosted run requests the key (run token), delivered over TLS, held in the runner's memory only. CI keys never reach our API.

## Consequences
- No financial exposure to tenant token spend.
- Strong SECURITY.md material: envelope encryption, rotation, redaction, isolation.
- The public demo runs on the author's key with strict rate limits.
- UI shows only `last4`; keys are write-only via the API.
