from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import aiohttp

TOKEN = os.getenv("TELEGRAM_TOKEN")
assert TOKEN, "Нет TELEGRAM_TOKEN в .env"

TG_BASE = f"https://api.telegram.org/bot{TOKEN}"

async def main():
    offset = None
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        print(">> debug_updates started. Напиши в группу /ping или /top20")
        while True:
            try:
                params = {"timeout": 30}
                if offset:
                    params["offset"] = offset
                async with s.get(f"{TG_BASE}/getUpdates", params=params) as r:
                    data = await r.json()
                results = data.get("result", [])
                # Лог размера пачки апдейтов
                print("getUpdates ok:", len(results))
                for upd in results:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    text = (msg.get("text") or "").strip()
                    print("<< incoming:", chat_id, text)
                    # Мини-команда для теста
                    if text.startswith("/ping"):
                        async with s.post(
                            f"{TG_BASE}/sendMessage",
                            data={"chat_id": chat_id, "text": "pong"},
                        ) as r2:
                            ok = await r2.json()
                            print(">> replied:", ok)
            except Exception as e:
                print("loop error:", e)
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")

