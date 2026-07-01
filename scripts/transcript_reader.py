"""
transcript_reader.py — Reads transcript JSON files from Azure Blob Storage.

Never reads from local disk during execution.
Called by distiller.py in a loop over all blobs.

Parses the exact transcript schema:
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
  "messages": [{"role": "user/agent", "text": "..."}],
  "resolution_time_minutes": 22,
  "was_escalated": true,
  "had_false_start": true,
  "satisfaction_score": 4
}
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Iterator

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME    = os.getenv("AZURE_CONTAINER_NAME", "transcripts")
BLOB_PREFIX       = "historical_transcripts"

# Map raw ticket_type strings → integer types used in AgentState
TICKET_TYPE_MAP = {
    "access_identity":    1,
    "asset_provisioning": 2,
    "service_degradation": 3,
    "critical_outage":    4,
}


@dataclass
class ParsedTranscript:
    """Clean, normalised transcript ready for distillation."""
    transcript_id: str
    ticket_type_str: str          # raw string e.g. "access_identity"
    ticket_type_int: int          # mapped int 1-4
    channel: str                  # "chat" or "voice"
    employee_id: str
    agent_id: str
    user_tone: str                # polite / rude / slang / technical
    agent_style: str              # professional / dismissive / verbose
    timestamp: str
    conversation_text: str        # full conversation as plain text for LLM
    messages: list                # original messages list
    resolution_time_minutes: int
    was_escalated: bool
    had_false_start: bool
    satisfaction_score: int


def _format_conversation(messages: list) -> str:
    """
    Converts messages list into a readable plain-text conversation.
    The LLM distiller receives this string.

    Args:
        messages: List of {"role": "user"|"agent", "text": "..."} dicts.

    Returns:
        Formatted conversation string.
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
        transcript_id          = raw.get("transcript_id", "UNKNOWN"),
        ticket_type_str        = ticket_type_str,
        ticket_type_int        = ticket_type_int,
        channel                = raw.get("channel", "chat"),
        employee_id            = raw.get("employee_id", ""),
        agent_id               = raw.get("agent_id", ""),
        user_tone              = raw.get("user_tone", ""),
        agent_style            = raw.get("agent_style", ""),
        timestamp              = raw.get("timestamp", ""),
        conversation_text      = _format_conversation(messages),
        messages               = messages,
        resolution_time_minutes= raw.get("resolution_time_minutes", 0),
        was_escalated          = raw.get("was_escalated", False),
        had_false_start        = raw.get("had_false_start", False),
        satisfaction_score     = raw.get("satisfaction_score", 0),
    )


class TranscriptReader:
    """
    Reads and parses transcript JSON files from Azure Blob Storage.

    Usage:
        reader = TranscriptReader()
        for transcript in reader.iter_all():
            # process transcript
    """

    def __init__(self):
        if not CONNECTION_STRING:
            raise ValueError("AZURE_STORAGE_CONNECTION_STRING not set in .env")
        self._service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
        self._container_client = self._service_client.get_container_client(CONTAINER_NAME)

    def list_blob_names(self) -> list[str]:
        """
        List all transcript blob names in the container (excludes manifest).

        Returns:
            Sorted list of blob name strings.
        """
        blobs = self._container_client.list_blobs(name_starts_with=BLOB_PREFIX)
        names = [
            b.name for b in blobs
            if b.name.endswith(".json") and "__manifest" not in b.name
        ]
        return sorted(names)

    def read_one(self, blob_name: str) -> ParsedTranscript:
        """
        Download and parse a single transcript blob.

        Args:
            blob_name: Full blob name e.g. "historical_transcripts/_TXN-00001.json"

        Returns:
            ParsedTranscript dataclass.
        """
        blob_client = self._service_client.get_blob_client(
            container=CONTAINER_NAME,
            blob=blob_name,
        )
        raw_bytes = blob_client.download_blob().readall()
        raw_dict  = json.loads(raw_bytes.decode("utf-8"))
        return _parse_raw(raw_dict)

    def iter_all(self) -> Iterator[ParsedTranscript]:
        """
        Lazily iterate over all transcripts in Azure Blob.
        Memory-efficient — downloads and yields one at a time.

        Yields:
            ParsedTranscript for each file in the container.
        """
        blob_names = self.list_blob_names()
        logger.info("Found {} transcripts in Azure Blob.", len(blob_names))

        for blob_name in blob_names:
            try:
                yield self.read_one(blob_name)
            except json.JSONDecodeError as exc:
                logger.error("JSON parse error for {}: {}", blob_name, exc)
            except Exception as exc:
                logger.error("Failed to read {}: {}", blob_name, exc)

    def read_manifest(self) -> dict:
        """
        Read the __manifest.json file if it exists.

        Returns:
            Manifest dict or empty dict if not found.
        """
        try:
            return json.loads(
                self._service_client.get_blob_client(
                    container=CONTAINER_NAME,
                    blob=f"{BLOB_PREFIX}/__manifest.json",
                ).download_blob().readall().decode("utf-8")
            )
        except Exception:
            logger.warning("No manifest found in Azure Blob.")
            return {}
