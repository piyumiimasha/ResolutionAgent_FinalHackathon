"""
blob_client.py — Azure Blob Storage client singleton.

Single place where the BlobServiceClient is created and configured.
Used by:
  - ingest_service/transcript_reader.py  (read transcripts during distillation)
  - ingest_service/s3_uploader.py        (renamed: upload_to_azure.py)
  - scripts/upload_to_azure.py           (one-time upload)

Exposes:
  - get_blob_service()      → singleton BlobServiceClient
  - get_container_client()  → client for the transcripts container
  - check_connection()      → health check
  - list_blobs()            → list all blobs with optional prefix
  - read_blob_json()        → download + parse one JSON blob
  - read_blob_text()        → download raw text from a blob
  - blob_exists()           → check if a specific blob exists
  - get_blob_count()        → count blobs in container with optional prefix
"""

import os
import json
from functools import lru_cache
from typing import Iterator, Any

from azure.storage.blob import (
    BlobServiceClient,
    ContainerClient,
    BlobClient,
    BlobProperties,
)
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from loguru import logger
from dotenv import load_dotenv

from infastructure.config import get_config

load_dotenv()


# ── Client singletons ─────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_blob_service() -> BlobServiceClient:
    """
    Returns the singleton BlobServiceClient.
    Built once at first call, reused everywhere.

    Raises:
        EnvironmentError: If AZURE_STORAGE_CONNECTION_STRING is not set.
    """
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    if not connection_string:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION_STRING not set in .env\n"
            "Get this from Azure Portal → Storage Account → Access Keys"
        )

    client = BlobServiceClient.from_connection_string(connection_string)
    logger.debug("Azure BlobServiceClient initialised")
    return client


@lru_cache(maxsize=4)
def get_container_client(container_name: str | None = None) -> ContainerClient:
    """
    Returns a ContainerClient for the given container.
    Defaults to AZURE_CONTAINER_NAME from .env.

    Args:
        container_name: Override container name (optional).

    Returns:
        ContainerClient for the specified container.
    """
    name = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
    client = get_blob_service().get_container_client(name)
    logger.debug("Azure ContainerClient ready (container={})", name)
    return client


# ── Container management ──────────────────────────────────────────────────────

def ensure_container(container_name: str | None = None) -> None:
    """
    Create the container if it does not already exist.
    Safe to call multiple times — no-op if container exists.

    Args:
        container_name: Container to create. Defaults to AZURE_CONTAINER_NAME.
    """
    name   = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
    client = get_blob_service().get_container_client(name)
    try:
        client.get_container_properties()
        logger.debug("Container '{}' already exists.", name)
    except ResourceNotFoundError:
        client.create_container()
        logger.info("Created Azure container '{}'.", name)
    except Exception as exc:
        logger.error("Failed to check/create container '{}': {}", name, exc)
        raise


# ── Blob listing ──────────────────────────────────────────────────────────────

def list_blobs(
    prefix: str | None = None,
    container_name: str | None = None,
    extension: str = ".json",
) -> list[BlobProperties]:
    """
    List blobs in the container, optionally filtered by prefix and extension.

    Args:
        prefix:         Blob name prefix e.g. "historical_transcripts/".
        container_name: Override container. Defaults to AZURE_CONTAINER_NAME.
        extension:      Only return blobs ending with this extension.

    Returns:
        Sorted list of BlobProperties objects.
    """
    container = get_container_client(container_name)
    try:
        blobs = list(container.list_blobs(name_starts_with=prefix))
        if extension:
            blobs = [b for b in blobs if b.name.endswith(extension)]
        blobs.sort(key=lambda b: b.name)
        logger.debug(
            "Listed {} blobs (prefix={} ext={})",
            len(blobs), prefix, extension,
        )
        return blobs
    except Exception as exc:
        logger.error("Failed to list blobs (prefix={}): {}", prefix, exc)
        return []


def list_blob_names(
    prefix: str | None = None,
    container_name: str | None = None,
    extension: str = ".json",
    exclude: list[str] | None = None,
) -> list[str]:
    """
    Convenience wrapper — returns just the blob name strings.

    Args:
        prefix:         Blob name prefix.
        container_name: Override container.
        extension:      File extension filter.
        exclude:        List of blob names to exclude e.g. ["__manifest.json"].

    Returns:
        Sorted list of blob name strings.
    """
    exclude = exclude or []
    blobs   = list_blobs(prefix=prefix, container_name=container_name, extension=extension)
    return [
        b.name for b in blobs
        if not any(ex in b.name for ex in exclude)
    ]


def get_blob_count(
    prefix: str | None = None,
    container_name: str | None = None,
) -> int:
    """
    Count blobs matching a prefix. Useful for verifying upload completeness.

    Args:
        prefix:         Blob prefix to count.
        container_name: Override container.

    Returns:
        Integer count.
    """
    return len(list_blobs(prefix=prefix, container_name=container_name))


# ── Blob reading ──────────────────────────────────────────────────────────────

