# MediSlot — Authentication and Authorisation Design

## 1. Scope and roles

MediSlot uses custom authentication. The only application roles are `DOCTOR` and `PATIENT`; there is no admin role.

Doctor and patient registrations are separate. Email uniqueness is enforced independently within each role's table. The same email may be registered once as a doctor and once as a patient.

Hospitals are preconfigured. A doctor selects an existing hospital during registration; manual approval is not part of the planned flow.

## 2. Password policy

- Minimum password length: 10 characters.
- Password must contain at least one letter and one digit.
- Hash passwords using bcrypt with a work factor of at least 12.
- Never store plaintext passwords or return password hashes from APIs.

## 3. Access JWT

- Algorithm: HS256.
- Lifetime: 15 minutes.
- Required claims:
  - `sub`: user identifier.
  - `role`: `DOCTOR` or `PATIENT`.
  - `iat`: issued-at time.
  - `exp`: expiry time.
  - `jti`: unique token identifier.
  - `session_id`: associated server-side session identifier.

The signing secret is stored in AWS Systems Manager Parameter Store as `SecureString`. It must not be committed to the repository, exposed in logs, or embedded in client code.

## 4. Refresh token and session policy

Refresh tokens are opaque random credentials. The system stores a secure hash of the refresh token, not the plaintext token.

**Refresh tokens are reusable and are not rotated.** A session has a fixed seven-day lifetime from creation. Refreshing a session issues a new 15-minute access JWT but does not issue a replacement refresh token and does not extend the seven-day session expiry.

A dedicated DynamoDB `SessionsTable` stores:
- `sessionId` (partition key)
- `userId`
- `role`
- `refreshTokenHash`
- `status` (`ACTIVE` or `REVOKED`)
- `createdAt`
- `expiresAt`
- `revokedAt`
- `expiryTimestamp` for TTL cleanup

There is no `AccessTokensTable`. Access JWTs remain stateless; the design does not persist every access-token `jti`.

## 5. Login and refresh

### Login
1. Identify the account in the relevant role-specific table.
2. Verify password using bcrypt.
3. Create a session with a seven-day fixed expiry.
4. Generate an opaque refresh token and store its hash in `SessionsTable`.
5. Issue a 15-minute HS256 access JWT containing the session ID.
6. Return access and refresh credentials securely.

### Refresh
1. Receive the refresh token.
2. Resolve and validate the associated session.
3. Verify session status is `ACTIVE` and current time is before the fixed session expiry.
4. Verify the session user and role.
5. Issue a new access JWT with a fresh `iat`, `exp`, and `jti`, retaining the same `session_id`.
6. Return the new access token only. Do not rotate or replace the refresh token.

## 6. Authorizer

For protected API requests, the authorizer:
1. Extracts the bearer access JWT.
2. Verifies HS256 signature.
3. Validates required claims and expiry.
4. Validates role is an allowed role.
5. Reads the `SessionsTable` record using `session_id`.
6. Requires session status `ACTIVE`, session not expired, and matching `userId`/`sub` and role.
7. Returns user ID and role in authorizer context.

Authorizer result caching is configured with TTL `0` so a revoked session is not accepted from a cached authorisation decision.

## 7. Logout and revocation

Logout revokes the current session by conditionally changing its `status` to `REVOKED` and setting `revokedAt`. Subsequent protected requests using access JWTs associated with that session fail the authorizer's session-state check.

This is session-level revocation. The design does not require an access-token denylist or an `AccessTokensTable`.

## 8. Security outcomes

- Missing, malformed, invalid-signature, expired, or revoked credentials: `401 Unauthorized`.
- Authenticated user with the wrong role or without ownership permission: `403 Forbidden`.
- Refresh token for an expired or revoked session: reject; do not create a new session implicitly.

## 9. Configuration and operational safeguards

- Store signing secret and sensitive database credentials in SSM `SecureString`.
- Keep token lifetime and bcrypt work factor configurable where required by the project rules.
- Do not log access tokens, refresh tokens, passwords, or signing secrets.
- Use TLS for API traffic.
- Apply least-privilege IAM permissions to authentication functions.
- TTL is cleanup support; application code must check session expiry and status explicitly.
