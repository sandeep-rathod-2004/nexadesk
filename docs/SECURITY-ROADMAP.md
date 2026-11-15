# Security roadmap

A system prompt is not a security boundary. Security must be enforced by application code, infrastructure and data permissions.

Before real customer data:
- Authentication, secure sessions/token lifecycle and admin MFA
- Server-derived tenant identity and tenant-scoped authorization on every operation
- Secrets outside source control; rotate any exposed credentials
- TLS, secure headers, strict CORS and rate limiting
- Upload size/type validation, malware scanning and isolated document parsing
- Document ACLs enforced during retrieval, not only in prompts
- Treat user messages and retrieved documents as untrusted content
- Tool allowlists, per-tool authorization, argument validation and approval for consequential actions
- Output checks for secrets and unauthorized personal data; regex alone is insufficient
- Audit logs that avoid storing secrets and unnecessary sensitive content
- Retention/deletion policy, backups and incident response
- Dependency scanning, SAST, DAST, penetration testing and adversarial evaluations

Evaluate prompt injection, cross-tenant access, data exfiltration, unsupported claims, citation correctness and safe abstention. RAG and citations reduce risk but do not eliminate hallucinations.

Implemented foundation: Argon2id password hashing, short-lived JWT access tokens, workspace memberships and owner/member roles, and workspace/owner-scoped conversation and message queries. SQLite is the local default, and schema changes use Alembic. The frontend keeps the access token in memory only; signing out or reloading clears it.

Remaining limitations: no refresh/revocation flow, MFA, rate limiting, audit logging, upload pipeline, RAG, output policy checks, production monitoring, or deployment hardening. Keep it on localhost and use synthetic data only.
