import asyncio
import os
import secrets
from datetime import datetime, timezone

import discord
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
import uvicorn

APP_NAME = "momo Bridge Server Discord Reader"
APP_VERSION = "0.3.0"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "").strip()
DISCORD_ALLOWED_CHANNEL_IDS_RAW = os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "").strip()


def parse_channel_ids(raw: str) -> tuple[set[int], list[str]]:
    ids: set[int] = set()
    invalid: list[str] = []
    for item in raw.replace("\n", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if value.isdigit():
            ids.add(int(value))
        else:
            invalid.append(value)
    return ids, invalid


ALLOWED_CHANNEL_IDS, INVALID_CHANNEL_IDS = parse_channel_ids(DISCORD_ALLOWED_CHANNEL_IDS_RAW)

intents = discord.Intents.none()
intents.guilds = True

discord_client = discord.Client(intents=intents)
discord_task: asyncio.Task | None = None


@discord_client.event
async def on_ready():
    print(
        f"Discord connected as {discord_client.user} "
        f"({discord_client.user.id if discord_client.user else 'unknown'})"
    )
    print(f"Discord Reader allowlist contains {len(ALLOWED_CHANNEL_IDS)} channel(s).")


async def start_discord_client():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN is not set; Discord connection will stay disabled.")
        return

    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    except Exception as exc:
        print(f"Discord connection failed: {type(exc).__name__}: {exc}")


@app.on_event("startup")
async def startup_event():
    global discord_task
    if INVALID_CHANNEL_IDS:
        print(f"Ignoring invalid DISCORD_ALLOWED_CHANNEL_IDS values: {INVALID_CHANNEL_IDS}")
    if DISCORD_BOT_TOKEN:
        discord_task = asyncio.create_task(start_discord_client())


@app.on_event("shutdown")
async def shutdown_event():
    global discord_task
    if not discord_client.is_closed():
        await discord_client.close()
    if discord_task and not discord_task.done():
        discord_task.cancel()


def discord_status():
    user = discord_client.user
    return {
        "configured": bool(DISCORD_BOT_TOKEN),
        "connected": discord_client.is_ready(),
        "user": str(user) if user else None,
        "user_id": user.id if user else None,
        "guild_count": len(discord_client.guilds) if discord_client.is_ready() else 0,
        "reader": {
            "api_key_configured": bool(BRIDGE_API_KEY),
            "allowed_channel_count": len(ALLOWED_CHANNEL_IDS),
            "allowlist_valid": not INVALID_CHANNEL_IDS,
        },
    }


async def require_bridge_key(
    x_bridge_key: str | None = Header(default=None, alias="X-Bridge-Key"),
) -> None:
    if not BRIDGE_API_KEY:
        raise HTTPException(status_code=503, detail="BRIDGE_API_KEY is not configured")
    if not x_bridge_key:
        raise HTTPException(status_code=401, detail="X-Bridge-Key header is required")
    if not secrets.compare_digest(x_bridge_key, BRIDGE_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid bridge API key")


def ensure_reader_ready() -> None:
    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="DISCORD_BOT_TOKEN is not configured")
    if not discord_client.is_ready():
        raise HTTPException(status_code=503, detail="Discord client is not connected")
    if not ALLOWED_CHANNEL_IDS:
        raise HTTPException(
            status_code=503,
            detail="DISCORD_ALLOWED_CHANNEL_IDS is not configured",
        )


def ensure_channel_allowed(channel_id: int) -> None:
    if channel_id not in ALLOWED_CHANNEL_IDS:
        raise HTTPException(status_code=403, detail="Channel is not in the allowlist")


async def get_allowed_channel(channel_id: int):
    ensure_reader_ready()
    ensure_channel_allowed(channel_id)

    channel = discord_client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await discord_client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise HTTPException(status_code=404, detail="Discord channel was not found") from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403,
                detail="Discord bot cannot access this channel",
            ) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Discord API error: {exc.status}",
            ) from exc

    if not hasattr(channel, "history"):
        raise HTTPException(status_code=400, detail="Channel does not support message history")
    return channel


def serialize_message(message: discord.Message) -> dict:
    author = message.author
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "author": str(author),
        "author_id": author.id,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "jump_url": message.jump_url,
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "url": attachment.url,
            }
            for attachment in message.attachments
        ],
    }


