---
runbook_id: RB-002
title: Asset Provisioning Issues
ticket_type: Asset Provisioning
severity: low
keywords: [laptop, hardware, broken, replacement, device, asset, screen cracked, won't boot, dead battery, peripheral, monitor, accessory, new hire, onboarding, software install, upgrade request, docking station, headset]
---

# Runbook: Asset Provisioning Issues

## Applies to
Any ticket involving a user's physical equipment or software entitlements — laptops, monitors, peripherals, software licenses — that is broken, missing, needs replacement, or needs to be newly issued.

## Diagnosis: identify the case

1. Query `assets_inventory` for the user's currently assigned device(s) — model, age, warranty status.
2. Determine: is this a repair/replacement of existing equipment, or a new provisioning request? This determines which workflow applies.
3. Branch based on what's actually needed:

### Case A — Confirmed hardware failure
**Example user phrasing:** "My laptop won't turn on," "I spilled coffee on my keyboard," "the screen is cracked."
**Signal:** Device won't power on/POST, physical damage (cracked screen, liquid damage), repeated hardware-tied BSOD/disk failure warnings.
**Fix:** Check inventory for replacement stock matching the user's device tier. If available, initiate hardware replacement workflow: reserve unit, reassign in `assets_inventory`, mark damaged unit pending repair/disposal. If no stock, log procurement request and issue a loaner if available.
**Verify:** New device shows as assigned to user in `assets_inventory` and user confirms it boots successfully.

### Case B — Software issue mistaken for hardware
**Example user phrasing:** "My laptop is broken, it won't start," "it's stuck on a black screen," "it just freezes constantly."
**Signal:** "Won't boot" or "frozen" reported but no physical damage — often resolvable without a swap.
**Fix:** Rule out a simple reimage/OS repair before treating as hardware failure. Walk through basic recovery (safe mode, boot repair, hard reset) first. Only escalate to Case A if recovery genuinely fails after these steps.
**Verify:** Device boots normally into the OS and user can log in.

### Case C — New hire / onboarding provisioning
**Example user phrasing:** "I'm starting Monday and don't have a laptop yet," "new employee needs a setup."
**Signal:** Request is for a brand-new device assignment tied to an onboarding date, not a repair.
**Fix:** Check inventory for the correct equipment tier for the role; initiate standard provisioning workflow (image, assign standard software bundle, ship or arrange pickup); coordinate delivery timeline against the start date — flag urgently if start date is within 48 hours and stock/imaging isn't ready.
**Verify:** Device is imaged, assigned, and delivered/available before the start date.

### Case D — Role change / upgrade request
**Example user phrasing:** "I need a more powerful laptop for this new role," "can I get a second monitor, I just moved to a hybrid setup."
**Signal:** Existing equipment is functional, but the user's role or work setup has changed and requires different/additional equipment.
**Fix:** Check whether the new role tier justifies the upgrade per company equipment policy; if approved, initiate provisioning workflow for the new/additional item; this is typically lower urgency than a repair since the user isn't fully blocked.
**Verify:** New equipment assigned in `assets_inventory`; confirm with user it meets the stated need.

### Case E — Peripheral / accessory request
**Example user phrasing:** "My mouse stopped working," "I need a headset for calls," "my docking station won't charge my laptop."
**Signal:** Request is for a monitor, dock, headset, mouse, or other accessory rather than the primary device.
**Fix:** Check accessory stock separately from primary device inventory; these typically don't require the full replacement workflow — direct fulfillment from accessory stock is usually sufficient and faster than a full asset reassignment.
**Verify:** Confirm accessory delivered/picked up and functioning.

### Case F — No replacement stock available
**Example user phrasing:** (follow-on from Case A/C) "It's been a week and I still don't have a laptop."
**Signal:** Diagnosis points to Case A or C, but inventory check shows no matching stock available.
**Fix:** Log an expedited procurement request; check for a loaner pool as a stopgap; communicate a realistic timeline to the user rather than leaving the ticket open with no update.
**Verify:** Loaner issued (if applicable) and procurement ticket has a tracked ETA.

### Case G — Software license / install request
**Example user phrasing:** "I need access to [software]," "can you install [tool] on my machine," "my license expired."
**Signal:** The issue is a software entitlement, not physical hardware — but often routed through the same asset/provisioning queue.
**Fix:** Check whether the software requires a paid license seat (verify budget/approval) versus a free internal tool (can self-serve install); push install via standard device management tooling rather than manual installation where possible.
**Verify:** Software confirmed installed/licensed and functional for the user.

### Case H — Offboarding asset recovery
**Example user phrasing:** (usually initiated by manager/HR, not the user) "This employee is leaving, please recover their equipment."
**Signal:** Ticket is about retrieving equipment from a departing employee, not issuing new equipment.
**Fix:** Schedule equipment return (shipping label or in-person drop-off); on return, wipe device per data security policy and update `assets_inventory` to mark it available for reissue; do not skip the wipe step even under time pressure.
**Verify:** Device returned, wiped, and inventory status updated to available.

## Escalation
If no replacement, loaner, or accessory stock exists and the user is fully blocked from working with no ETA available, escalate to the IT Asset Manager for expedited procurement approval.

## Resolution time target
Medium volume, low severity. Same-day initiation of the appropriate workflow is expected; the AHT target measures triage + workflow initiation, not physical delivery time. Cases A, B, and E are the most common and should triage fastest; Cases C and H are date-sensitive and should be flagged with their relevant deadline.
