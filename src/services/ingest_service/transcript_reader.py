"""
transcript_reader.py — Reads and parses transcript JSON files from Azure Blob Storage.

Never reads from local disk during execution.
Called by distiller.py in a loop over all 1050 transcripts.

Exact transcript schema supported:
{
  "transcript_id": "TXN-00001",
  "ticket_type": "access_identity",
  "channel": "chat",
  "employee_id": "EMP-0150",
  "agent_id": "EMP-0311",
  "user_tone": "slang",
  "agent_style": "professional",
  "conversation_length": 18,
  "timestamp": "2024-02-16T01:26:00Z",
  "messages": [{"role": "user"|"agent", "text": "..."}],
  "resolution_time_minutes": 22,
  "was_escalated": true,
  "had_false_start": true,
  "satisfaction_score": 4
}
"""

import os
from dataclasses import dataclass
from typing import Iterator

from loguru import logger
from dotenv import load_dotenv

from services.azure.blob_service_client import (
    iter_blobs_as_json,
    list_blob_names,
    read_blob_json,
)

load_dotenv()

BLOB_PREFIX = os.getenv("AZURE_BLOB_PREFIX", "historical_transcripts")

# Map raw ticket_type strings → integer types used in AgentState
TICKET_TYPE_MAP = {
    "access_identity":     1,
    "asset_provisioning":  2,
    "service_degradation": 3,
    "critical_outage":     4,
}


@dataclass
class ParsedTranscript:
    """Clean, normalised transcript ready for distillation."""
    transcript_id:           str
    ticket_type_str:         str     # raw e.g. "access_identity"
    ticket_type_int:         int     # mapped 1-4
    channel:                 str     # "chat" | "voice"
    employee_id:             str
    agent_id:                str
    user_tone:               str     # polite | rude | slang | technical
    agent_style:             str     # professional | dismissive | verbose
    timestamp:               str
    conversation_text:       str     # full conversation as plain text for LLM
    messages:                list    # original messages list
    resolution_time_minutes: int
    was_escalated:           bool
    had_false_start:         bool
    satisfaction_score:      int


def _format_conversation(messages: list) -> str:
    """
    Convert messages list into readable plain-text for the LLM distiller.

    Args:
        messages: List of {"role": "user"|"agent", "text": "..."} dicts.

    Returns:
        Formatted string with USER: / AGENT: prefixes.
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        text = msg.get("text", "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _parse_raw(raw: dict) -> ParsedTranscript:
    """
    Parse a raw transcript dict into a clean ParsedTranscript.

    Args:
        raw: Raw JSON dict loaded from Azure Blob.

    Returns:
        ParsedTranscript dataclass.
    """
    ticket_type_str = raw.get("ticket_type", "access_identity")
    ticket_type_int = TICKET_TYPE_MAP.get(ticket_type_str, 1)
    messages        = raw.get("messages", [])

    return ParsedTranscript(
        transcript_id           = raw.get("transcript_id", "UNKNOWN"),
        ticket_type_str         = ticket_type_str,
        ticket_type_int         = ticket_type_int,
        channel                 = raw.get("channel", "chat"),
        employee_id             = raw.get("employee_id", ""),
        agent_id                = raw.get("agent_id", ""),
        user_tone               = raw.get("user_tone", ""),
        agent_style             = raw.get("agent_style", ""),
        timestamp               = raw.get("timestamp", ""),
        conversation_text       = _format_conversation(messages),
        messages                = messages,
        resolution_time_minutes = raw.get("resolution_time_minutes", 0),
        was_escalated           = raw.get("was_escalated", False),
        had_false_start         = raw.get("had_false_start", False),
        satisfaction_score      = raw.get("satisfaction_score", 0),
    )


class TranscriptReader:
    """
    Reads and parses all transcript JSON files from Azure Blob Storage.
    Uses blob_client.py — never instantiates its own Azure client.

    Usage:
        reader = TranscriptReader()

        # Iterate all transcripts (memory-efficient, one at a time)
        for transcript in reader.iter_all():
            print(transcript.transcript_id, transcript.ticket_type_str)

        # Read one specific transcript
        t = reader.read_one("historical_transcripts/_TXN-00001.json")

        # Get dataset stats
        names = reader.list_all_names()
        print(f"Total transcripts: {len(names)}")
    """

    def list_all_names(self) -> list[str]:
        """
        List all transcript blob names (excludes manifest).

        Returns:
            Sorted list of blob name strings.
        """
        return list_blob_names(
            prefix  = BLOB_PREFIX,
            exclude = ["__manifest"],
        )

    def read_one(self, blob_name: str) -> ParsedTranscript:
        """
        Download and parse a single transcript blob.

        Args:
            blob_name: Full blob name e.g. "historical_transcripts/_TXN-00001.json"

        Returns:
            ParsedTranscript dataclass.
        """
        raw = read_blob_json(blob_name)
        return _parse_raw(raw)

    def iter_all(self) -> Iterator[ParsedTranscript]:
        """
        Lazily iterate over all transcripts in Azure Blob.
        Memory-efficient — downloads and parses one at a time.

        Yields:
            ParsedTranscript for each JSON file.
        """
        total = 0
        for blob_name, raw_dict in iter_blobs_as_json(
            prefix      = BLOB_PREFIX,
            exclude     = ["__manifest"],
            skip_errors = True,
        ):
            try:
                yield _parse_raw(raw_dict)
                total += 1
            except Exception as exc:
                logger.error("Failed to parse transcript from '{}': {}", blob_name, exc)

        logger.info("TranscriptReader: yielded {} transcripts total", total)

    def read_manifest(self) -> dict:
        """
        Read the __manifest.json file if it exists.

        Returns:
            Manifest dict or empty dict if not found.
        """
        try:
            return read_blob_json(f"{BLOB_PREFIX}/__manifest.json")
        except Exception:
            logger.warning("No manifest found in Azure Blob.")
            return {}