def serialize_channel(channel) -> dict:
    guild = getattr(channel, "guild", None)
    return {
        "id": channel.id,
        "name": getattr(channel, "name", None),
        "type": str(getattr(channel, "type", "unknown")),
        "guild_id": guild.id if guild else None,
        "guild_name": guild.name if guild else None,
    }


@app.get("/", response_class=HTMLResponse)
async def home():
    now = datetime.now(timezone.utc).isoformat()
    status = discord_status()
    state = (
        "Connected"
        if status["connected"]
        else ("Configured / connecting" if status["configured"] else "Token not set")
    )
    reader = status["reader"]
    reader_state = (
        "Ready"
        if status["connected"]
        and reader["api_key_configured"]
        and reader["allowed_channel_count"] > 0
        else "Setup required"
    )
    badge = "● Discord OK" if status["connected"] else "● Server OK"
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Yu Gothic UI",sans-serif;background:#f6f8fb;color:#172033;margin:0}}
main{{max-width:760px;margin:64px auto;padding:0 20px}}
.card{{background:white;border:1px solid #e1e6ef;border-radius:20px;padding:30px;box-shadow:0 12px 36px rgba(15,23,42,.07)}}
.ok{{display:inline-block;background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;border-radius:999px;padding:7px 12px;font-weight:700}}
code{{background:#f1f5f9;padding:2px 6px;border-radius:6px}}
h1{{margin:14px 0 6px}}
p{{line-height:1.8}}
</style>
</head>
<body>
<main>
<div class="card">
<span class="ok">{badge}</span>
<h1>{APP_NAME}</h1>
<p>許可したDiscordチャンネルだけを読み取るテスト版です。</p>
<p><b>Version:</b> {APP_VERSION}</p>
<p><b>UTC:</b> {now}</p>
<p><b>Discord:</b> {state}</p>
<p><b>Reader:</b> {reader_state}</p>
<p><b>Allowed channels:</b> {reader["allowed_channel_count"]}</p>
<p><b>Status API:</b> <code>/discord/status</code></p>
<p><b>Health:</b> <code>/health</code></p>
</div>
</main>
</body>
</html>"""


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "version": APP_VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "discord": discord_status(),
    }


@app.get("/discord/status")
async def get_discord_status():
    return discord_status()


@app.get("/discord/channels")
async def get_allowed_channels(_: None = Depends(require_bridge_key)):
    ensure_reader_ready()
    channels = []
    for channel_id in sorted(ALLOWED_CHANNEL_IDS):
        try:
            channel = await get_allowed_channel(channel_id)
            channels.append({"ok": True, **serialize_channel(channel)})
        except HTTPException as exc:
            channels.append(
                {
                    "ok": False,
                    "id": channel_id,
                    "status_code": exc.status_code,
                    "error": exc.detail,
                }
            )
    return {"channels": channels}


@app.get("/discord/messages/{channel_id}")
async def get_messages(
    channel_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    before: int | None = Query(default=None, ge=1),
    _: None = Depends(require_bridge_key),
):
    channel = await get_allowed_channel(channel_id)
    before_object = discord.Object(id=before) if before else None

    try:
        messages = [
            serialize_message(message)
            async for message in channel.history(
                limit=limit,
                before=before_object,
                oldest_first=False,
            )
        ]
    except discord.Forbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="Discord bot lacks permission to read message history",
        ) from exc
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord API error: {exc.status}",
        ) from exc

    return {
        "channel": serialize_channel(channel),
        "count": len(messages),
        "messages": messages,
    }


@app.get("/discord/search/{channel_id}")
async def search_messages(
    channel_id: int,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    scan_limit: int = Query(default=200, ge=1, le=500),
    _: None = Depends(require_bridge_key),
):
    channel = await get_allowed_channel(channel_id)
    needle = q.casefold()
    matches = []
    scanned = 0

    try:
        async for message in channel.history(limit=scan_limit, oldest_first=False):
            scanned += 1
            if needle in message.content.casefold():
                matches.append(serialize_message(message))
                if len(matches) >= limit:
                    break
    except discord.Forbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="Discord bot lacks permission to read message history",
        ) from exc
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord API error: {exc.status}",
        ) from exc

    return {
        "channel": serialize_channel(channel),
        "query": q,
        "scanned": scanned,
        "count": len(matches),
        "messages": matches,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
