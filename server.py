import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

APP_NAME = "momo Bridge Server Setup Test"
APP_VERSION = "0.1.1"

app = FastAPI(title=APP_NAME, version=APP_VERSION)


@app.get("/", response_class=HTMLResponse)
async def home():
    now = datetime.now(timezone.utc).isoformat()
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
<span class="ok">● Server OK</span>
<h1>{APP_NAME}</h1>
<p>クラウド上でHTTPS公開できるかだけを確認する最小テスト版です。</p>
<p><b>Version:</b> {APP_VERSION}</p>
<p><b>UTC:</b> {now}</p>
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
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
