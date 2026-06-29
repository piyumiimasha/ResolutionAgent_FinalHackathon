---
runbook_id: RB-001
title: Access & Identity Issues
ticket_type: Access & Identity
severity: low
keywords: [vpn, access, login, authentication, reset, mfa, locked out, connection refused, timeout, credentials rejected, account locked, sso, password, expired, 2fa, otp, badge access, permission denied]
---

# Runbook: Access & Identity Issues

## Applies to
Any ticket where a single user cannot access a system they normally have access to — VPN, SSO, internal tools, shared drives, badge/physical access systems — due to a credential, session, permission, or account-status problem. This covers individual access failures, not platform-wide outages (see Critical Outages runbook for that).

## Diagnosis: identify the case

1. Query the user's account record for status (active / locked / suspended) and group/role membership.
2. Check `services` registry for the relevant gateway/auth service health — rule out a service-side outage before treating this as a user-level issue.
3. Ask or infer: is this affecting one user or many? One user → continue below. Many users → escalate to Critical Outages immediately.
4. Branch based on what you find:

### Case A — Session or token expired
**Example user phrasing:** "I keep getting logged out," "it says my session expired," "VPN disconnects every hour."
**Signal:** Account is active, service is healthy, error mentions "session expired" or "token invalid."
**Root cause:** Token TTL reached, or client cached an old token after a policy change shortened session length.
**Fix:** Force-expire the stale session server-side, issue a fresh token, instruct user to fully quit and relaunch the client (not just reconnect) so it picks up the new token.
**Verify:** Confirm new session timestamp in auth logs and that user can reach an internal resource.

### Case B — MFA / 2FA device issue
**Example user phrasing:** "My authenticator code isn't working," "I lost my phone and can't get my OTP," "it says invalid code every time."
**Signal:** Error specifically references MFA/2FA rejection or device not recognized.
**Fix:**
- If TOTP codes are consistently rejected but the device is the right one → likely clock drift; have user resync their authenticator app's time.
- If device was lost/replaced → re-enroll a new MFA device only after verifying identity through a secondary channel (manager confirmation or ID check per company policy).
- If user has a backup code → walk them through using it as a temporary bridge while re-enrolling.
**Verify:** Confirm new device shows as enrolled and user completes a successful MFA challenge.

### Case C — Account locked or access revoked
**Example user phrasing:** "It says my account is locked," "I can't get in and I didn't change my password," "I used to have access to this folder and now I don't."
**Signal:** Account status shows locked, suspended, or group membership recently changed/expired (e.g. offboarding, role change, expired contractor access).
**Fix:** If lockout is due to a failed-attempt threshold, unlock after identity verification. If access was intentionally revoked (policy, offboarding, role change), do NOT override — this is expected behavior; inform the user and route to their manager rather than restoring access yourself.
**Verify:** Confirm account status flips to active in the identity system, or confirm with the user's manager that revocation was unintended before restoring.

### Case D — Certificate or client misconfiguration
**Example user phrasing:** "VPN won't connect, it mentions a certificate error," "my laptop got reimaged and now VPN doesn't work."
**Signal:** VPN client shows certificate mismatch/expired errors specifically (not session or MFA).
**Fix:** Regenerate and push a new client certificate; confirm client config matches current gateway requirements (especially after a reimage or OS update).
**Verify:** Client connects and certificate expiry date in the new cert is in the future.

### Case E — Password reset / forgotten password
**Example user phrasing:** "I forgot my password," "can you reset my password," "the reset link isn't working."
**Signal:** User explicitly requests a reset, or repeated login failures with "invalid password" (not locked yet).
**Fix:** Trigger self-service reset flow if available; if reset email isn't arriving, check spam/email delivery rather than assuming account issue; if user is below the lockout threshold, a successful reset avoids escalation entirely.
**Verify:** User confirms successful login with new password.

### Case F — Permission/role mismatch (not a login failure)
**Example user phrasing:** "I can log in but I can't see the X folder/tool I need," "access denied when I try to open this."
**Signal:** Authentication succeeds, but a specific resource returns permission-denied. This is a different failure mode than not being able to log in at all — don't treat it as a VPN/session issue.
**Fix:** Check group/role membership against the resource's required permission group. If the user genuinely should have access (role change, project assignment), add them to the correct group. If unclear, route to the resource owner for an access decision rather than self-approving.
**Verify:** Confirm group membership updated and user can access the resource.

### Case G — Badge / physical access issue
**Example user phrasing:** "My badge isn't working at the office door," "I can't get into the server room."
**Signal:** Physical access system, not a software login — separate system from VPN/SSO but often routed through the same IT ticket queue.
**Fix:** Check badge system for deactivation/expiry; reissue or reactivate badge after identity verification; for server room or restricted-area access specifically, confirm the user has the required security clearance before reactivating, don't auto-approve.
**Verify:** Test badge swipe succeeds at the relevant door/reader.

### Case H — Third-party / federated SSO failure
**Example user phrasing:** "I can't log into [external tool] using my company login," "it redirects me back to login and just loops."
**Signal:** Error occurs specifically when authenticating into a third-party app via SSO, not the core internal systems.
**Fix:** Check the SSO/identity provider integration status for that specific app — this is often an app-side configuration issue (expired SAML certificate, misconfigured redirect URL) rather than a user account problem. Escalate to the app owner if the integration itself is broken rather than retrying user-side fixes repeatedly.
**Verify:** Confirm successful login to the third-party app post-fix.

## Escalation
If the `services` health check in step 2 shows the gateway/auth service itself is degraded — i.e. multiple users are affected, not just this one — this is NOT a Type 1 ticket. Reclassify as a Type 4 Critical Outage and escalate immediately.

## Resolution time target
Under 5 minutes. High volume, low severity — Cases A, B, and E are common enough to resolve via the CAG cache layer without invoking the full agent swarm. Cases C, D, F, G, and H more often need L1/L2 investigation since they involve account-status, permission, or config checks that vary per user.
