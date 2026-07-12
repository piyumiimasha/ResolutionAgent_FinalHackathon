# src/api/routers/voice.py
from fastapi import APIRouter
from livekit.api import AccessToken, VideoGrants
import os

router = APIRouter()

@router.post("/voice/token")
async def get_voice_token(body: dict):
    user_id = body.get("user_id", "anonymous")
    room    = body.get("room", "it-support")

    token = (
        AccessToken(
            os.getenv("LIVEKIT_API_KEY"),
            os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_identity(user_id)
        .with_name(user_id)
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {
        "token": token,
        "url": os.getenv("LIVEKIT_URL"),   # ← sends URL to browser from .env
    }