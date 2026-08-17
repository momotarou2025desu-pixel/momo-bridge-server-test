import asyncio
import os
from datetime import datetime, timezone

import discord
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

APP_NAME = "momo Bridge Server Discord Test"
APP_VERSION = "0.2.0"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

intents = discord.Intents.none()
intents.guilds = True

discord_client = discord.Client(intents=intents)
discord_task: asyncio.Task | None = None


@discord_client.event
async def on_ready():
    print(f"Discord connected as {discord_client.user} ({discord_client.user.id if discord_client.user else 'unknown'})")


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
    }


@app.get("/", response_class=HTMLResponse)
async def home():
    now = datetime.now(timezone.utc).isoformat()
    status = discord_status()
    state = "Connected" if status["connected"] else ("Configured / connecting" if status["configured"] else "Token not set")
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
<p>Render上のHTTPSサーバーからDiscord Botへ接続できるかを確認するテスト版です。</p>
<p><b>Version:</b> {APP_VERSION}</p>
<p><b>UTC:</b> {now}</p>
<p><b>Discord:</b> {state}</p>
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
