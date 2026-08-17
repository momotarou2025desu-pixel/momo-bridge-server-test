import asyncio
import contextlib
import hashlib
import os
import secrets
from datetime import datetime, timezone

import discord
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
import uvicorn

APP_NAME = "momo Bridge"
APP_VERSION = "0.5.1"
POSTING_NAME = "momotarou(AI)"
WRITER_WEBHOOK_NAME = "momo Bridge Writer"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "").strip()
DISCORD_ALLOWED_CHANNEL_IDS_RAW = os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "").strip()
DISCORD_ALLOWED_WRITE_CHANNEL_IDS_RAW = os.environ.get(
    "DISCORD_ALLOWED_WRITE_CHANNEL_IDS",
    DISCORD_ALLOWED_CHANNEL_IDS_RAW,
).strip()


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
ALLOWED_WRITE_CHANNEL_IDS, INVALID_WRITE_CHANNEL_IDS = parse_channel_ids(
    DISCORD_ALLOWED_WRITE_CHANNEL_IDS_RAW
)
MCP_ROUTE_TOKEN = (
    hashlib.sha256(BRIDGE_API_KEY.encode("utf-8")).hexdigest()[:32]
    if BRIDGE_API_KEY
    else "not-configured"
)
MCP_MOUNT_PATH = f"/mcp/{MCP_ROUTE_TOKEN}"

intents = discord.Intents.none()
intents.guilds = True

discord_client = discord.Client(intents=intents)
discord_task: asyncio.Task | None = None
writer_webhook_cache: dict[int, discord.Webhook] = {}

mcp = MCPServer("momo Bridge")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)
WRITE_CREATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
)


class SendMessageRequest(BaseModel):
    channel_id: str = Field(min_length=1, max_length=30)
    content: str = Field(min_length=1, max_length=2000)


@discord_client.event
async def on_ready():
    print(
        f"Discord connected as {discord_client.user} "
        f"({discord_client.user.id if discord_client.user else 'unknown'})"
    )
    print(f"Discord read allowlist contains {len(ALLOWED_CHANNEL_IDS)} channel(s).")
    print(f"Discord write allowlist contains {len(ALLOWED_WRITE_CHANNEL_IDS)} channel(s).")
    print(f"Discord posting name: {POSTING_NAME}")
    if BRIDGE_API_KEY:
        print("MCP endpoint configured with a private derived route token.")


async def start_discord_client():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN is not set; Discord connection will stay disabled.")
        return

    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    except Exception as exc:
        print(f"Discord connection failed: {type(exc).__name__}: {exc}")


def discord_status() -> dict:
    user = discord_client.user
    write_allowlist_inherited = not bool(
        os.environ.get("DISCORD_ALLOWED_WRITE_CHANNEL_IDS", "").strip()
    )
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
            "mcp_configured": bool(BRIDGE_API_KEY),
        },
        "writer": {
            "allowed_channel_count": len(ALLOWED_WRITE_CHANNEL_IDS),
            "allowlist_valid": not INVALID_WRITE_CHANNEL_IDS,
            "allowlist_source": "read_allowlist" if write_allowlist_inherited else "write_allowlist",
            "posting_name": POSTING_NAME,
            "manage_webhooks_required": True,
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


def ensure_discord_ready() -> None:
    if not DISCORD_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="DISCORD_BOT_TOKEN is not configured")
    if not discord_client.is_ready():
        raise HTTPException(status_code=503, detail="Discord client is not connected")


def ensure_reader_ready() -> None:
    ensure_discord_ready()
    if not ALLOWED_CHANNEL_IDS:
        raise HTTPException(
            status_code=503,
            detail="DISCORD_ALLOWED_CHANNEL_IDS is not configured",
        )


def ensure_writer_ready() -> None:
    ensure_discord_ready()
    if not ALLOWED_WRITE_CHANNEL_IDS:
        raise HTTPException(
            status_code=503,
            detail="No Discord channels are configured for writing",
        )


