"""Flux Cloudflare Python Worker: Telegram webhook + live Startup Intelligence.

This is the first Cloudflare-native migration slice. It intentionally does not
pretend that the original long-running Python app can run unchanged on Workers.
"""
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from js import fetch
from workers import WorkerEntrypoint, Response, handler

TELEGRAM_API = "https://api.telegram.org"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
USER_AGENT = "FluxStartupIntelligence/1.0 (Cloudflare Worker)"


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _fetch_json(url, headers=None, method="GET", body=None):
    init = {"method": method, "headers": headers or {"User-Agent": USER_AGENT}}
    if body is not None:
        init["body"] = json.dumps(body)
        init["headers"] = {**init["headers"], "Content-Type": "application/json"}
    response = await fetch(url, init)
    if not response.ok:
        raise RuntimeError(f"Upstream HTTP {response.status}: {url}")
    return await response.json()


async def _telegram_send(env, chat_id, text):
    token = env.TELEGRAM_BOT_TOKEN
    return await _fetch_json(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        {"Content-Type": "application/json"}, "POST",
        {"chat_id": chat_id, "text": str(text)[:4000], "disable_web_page_preview": True},
    )


async def _db_all(db, sql, params=()):
    statement = db.prepare(sql)
    if params:
        statement = statement.bind(*params)
    result = await statement.all()
    return list(result.results or [])


async def _db_run(db, sql, params=()):
    statement = db.prepare(sql)
    if params:
        statement = statement.bind(*params)
    return await statement.run()


async def _remember_message(db, chat_id, role, content):
    await _db_run(db,
        "INSERT INTO chat_history(chat_id, role, content, created_at) VALUES(?, ?, ?, ?)",
        (str(chat_id), role, content[:12000], _now()))
    await _db_run(db,
        "DELETE FROM chat_history WHERE chat_id=? AND id NOT IN "
        "(SELECT id FROM chat_history WHERE chat_id=? ORDER BY id DESC LIMIT 20)",
        (str(chat_id), str(chat_id)))


async def _chat_reply(env, chat_id, user_text):
    if not getattr(env, "ANTHROPIC_API_KEY", None):
        return ("Flux is connected, but the AI key is not configured yet. "
                "Add ANTHROPIC_API_KEY in Cloudflare Worker secrets.")
    await _remember_message(env.DB, chat_id, "user", user_text)
    rows = await _db_all(env.DB,
        "SELECT role, content FROM chat_history WHERE chat_id=? ORDER BY id DESC LIMIT 12",
        (str(chat_id),))
    messages = [{"role": r["role"], "content": r["content"]} for r in rows[::-1]]
    payload = {
        "model": getattr(env, "ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 700,
        "system": ("You are Flux, a concise personal AI assistant. Be helpful and clear. "
                   "Do not claim to have performed actions you cannot perform. "
                   "Email, calendar, flight and shopping integrations are not enabled in this first Cloudflare release."),
        "messages": messages,
    }
    data = await _fetch_json(ANTHROPIC_API,
        {"x-api-key": env.ANTHROPIC_API_KEY,
         "anthropic-version": "2023-06-01",
         "content-type": "application/json"}, "POST", payload)
    answer = "\n".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
    answer = answer.strip() or "I couldn't generate a response just now. Please try again."
    await _remember_message(env.DB, chat_id, "assistant", answer)
    return answer


async def _scan_reddit():
    queries = ["startup idea", "people need", "looking for a tool"]
    items = []
    for query in queries:
        url = "https://www.reddit.com/search.json?q=" + quote(query) + "&sort=new&limit=8"
        try:
            data = await _fetch_json(url, {"User-Agent": USER_AGENT})
            for child in data.get("data", {}).get("children", []):
                p = child.get("data", {})
                sid = str(p.get("id", ""))
                if sid:
                    items.append({"source":"reddit", "source_id":sid,
                        "title":p.get("title", "Untitled"),
                        "url":"https://www.reddit.com" + p.get("permalink", ""),
                        "summary":(p.get("selftext") or "")[:700],
                        "published_at":datetime.fromtimestamp(p.get("created_utc", 0), timezone.utc).isoformat()})
        except Exception as exc:
            print("Reddit scan failed:", str(exc))
    return items


async def _scan_github():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    queries = [f"stars:>20 created:>{cutoff}", f"topic:ai stars:>50 pushed:>{cutoff}"]
    items = []
    for query in queries:
        url = "https://api.github.com/search/repositories?q=" + quote(query) + "&sort=updated&order=desc&per_page=10"
        try:
            data = await _fetch_json(url, {"User-Agent": USER_AGENT, "Accept":"application/vnd.github+json"})
            for repo in data.get("items", []):
                items.append({"source":"github", "source_id":str(repo.get("id")),
                    "title":repo.get("full_name", "Repository"), "url":repo.get("html_url", ""),
                    "summary":(repo.get("description") or "")[:700],
                    "published_at":repo.get("pushed_at") or repo.get("created_at")})
        except Exception as exc:
            print("GitHub scan failed:", str(exc))
    return items


async def _scan_hn():
    items = []
    for query in ("startup", "Show HN", "looking for"):
        url = "https://hn.algolia.com/api/v1/search_by_date?query=" + quote(query) + "&tags=story&hitsPerPage=10"
        try:
            data = await _fetch_json(url, {"User-Agent": USER_AGENT})
            for hit in data.get("hits", []):
                sid = str(hit.get("objectID", ""))
                if sid:
                    items.append({"source":"hackernews", "source_id":sid,
                        "title":hit.get("title") or hit.get("story_title") or "Hacker News story",
                        "url":hit.get("url") or f"https://news.ycombinator.com/item?id={sid}",
                        "summary":(hit.get("story_text") or "")[:700],
                        "published_at":hit.get("created_at")})
        except Exception as exc:
            print("Hacker News scan failed:", str(exc))
    return items


async def startup_scan(env, chat_id=None):
    # Fetch live source data on each scan. If a source fails, do not label old rows as fresh.
    results = (await _scan_reddit()) + (await _scan_github()) + (await _scan_hn())
    fresh = []
    for item in results:
        await _db_run(env.DB,
            "INSERT OR IGNORE INTO startup_items(source, source_id, title, url, summary, published_at, first_seen_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (item["source"], item["source_id"], item["title"], item["url"],
             item["summary"], item.get("published_at"), _now()))
        inserted = await _db_all(env.DB, "SELECT changes() AS changed")
        if inserted and inserted[0]["changed"]:
            fresh.append(item)
    fresh.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    if chat_id is not None:
        if not fresh:
            await _telegram_send(env, chat_id,
                "Startup Intelligence checked live Reddit, GitHub and Hacker News sources. "
                "No new items were found (or sources returned no results).")
        else:
            lines = ["⚡ Flux Startup Intelligence — new live findings"]
            for item in fresh[:8]:
                lines.append(f"\n• [{item['source']}] {item['title']}\n{item['url']}")
                if item.get("summary"):
                    lines.append(item["summary"][:180])
            await _telegram_send(env, chat_id, "\n".join(lines)[:3900])
    return len(fresh)