def read_blob_bytes(
    blob_name: str,
    container_name: str | None = None,
) -> bytes:
    """
    Download raw bytes from a blob.

    Args:
        blob_name:      Full blob name e.g. "historical_transcripts/_TXN-00001.json"
        container_name: Override container.

    Returns:
        Raw bytes content.

    Raises:
        ResourceNotFoundError: If blob does not exist.
    """
    service   = get_blob_service()
    container = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
    blob_client: BlobClient = service.get_blob_client(
        container = container,
        blob      = blob_name,
    )
    return blob_client.download_blob().readall()


def read_blob_text(
    blob_name: str,
    container_name: str | None = None,
    encoding: str = "utf-8",
) -> str:
    """
    Download and decode a blob as a text string.

    Args:
        blob_name:      Full blob name.
        container_name: Override container.
        encoding:       Text encoding (default utf-8).

    Returns:
        Decoded string content.
    """
    raw = read_blob_bytes(blob_name, container_name)
    return raw.decode(encoding)


def read_blob_json(
    blob_name: str,
    container_name: str | None = None,
) -> dict[str, Any]:
    """
    Download and JSON-parse a blob.

    Args:
        blob_name:      Full blob name.
        container_name: Override container.

    Returns:
        Parsed dict.

    Raises:
        json.JSONDecodeError: If blob content is not valid JSON.
        ResourceNotFoundError: If blob does not exist.
    """
    text = read_blob_text(blob_name, container_name)
    return json.loads(text)


def iter_blobs_as_json(
    prefix: str | None = None,
    container_name: str | None = None,
    exclude: list[str] | None = None,
    skip_errors: bool = True,
) -> Iterator[tuple[str, dict]]:
    """
    Lazily iterate over all JSON blobs in the container.
    Memory-efficient — downloads and yields one file at a time.

    Args:
        prefix:         Blob prefix to filter.
        container_name: Override container.
        exclude:        Blob names to skip (e.g. ["__manifest.json"]).
        skip_errors:    If True, log and skip bad blobs instead of raising.

    Yields:
        Tuple of (blob_name, parsed_dict) for each blob.
    """
    names = list_blob_names(
        prefix         = prefix,
        container_name = container_name,
        exclude        = exclude or ["__manifest"],
    )
    logger.info("Iterating {} JSON blobs from Azure Blob...", len(names))

    for blob_name in names:
        try:
            data = read_blob_json(blob_name, container_name)
            yield blob_name, data
        except json.JSONDecodeError as exc:
            logger.error("JSON parse error for blob '{}': {}", blob_name, exc)
            if not skip_errors:
                raise
        except ResourceNotFoundError:
            logger.warning("Blob not found (skipping): {}", blob_name)
        except Exception as exc:
            logger.error("Failed to read blob '{}': {}", blob_name, exc)
            if not skip_errors:
                raise


# ── Blob writing ──────────────────────────────────────────────────────────────

def upload_blob(
    blob_name: str,
    data: bytes | str,
    container_name: str | None = None,
    overwrite: bool = True,
    content_type: str = "application/json",
) -> bool:
    """
    Upload data to a blob.

    Args:
        blob_name:      Target blob name.
        data:           Bytes or string to upload.
        container_name: Override container.
        overwrite:      Overwrite if blob exists (default True).
        content_type:   MIME type for the blob.

    Returns:
        True on success, False on failure.
    """
    try:
        service   = get_blob_service()
        container = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
        blob_client = service.get_blob_client(container=container, blob=blob_name)

        if isinstance(data, str):
            data = data.encode("utf-8")

        from azure.storage.blob import ContentSettings
        blob_client.upload_blob(
            data,
            overwrite        = overwrite,
            content_settings = ContentSettings(content_type=content_type),
        )
        logger.debug("Uploaded blob: {}", blob_name)
        return True

    except Exception as exc:
        logger.error("Failed to upload blob '{}': {}", blob_name, exc)
        return False


# ── Utility ───────────────────────────────────────────────────────────────────

def blob_exists(blob_name: str, container_name: str | None = None) -> bool:
    """
    Check if a specific blob exists in the container.

    Args:
        blob_name:      Blob name to check.
        container_name: Override container.

    Returns:
        True if exists, False otherwise.
    """
    try:
        service   = get_blob_service()
        container = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
        blob_client = service.get_blob_client(container=container, blob=blob_name)
        blob_client.get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False
    except Exception:
        return False


# FIXED
async def check_connection() -> bool:
    """
    Health check — verifies Azure Blob is reachable.

    Returns:
        True if connected, False otherwise.
    """
    try:
        service = get_blob_service()
        # max_results removed — not supported in azure-storage-blob >= 12.x
        # consume just one item to trigger the actual network call
        next(iter(service.list_containers()), None)
        logger.debug("Azure Blob connection OK")
        return True
    except ServiceRequestError as exc:
        logger.error("Azure Blob network error: {}", exc)
        return False
    except Exception as exc:
        logger.error("Azure Blob connection failed: {}", exc)
        return False


def get_blob_url(blob_name: str, container_name: str | None = None) -> str:
    """
    Get the public URL for a blob (useful for logging/debugging).

    Args:
        blob_name:      Blob name.
        container_name: Override container.

    Returns:
        Full blob URL string.
    """
    service   = get_blob_service()
    container = container_name or os.getenv("AZURE_CONTAINER_NAME", "transcripts")
    return f"{service.url}{container}/{blob_name}"