def ensure_channel_allowed(channel_id: int) -> None:
    if channel_id not in ALLOWED_CHANNEL_IDS:
        raise HTTPException(status_code=403, detail="Channel is not in the read allowlist")


def ensure_write_channel_allowed(channel_id: int) -> None:
    if channel_id not in ALLOWED_WRITE_CHANNEL_IDS:
        raise HTTPException(status_code=403, detail="Channel is not in the write allowlist")


async def fetch_discord_channel(channel_id: int):
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
    return channel


async def get_allowed_channel(channel_id: int):
    ensure_reader_ready()
    ensure_channel_allowed(channel_id)
    channel = await fetch_discord_channel(channel_id)
    if not hasattr(channel, "history"):
        raise HTTPException(status_code=400, detail="Channel does not support message history")
    return channel


async def get_write_channel(channel_id: int) -> discord.TextChannel:
    ensure_writer_ready()
    ensure_write_channel_allowed(channel_id)
    channel = await fetch_discord_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=400,
            detail="v0.5 posting supports Discord text channels only",
        )
    return channel


def serialize_message(message: discord.Message) -> dict:
    author = message.author
    return {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "author": str(author),
        "author_id": str(author.id),
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "jump_url": message.jump_url,
        "attachments": [
            {
                "id": str(attachment.id),
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
        "id": str(channel.id),
        "name": getattr(channel, "name", None),
        "type": str(getattr(channel, "type", "unknown")),
        "guild_id": str(guild.id) if guild else None,
        "guild_name": guild.name if guild else None,
    }


async def read_messages_impl(
    channel_id: int,
    limit: int = 20,
    before: int | None = None,
    after: int | None = None,
) -> dict:
    if before and after:
        raise HTTPException(status_code=400, detail="Use before or after, not both")
    channel = await get_allowed_channel(channel_id)
    before_object = discord.Object(id=before) if before else None
    after_object = discord.Object(id=after) if after else None

    try:
        messages = [
            serialize_message(message)
            async for message in channel.history(
                limit=limit,
                before=before_object,
                after=after_object,
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


async def search_messages_impl(
    channel_id: int,
    query: str,
    limit: int = 20,
    scan_limit: int = 200,
) -> dict:
    channel = await get_allowed_channel(channel_id)
    needle = query.casefold()
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
        "query": query,
        "scanned": scanned,
        "count": len(matches),
        "messages": matches,
    }


async def get_writer_webhook(channel: discord.TextChannel) -> discord.Webhook:
    cached = writer_webhook_cache.get(channel.id)
    if cached is not None and cached.token:
        return cached

    try:
        webhooks = await channel.webhooks()
    except discord.Forbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="Discord bot needs Manage Webhooks permission in this channel",
        ) from exc
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord API error while listing webhooks: {exc.status}",
        ) from exc

    bot_user = discord_client.user
    for webhook in webhooks:
        created_by_this_bot = (
            webhook.user is not None
            and bot_user is not None
            and webhook.user.id == bot_user.id
        )
        if webhook.name == WRITER_WEBHOOK_NAME and created_by_this_bot and webhook.token:
            writer_webhook_cache[channel.id] = webhook
            return webhook

    try:
        webhook = await channel.create_webhook(
            name=WRITER_WEBHOOK_NAME,
            reason="momo Bridge v0.5 posting webhook",
        )
    except discord.Forbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="Discord bot needs Manage Webhooks permission to create the posting webhook",
        ) from exc
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord API error while creating webhook: {exc.status}",
        ) from exc

    if not webhook.token:
        raise HTTPException(status_code=502, detail="Created Discord webhook has no execution token")

    writer_webhook_cache[channel.id] = webhook
    return webhook


