"""
inspect_transcripts.py — Utility to explore the transcript dataset before running
the full pipeline. Run this first to understand what you're working with.

Usage:
    python scripts/inspect_transcripts.py

Prints:
  - Manifest contents (if exists)
  - Ticket type distribution
  - Channel distribution (chat vs voice)
  - Tone and agent style breakdown
  - Sample of 3 transcripts
"""

import asyncio
from collections import Counter
from loguru import logger
from src.services.ingest_service.transcript_reader import TranscriptReader


async def main():
    reader = TranscriptReader()

    # ── Manifest ──────────────────────────────────────────────────────
    manifest = reader.read_manifest()
    if manifest:
        logger.info("MANIFEST: {}", manifest)
    else:
        logger.info("No manifest found.")

    # ── Collect stats ─────────────────────────────────────────────────
    ticket_types  = Counter()
    channels      = Counter()
    tones         = Counter()
    agent_styles  = Counter()
    escalated     = 0
    false_starts  = 0
    total         = 0
    sample        = []

    for transcript in reader.iter_all():
        total         += 1
        ticket_types[transcript.ticket_type_str]  += 1
        channels[transcript.channel]              += 1
        tones[transcript.user_tone]               += 1
        agent_styles[transcript.agent_style]      += 1
        if transcript.was_escalated:
            escalated += 1
        if transcript.had_false_start:
            false_starts += 1
        if total <= 3:
            sample.append(transcript)

    # ── Print report ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"TRANSCRIPT DATASET REPORT  (total: {total})")
    print("=" * 60)

    print("\nTicket Type Distribution:")
    for k, v in ticket_types.most_common():
        print(f"  {k:30s}: {v:4d}  ({v/total*100:.1f}%)")

    print("\nChannel Distribution:")
    for k, v in channels.most_common():
        print(f"  {k:20s}: {v:4d}  ({v/total*100:.1f}%)")

    print("\nUser Tone Distribution:")
    for k, v in tones.most_common():
        print(f"  {k:20s}: {v:4d}  ({v/total*100:.1f}%)")

    print("\nAgent Style Distribution:")
    for k, v in agent_styles.most_common():
        print(f"  {k:20s}: {v:4d}  ({v/total*100:.1f}%)")

    print(f"\nEscalated tickets   : {escalated} ({escalated/total*100:.1f}%)")
    print(f"Had false starts    : {false_starts} ({false_starts/total*100:.1f}%)")

    print("\n" + "=" * 60)
    print("SAMPLE TRANSCRIPTS (first 3)")
    print("=" * 60)
    for t in sample:
        print(f"\nID       : {t.transcript_id}")
        print(f"Type     : {t.ticket_type_str} (int: {t.ticket_type_int})")
        print(f"Channel  : {t.channel}")
        print(f"Tone     : {t.user_tone} | Agent: {t.agent_style}")
        print(f"Resolved : {t.resolution_time_minutes} mins | Escalated: {t.was_escalated}")
        print(f"Conversation preview:")
        preview_lines = t.conversation_text.split("\n")[:4]
        for line in preview_lines:
            print(f"  {line}")
        print("  ...")


if __name__ == "__main__":
    asyncio.run(main())
