---
runbook_id: RB-004
title: Critical Outages
ticket_type: Critical Outages
severity: critical
keywords: [redis, oom, out of memory, auth-db, database down, login failure mass, connection refused, service down, outage, cache full, eviction, all users affected, site down, 500 errors everywhere, deploy broke, cascading failure]
---

# Runbook: Critical Outages

## Applies to
Any incident where a core service is fully down or failing for all/most users — not degraded, not single-user. These are rare, high blast-radius incidents. Voice escalation via LiveKit triggers automatically for this ticket type regardless of which case below applies, and DevOps clearance must be verified before any remediation action executes.

## Diagnosis: identify the case

1. Query `services` registry to confirm which service is down and map its dependency graph (blast radius — who else does this affect).
2. Query `server_metrics` for the affected host: memory, CPU, disk I/O, connection count.
3. Check `incident_history` for prior occurrences of this exact failure pattern.
4. Branch based on the failing component:

### Case A — Redis / cache out of memory
**Example user phrasing (often from monitoring/alerts rather than end users):** "Redis is throwing OOM errors," "cache is full and writes are failing," "prod-cache-01 connections refused."
**Signal:** `used_memory` at/near `maxmemory`, clients getting OOM or connection-refused errors from a cache instance, eviction count climbing.
**Fix:**
- If eviction policy is `noeviction`, switch to `allkeys-lru` (or appropriate policy) — stops write failures immediately.
- If a specific key namespace is consuming disproportionate memory, flush or set TTLs on that namespace.
- If memory doesn't release after flush, execute a controlled restart via Action MCP.
- If recurring (check `incident_history`), recommend permanent fix: scale instance memory or enforce stricter TTLs.
**Verify:** `used_memory` drops below threshold and dependent services' error rates return to zero.

### Case B — Auth/database down
**Example user phrasing:** "Nobody can log in," "the whole site says login failed for everyone," "auth-db is down."
**Signal:** Mass login failures across many users simultaneously, auth service logs show DB connection timeouts, `services` shows auth-db unreachable.
**Fix:**
- Connection pool exhaustion → increase max connections or restart the connection pooler.
- DB process crashed → restart the auth-db service.
- Resource exhaustion → free resources or fail over to a standby replica if available.
**Verify:** Confirm recovery with a test login flow end-to-end, not just a healthy metrics reading — a "healthy" metric can still mask an app-level auth bug.

### Case C — Generic service crash / down (not cache or auth)
**Example user phrasing:** "The whole app is throwing 500 errors," "[service] just went down right after the deploy."
**Signal:** A service is unresponsive or returning errors for all requests, doesn't match Case A or B specifically (e.g. a worker process died, a deploy broke the service).
**Fix:** Check for a recent deploy correlating with the crash — this is a rollback candidate. Restart the service via Action MCP; if it crash-loops after restart, treat as a bad deploy and roll back rather than repeatedly restarting, which wastes time and can mask the real cause.
**Verify:** Service health check passes consistently for several minutes post-fix (not just a momentary green status right after restart).

### Case D — Cascading dependency failure
**Example user phrasing:** "Three different things broke at the same time," "everything downstream of [service] is failing."
**Signal:** Multiple services are failing simultaneously, and the `services` dependency graph shows they all depend on one common upstream component.
**Fix:** Identify and fix the root upstream component first (often maps to Case A, B, or C underneath) rather than treating each downstream symptom separately. Don't apply fixes to downstream services individually — they'll recover once the root dependency is restored, and fixing symptoms separately wastes time and can create conflicting state.
**Verify:** Confirm the root component is healthy first, then confirm downstream services recover without separate intervention.

### Case E — Full regional/infrastructure outage
**Example user phrasing:** "Everything is down, not just one service," "the whole platform is unreachable."
**Signal:** Not a single service or dependency — `services` registry shows widespread failures across unrelated services simultaneously, often correlating with an infrastructure provider status page incident (cloud region outage, network provider issue).
**Fix:** Check the infrastructure provider's status page before attempting application-level fixes — if it's a provider-side outage, application restarts won't help and waste critical response time. If failover to another region/provider is available, initiate that; otherwise communicate status and ETA based on provider updates.
**Verify:** Provider status page confirms resolution, or failover region confirmed healthy and serving traffic.

### Case F — Data corruption / bad deploy affecting data integrity
**Example user phrasing:** "Things are loading but showing wrong/garbled data," "orders are showing the wrong totals for everyone."
**Signal:** Services are technically "up" (not erroring, not slow) but returning incorrect data at scale — a different failure mode than down/slow, and a restart will NOT fix this.
**Fix:** Do not attempt a generic restart — this can make data corruption worse or mask the issue temporarily. Identify the deploy or migration that introduced the issue and roll it back; if the bad data has already been written, this needs an engineering-led data remediation plan, not an automated runbook fix. Escalate to senior engineering immediately rather than attempting automated remediation.
**Verify:** Confirm with engineering that the rollback/fix is complete and spot-check data correctness before closing.

### Case G — Security-related outage (suspected breach or DoS)
**Example user phrasing:** "We're seeing a massive unexplained traffic spike and now everything is down," "this looks like it could be an attack."
**Signal:** Outage correlates with an abnormal traffic pattern (volumetric spike, unusual request patterns) rather than a normal capacity or code issue.
**Fix:** Do not treat this purely as a capacity problem — scaling up in response to an attack can be costly and ineffective. Escalate to security/DevOps immediately; apply rate-limiting or WAF rules as an immediate mitigation if available, but the investigation and decision-making here belongs with security on-call, not standard remediation steps.
**Verify:** Confirm with security team that mitigation is in place and traffic patterns have normalized before considering this resolved.

## Escalation
This entire ticket type is escalation by default — confirm DevOps/on-call clearance before any restart, flush, or failover action. If not resolved within 5 minutes of the remediation attempt, escalate to senior on-call/incident commander and consider a status-page update. Cases F and G specifically require human engineering/security judgment and should not be auto-remediated even if a plausible automated fix exists.

## Resolution time target
Critical severity — target AHT under 5 minutes from initial detection to remediation action taken, inclusive of voice escalation and clearance verification. Cases F and G may legitimately exceed this target since they require human decision-making — the AHT target there should be measured to "escalated to the right human," not "resolved."