async def send_message_impl(channel_id: int, content: str) -> dict:
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="Message content cannot be empty")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="Message content must be 2000 characters or fewer")

    channel = await get_write_channel(channel_id)
    webhook = await get_writer_webhook(channel)

    try:
        message = await webhook.send(
            content,
            username=POSTING_NAME,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
        )
    except discord.Forbidden as exc:
        writer_webhook_cache.pop(channel.id, None)
        raise HTTPException(
            status_code=403,
            detail="Discord refused webhook execution",
        ) from exc
    except discord.NotFound as exc:
        writer_webhook_cache.pop(channel.id, None)
        raise HTTPException(
            status_code=502,
            detail="Discord posting webhook was not found; retry to recreate it",
        ) from exc
    except (discord.HTTPException, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Discord webhook send failed: {type(exc).__name__}: {exc}",
        ) from exc

    return {
        "ok": True,
        "posting_name": POSTING_NAME,
        "channel": serialize_channel(channel),
        "message": serialize_message(message),
    }


@mcp.tool(annotations=READ_ONLY)
async def discord_status_tool() -> dict:
    """Verify Discord connectivity and show momo Bridge read/write configuration without exposing secrets."""
    return discord_status()


@mcp.tool(annotations=READ_ONLY)
async def list_allowed_channels() -> dict:
    """List only the Discord channels explicitly allowlisted for reading."""
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
                    "id": str(channel_id),
                    "status_code": exc.status_code,
                    "error": exc.detail,
                }
            )
    return {"channels": channels}


@mcp.tool(annotations=READ_ONLY)
async def read_channel_messages(
    channel_id: str,
    limit: int = 50,
    before: str | None = None,
    after: str | None = None,
) -> dict:
    """Read recent message history from one read-allowlisted Discord channel. Newest messages are returned first."""
    if not channel_id.isdigit():
        raise ValueError("channel_id must be a Discord numeric ID")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    before_id = int(before) if before and before.isdigit() else None
    after_id = int(after) if after and after.isdigit() else None
    if before and before_id is None:
        raise ValueError("before must be a Discord numeric message ID")
    if after and after_id is None:
        raise ValueError("after must be a Discord numeric message ID")
    return await read_messages_impl(int(channel_id), limit, before_id, after_id)


@mcp.tool(annotations=READ_ONLY)
async def get_discord_message(channel_id: str, message_id: str) -> dict:
    """Fetch one Discord message by ID from a read-allowlisted channel."""
    if not channel_id.isdigit() or not message_id.isdigit():
        raise ValueError("channel_id and message_id must be Discord numeric IDs")
    channel = await get_allowed_channel(int(channel_id))
    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound as exc:
        raise ValueError("Discord message was not found") from exc
    except discord.Forbidden as exc:
        raise ValueError("Discord bot cannot access this message") from exc
    except discord.HTTPException as exc:
        raise ValueError(f"Discord API error: {exc.status}") from exc
    return serialize_message(message)


@mcp.tool(annotations=READ_ONLY)
async def search_discord_messages(
    query: str,
    channel_ids: list[str] | None = None,
    limit: int = 25,
) -> dict:
    """Search message content only within read-allowlisted Discord channels."""
    if not query or len(query) > 200:
        raise ValueError("query must contain 1 to 200 characters")
    if limit < 1 or limit > 25:
        raise ValueError("limit must be between 1 and 25")

    requested = channel_ids or [str(cid) for cid in sorted(ALLOWED_CHANNEL_IDS)]
    results = []
    for raw_channel_id in requested:
        if not raw_channel_id.isdigit():
            raise ValueError("channel_ids must contain Discord numeric IDs")
        channel_id = int(raw_channel_id)
        ensure_channel_allowed(channel_id)
        result = await search_messages_impl(channel_id, query, limit=limit, scan_limit=500)
        results.extend(result["messages"])

    results.sort(key=lambda item: item["created_at"], reverse=True)
    return {
        "query": query,
        "count": len(results[:limit]),
        "messages": results[:limit],
    }


