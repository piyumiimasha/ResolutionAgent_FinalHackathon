# src/api/routers/voice.py
from fastapi import APIRouter, HTTPException, status
from livekit.api import AccessToken, VideoGrants
import os

router = APIRouter()

@router.post("/voice/token")
async def get_voice_token(body: dict):
    user_id = body.get("user_id", "anonymous")
    room    = body.get("room", "it-support")

    livekit_url    = os.getenv("LIVEKIT_URL")
    livekit_key    = os.getenv("LIVEKIT_API_KEY")
    livekit_secret = os.getenv("LIVEKIT_API_SECRET")

    missing = [
        name
        for name, value in (
            ("LIVEKIT_URL", livekit_url),
            ("LIVEKIT_API_KEY", livekit_key),
            ("LIVEKIT_API_SECRET", livekit_secret),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Missing LiveKit env vars: {', '.join(missing)}",
        )

    token = (
        AccessToken(livekit_key, livekit_secret)
        .with_identity(user_id)
        .with_name(user_id)
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {
        "token": token,
        "url": livekit_url,   # ← sends URL to browser from .env
    }