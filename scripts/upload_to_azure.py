"""
upload_to_azure.py — One-time script to upload all 1050 transcripts to Azure Blob Storage.

Usage:
    python scripts/upload_to_azure.py

After this runs, local files are never read again during execution.
Everything downstream (distiller, cag_populator) reads from Azure Blob.

Requirements in .env:
    AZURE_STORAGE_CONNECTION_STRING=
    AZURE_CONTAINER_NAME=transcripts
"""

import os
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.azure.blob_service_client import BlobServiceClient
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME    = os.getenv("AZURE_CONTAINER_NAME", "transcripts")
LOCAL_DATA_DIR    = Path("data/historical_transcripts")
BLOB_PREFIX       = "historical_transcripts"
MAX_WORKERS       = 10   # parallel uploads


def get_blob_client() -> BlobServiceClient:
    """Initialise Azure Blob Service client."""
    if not CONNECTION_STRING:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING not set in .env")
    return BlobServiceClient.from_connection_string(CONNECTION_STRING)


def ensure_container_exists(service_client: BlobServiceClient) -> None:
    """Create the container if it doesn't already exist."""
    container_client = service_client.get_container_client(CONTAINER_NAME)
    try:
        container_client.get_container_properties()
        logger.info("Container '{}' already exists.", CONTAINER_NAME)
    except Exception:
        container_client.create_container()
        logger.info("Created container '{}'.", CONTAINER_NAME)


def upload_file(service_client: BlobServiceClient, file_path: Path) -> dict:
    """
    Upload a single JSON file to Azure Blob Storage.

    Args:
        service_client: Initialised BlobServiceClient.
        file_path:      Local path to the JSON transcript file.

    Returns:
        Dict with file name and upload status.
    """
    blob_name = f"{BLOB_PREFIX}/{file_path.name}"
    try:
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME,
            blob=blob_name,
        )
        with open(file_path, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)
        return {"file": file_path.name, "status": "success", "blob": blob_name}
    except Exception as exc:
        logger.error("Failed to upload {}: {}", file_path.name, exc)
        return {"file": file_path.name, "status": "failed", "error": str(exc)}


def main():
    logger.info("Starting Azure Blob upload from {}", LOCAL_DATA_DIR)

    # Validate local directory
    if not LOCAL_DATA_DIR.exists():
        raise FileNotFoundError(f"Data directory not found: {LOCAL_DATA_DIR}")

    json_files = sorted(LOCAL_DATA_DIR.glob("*.json"))
    # Exclude manifest from transcript upload
    json_files = [f for f in json_files if f.name != "__manifest.json"]
    logger.info("Found {} transcript files to upload.", len(json_files))

    service_client = get_blob_client()
    ensure_container_exists(service_client)

    # ── Parallel upload ───────────────────────────────────────────────
    results = {"success": 0, "failed": 0, "failed_files": []}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(upload_file, service_client, f): f
            for f in json_files
        }
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result["status"] == "success":
                results["success"] += 1
            else:
                results["failed"] += 1
                results["failed_files"].append(result["file"])

            # Progress log every 50 files
            if i % 50 == 0 or i == len(json_files):
                logger.info(
                    "Progress: {}/{} | Success: {} | Failed: {}",
                    i, len(json_files), results["success"], results["failed"],
                )

    # ── Summary ───────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Upload complete.")
    logger.info("  Successful : {}", results["success"])
    logger.info("  Failed     : {}", results["failed"])
    if results["failed_files"]:
        logger.warning("  Failed files: {}", results["failed_files"])

    # Also upload the manifest if it exists
    manifest = LOCAL_DATA_DIR / "__manifest.json"
    if manifest.exists():
        r = upload_file(service_client, manifest)
        logger.info("Manifest upload: {}", r["status"])


if __name__ == "__main__":
    main()