@mcp.tool(annotations=WRITE_CREATE)
async def send_discord_message(channel_id: str, content: str) -> dict:
    """Create one new plain-text Discord message as momotarou(AI) in a write-allowlisted text channel. Mentions are disabled."""
    if not channel_id.isdigit():
        raise ValueError("channel_id must be a Discord numeric ID")
    if not content or not content.strip():
        raise ValueError("content cannot be empty")
    if len(content) > 2000:
        raise ValueError("content must be 2000 characters or fewer")
    return await send_message_impl(int(channel_id), content)


mcp_http_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global discord_task
    if INVALID_CHANNEL_IDS:
        print(f"Ignoring invalid DISCORD_ALLOWED_CHANNEL_IDS values: {INVALID_CHANNEL_IDS}")
    if INVALID_WRITE_CHANNEL_IDS:
        print(
            "Ignoring invalid DISCORD_ALLOWED_WRITE_CHANNEL_IDS values: "
            f"{INVALID_WRITE_CHANNEL_IDS}"
        )

    async with mcp.session_manager.run():
        if DISCORD_BOT_TOKEN:
            discord_task = asyncio.create_task(start_discord_client())
        try:
            yield
        finally:
            if not discord_client.is_closed():
                await discord_client.close()
            if discord_task and not discord_task.done():
                discord_task.cancel()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


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
    writer = status["writer"]
    reader_state = (
        "Ready"
        if status["connected"]
        and reader["api_key_configured"]
        and reader["allowed_channel_count"] > 0
        else "Setup required"
    )
    writer_state = (
        "Ready"
        if status["connected"] and writer["allowed_channel_count"] > 0
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
<p>許可したDiscordチャンネルを読み取り、許可したチャンネルへ momotarou(AI) 名義で投稿するMCP対応版です。</p>
<p><b>Version:</b> {APP_VERSION}</p>
<p><b>UTC:</b> {now}</p>
<p><b>Discord:</b> {state}</p>
<p><b>Reader:</b> {reader_state} ({reader["allowed_channel_count"]} channels)</p>
<p><b>Writer:</b> {writer_state} ({writer["allowed_channel_count"]} channels)</p>
<p><b>Posting name:</b> {POSTING_NAME}</p>
<p><b>MCP:</b> {"Configured" if reader["mcp_configured"] else "Not configured"}</p>
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


@app.get("/mcp-info")
async def get_mcp_info(
    request: Request,
    _: None = Depends(require_bridge_key),
):
    base_url = str(request.base_url).rstrip("/")
    return {
        "name": "momo Bridge",
        "version": APP_VERSION,
        "transport": "streamable-http",
        "server_url": f"{base_url}{MCP_MOUNT_PATH}",
        "authentication_for_chatgpt": "none (private unguessable endpoint URL)",
        "warning": "Treat server_url as a secret. Anyone with this URL can call the configured read/write MCP tools.",
    }


@app.get("/discord/channels")
async def get_allowed_channels_rest(_: None = Depends(require_bridge_key)):
    return await list_allowed_channels()


@app.get("/discord/messages/{channel_id}")
async def get_messages_rest(
    channel_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    before: int | None = Query(default=None, ge=1),
    _: None = Depends(require_bridge_key),
):
    return await read_messages_impl(channel_id, limit, before, None)


@app.get("/discord/search/{channel_id}")
async def search_messages_rest(
    channel_id: int,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    scan_limit: int = Query(default=200, ge=1, le=500),
    _: None = Depends(require_bridge_key),
):
    return await search_messages_impl(channel_id, q, limit, scan_limit)


@app.post("/discord/send")
async def send_message_rest(
    body: SendMessageRequest,
    _: None = Depends(require_bridge_key),
):
    if not body.channel_id.isdigit():
        raise HTTPException(status_code=400, detail="channel_id must be a Discord numeric ID")
    return await send_message_impl(int(body.channel_id), body.content)


app.mount(MCP_MOUNT_PATH, mcp_http_app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
