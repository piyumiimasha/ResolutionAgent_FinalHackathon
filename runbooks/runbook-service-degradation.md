---
runbook_id: RB-003
title: Service Degradation Issues
ticket_type: Service Degradation
severity: medium
keywords: [slow, api, latency, checkout, degraded, timeout, high response time, performance, sluggish, intermittent, spike, queue backlog, error rate elevated]
---

# Runbook: Service Degradation Issues

## Applies to
A specific service or API endpoint is responding slower than normal, intermittently erroring, or showing elevated error rates — but is not fully down. Users report delays or flakiness, not outright failure.

## Diagnosis: identify the case

1. Query `server_metrics` for the affected service: CPU, memory, request latency, request volume, error rate over the last 30-60 minutes.
2. Query `services` registry to identify the service's direct dependencies.
3. Check `incident_history` for prior occurrences with similar symptoms on this service.
4. Branch based on where the signal points:

### Case A — Downstream dependency saturated
**Example user phrasing:** "Checkout is taking forever," "the app is so slow today," "everything just spins for a while before loading."
**Signal:** The service itself looks fine, but a dependency (DB connection pool, cache, third-party API) shows elevated load or near-saturation in `server_metrics`.
**Fix:** Recommend/trigger scaling the saturated dependency, or restarting a stuck worker/connection pooler. Identify which specific dependency via the `services` dependency graph rather than guessing.
**Verify:** Latency on the primary service returns to baseline after the dependency recovers.

### Case B — Traffic spike without a clear bottleneck
**Example user phrasing:** "It's slow but only during the sale/launch," "seems fine now but was bad earlier."
**Signal:** Request volume is elevated across the board, no single dependency is saturated, latency scales roughly with load.
**Fix:** Recommend horizontal scaling of the affected service to absorb the load; if this correlates with a known event (sale, launch, marketing campaign), flag for pre-emptive scaling next time rather than only reacting.
**Verify:** Latency stabilizes as additional capacity comes online.

### Case C — Known recurring pattern
**Example user phrasing:** "This happens every night around the same time," "this is the third time this week."
**Signal:** Check `incident_history` first — if this service has had this exact symptom before (e.g. nightly batch job contention, a known N+1 query, a cron job overlapping with peak hours), it's a recurring case.
**Fix:** Surface the prior documented root cause and fix directly; don't re-diagnose from scratch. If no permanent fix was ever applied, flag this ticket for an engineering follow-up rather than just patching it again.
**Verify:** Confirm this occurrence matches the documented pattern's signature (same service, same time window, same metric profile) before applying the known fix.

### Case D — Slow but trending toward failure
**Example user phrasing:** "It's getting worse, now some requests are actually failing," "started slow, now I'm getting errors too."
**Signal:** Latency is climbing over time and starting to produce occasional timeouts/5xx errors, not just slowness.
**Fix:** Treat this as an early-warning sign. Apply Case A/B remediation immediately and monitor closely — if errors increase rather than stabilize, reclassify to Type 4 Critical Outage rather than waiting for a full outage to develop.
**Verify:** Error rate trend reverses after remediation; if it continues climbing, escalate per the Escalation section below.

### Case E — Single-region or single-instance degradation
**Example user phrasing:** "It's slow for me but my coworker in another office says it's fine."
**Signal:** Metrics show degradation isolated to one region, availability zone, or instance, not the service globally.
**Fix:** Check health of the specific instance/region in `server_metrics`; this is often a localized infra issue (one bad node, a regional network problem) rather than an application-level bug. Drain traffic from the unhealthy instance/region if load balancing allows, rather than attempting an application-level fix.
**Verify:** Confirm the affected region/instance metrics recover or that traffic has been successfully shifted away from it.

### Case F — Third-party / external API slowness
**Example user phrasing:** "Payments are slow," "the shipping calculator is taking ages."
**Signal:** `services` dependency graph shows the affected service calls an external third-party API, and that specific call is where the latency originates.
**Fix:** This is often outside your direct control — check the third-party's public status page; if there's no outage on their end, check if your integration has a retry/timeout misconfiguration making the slowness worse than it needs to be (e.g. no circuit breaker, retries piling up). Apply a circuit breaker or reduce timeout/retry aggressiveness as a mitigation while waiting on the third party.
**Verify:** Confirm whether the slowness resolves on the third party's end, or that your mitigation reduces user-facing impact even if the upstream issue persists.

### Case G — Resource leak (gradual degradation with no external trigger)
**Example user phrasing:** "It was fine this morning but has been getting slower all day for no reason."
**Signal:** `server_metrics` shows memory or connection count climbing steadily over hours with no corresponding traffic increase — classic resource leak signature.
**Fix:** This usually requires a restart as an immediate mitigation (clears the leaked resources) plus an engineering follow-up to find and fix the leak itself. Don't treat the restart as the permanent fix — flag for code-level investigation.
**Verify:** Resource metrics drop back to baseline immediately after restart; track whether they begin climbing again at a similar rate (confirms the leak is still present and just temporarily cleared).

## Escalation
If the service moves from "slow" to producing sustained errors/timeouts (full outage), reclassify as Type 4 Critical Outage and escalate immediately rather than continuing degradation-path remediation.

## Resolution time target
Low volume, medium severity. Investigation should complete within the 5-minute AHT target using metrics + procedural memory; remediation execution time may extend beyond that depending on the fix required (e.g. Case G's engineering follow-up is separate from the immediate mitigation).
