"""
db/supabase_client.py — Supabase client singleton.

Single place where the Supabase client is created.
All stores (st_store, lt_store, episodic_store) and MCP servers
call get_supabase() instead of creating their own clients.

Also exposes:
  - get_supabase_url() / get_supabase_key() for validation
  - check_connection()  for health checks
"""

import os
from functools import lru_cache

from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """
    Returns the singleton Supabase client.
    Built once, reused across all stores and MCP servers.

    Raises:
        EnvironmentError: If SUPABASE_URL or SUPABASE_KEY are not set.
    """
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")

    if not url:
        raise EnvironmentError("SUPABASE_URL not set in .env")
    if not key:
        raise EnvironmentError("SUPABASE_KEY not set in .env — use the service role key")

    client = create_client(url, key)
    logger.debug("Supabase client created (url={}...)", url[:30])
    return client


async def check_connection() -> bool:
    """
    Health check — verifies Supabase is reachable by pinging the tickets table.

    Returns:
        True if connected, False otherwise.
    """
    try:
        sb = get_supabase()
        sb.table("tickets").select("id").limit(1).execute()
        logger.debug("Supabase connection OK")
        return True
    except Exception as exc:
        logger.error("Supabase connection failed: {}", exc)
        return False


def get_supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "")


def get_supabase_key() -> str:
    return os.getenv("SUPABASE_KEY", "")