async def _handle_update(env, update):
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("chat"):
        return
    chat_id = message["chat"]["id"]
    owner = str(getattr(env, "TELEGRAM_OWNER_ID", ""))
    if owner and str(message.get("from", {}).get("id", "")) != owner:
        return
    text = (message.get("text") or "").strip()
    await _db_run(env.DB, "INSERT OR REPLACE INTO settings(key,value) VALUES('owner_chat_id',?)", (str(chat_id),))
    if not text:
        await _telegram_send(env, chat_id, "I can read text messages in this first release. Voice, images and files are coming in a later migration stage.")
        return
    command = text.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        await _telegram_send(env, chat_id,
            "Hi! I'm Flux.\n\nCommands:\n/start — welcome\n/startup — latest saved discoveries\n"
            "/startup_scan — scan live Reddit, GitHub and Hacker News\n"
            "/startup_auto on|off — control automatic scans\n\nSend me a message to chat with AI.")
    elif command == "/startup_scan":
        await _telegram_send(env, chat_id, "Scanning live sources now. This may take a few seconds…")
        await startup_scan(env, chat_id)
    elif command == "/startup":
        rows = await _db_all(env.DB,
            "SELECT source, title, url, summary FROM startup_items ORDER BY first_seen_at DESC LIMIT 8")
        if not rows:
            await _telegram_send(env, chat_id, "No saved discoveries yet. Use /startup_scan to fetch live items.")
        else:
            out = ["⚡ Latest saved Startup Intelligence findings"]
            for row in rows:
                out.append(f"\n• [{row['source']}] {row['title']}\n{row['url']}")
            await _telegram_send(env, chat_id, "\n".join(out)[:3900])
    elif command == "/startup_auto":
        parts = text.split()
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            await _telegram_send(env, chat_id, "Use /startup_auto on or /startup_auto off")
        else:
            value = "1" if parts[1].lower() == "on" else "0"
            await _db_run(env.DB, "INSERT OR REPLACE INTO settings(key,value) VALUES('startup_auto',?)", (value,))
            await _telegram_send(env, chat_id, "Automatic Startup Intelligence scans are " + parts[1].lower() + ".")
    else:
        try:
            answer = await _chat_reply(env, chat_id, text)
        except Exception as exc:
            print("AI reply failed:", str(exc))
            answer = "I hit a temporary AI connection problem. Please try again shortly."
        await _telegram_send(env, chat_id, answer)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = request.url.split("?", 1)[0]
        if request.method == "GET" and path.endswith("/health"):
            return Response.json({"ok": True, "service": "Flux", "runtime": "Cloudflare Python Worker"})
        if request.method == "POST" and path.endswith("/telegram/webhook"):
            expected = getattr(self.env, "TELEGRAM_WEBHOOK_SECRET", "")
            received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not expected or received != expected:
                return Response("Unauthorized", status=401)
            try:
                update = await request.json()
                await _handle_update(self.env, update)
                return Response("ok")
            except Exception as exc:
                print("Webhook error:", str(exc))
                return Response("Webhook processing failed", status=500)
        return Response.json({"service":"Flux", "message":"Flux Worker is running", "health":"/health"})


@handler
async def on_scheduled(event, env, ctx):
    try:
        rows = await _db_all(env.DB, "SELECT value FROM settings WHERE key='startup_auto'")
        enabled = bool(rows and rows[0]["value"] == "1")
        if enabled:
            owner = await _db_all(env.DB, "SELECT value FROM settings WHERE key='owner_chat_id'")
            await startup_scan(env, int(owner[0]["value"]) if owner else None)
    except Exception as exc:
        print("Scheduled Startup Intelligence scan failed:", str(exc))
