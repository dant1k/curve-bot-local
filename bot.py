# bot.py — Curve pools bot with real volumes/TVL/Base APY via curve-api v1
import os, ssl, certifi, asyncio, aiohttp
from aiohttp import ClientTimeout
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import ChatMigrated

load_dotenv(override=True)

TOKEN   = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
CHAINS  = [c.strip().lower() for c in (os.getenv("CHAINS") or "ethereum,arbitrum,polygon").split(",") if c.strip()]
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
INSECURE_SSL    = (os.getenv("INSECURE_SSL", "0") == "1")
HIDE_ZERO       = (os.getenv("HIDE_ZERO", "0") == "1")

if not TOKEN or ":" not in TOKEN:
    raise SystemExit(f"Bad TELEGRAM_TOKEN: {TOKEN!r}")
if not CHAT_ID:
    raise SystemExit("Set CHAT_ID in .env")

bot = Bot(TOKEN)

API = "https://api.curve.finance/v1"

HEADERS = {
    "User-Agent": "curve-bot/1.0 (+https://github.com/)",
    "Accept": "application/json",
}

SSL_VERIFIED = ssl.create_default_context(cafile=certifi.where())
SSL_INSECURE = ssl.create_default_context()
SSL_INSECURE.check_hostname = False
SSL_INSECURE.verify_mode = ssl.CERT_NONE

def ssl_ctx():
    return SSL_INSECURE if INSECURE_SSL else SSL_VERIFIED

def usd_short(x):
    try:
        v = float(x or 0)
        if v >= 1e12: return f"${v/1e12:.2f}T"
        if v >= 1e9:  return f"${v/1e9:.2f}B"
        if v >= 1e6:  return f"${v/1e6:.2f}M"
        if v >= 1e3:  return f"${v/1e3:.2f}k"
        return f"${v:,.0f}"
    except: return "-"

def pct(x):
    try: return f"{float(x)*100:.2f}%"
    except: return "0.00%"

REGISTRIES = [
    "main",
    "factory",
    "crypto",
    "factory-crypto",
    "factory-tricrypto",
    "factory-crvusd",
    "factory-stable-ng",
    "factory-twocrypto",
    "factory-eywa",
]

async def _get_json(session: aiohttp.ClientSession, url: str):
    t = ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        async with session.get(url, headers=HEADERS, ssl=ssl_ctx(), timeout=t) as r:
            if r.status >= 400:
                return {"error": f"{r.status}", "url": url}
            return await r.json()
    except Exception as e:
        # разок пробуем небезопасный SSL, если включен INSECURE_SSL=1 он и так используется
        if not INSECURE_SSL:
            try:
                async with session.get(url, headers=HEADERS, ssl=SSL_INSECURE, timeout=t) as r:
                    if r.status >= 400:
                        return {"error": f"{r.status}", "url": url}
                    return await r.json()
            except Exception as e2:
                return {"error": str(e2), "url": url}
        return {"error": str(e), "url": url}

async def fetch_volumes(session: aiohttp.ClientSession, chain: str) -> dict:
    """ /v1/getVolumes/{chain} → address -> volumeUSD """
    url = f"{API}/getVolumes/{chain}"
    js = await _get_json(session, url)
    pools = (js.get("data") or {}).get("pools") or []
    by = {}
    for p in pools:
        addr = (p.get("address") or "").lower()
        if not addr: continue
        by[addr] = float(p.get("volumeUSD") or 0)
    return by

async def fetch_base_apys(session: aiohttp.ClientSession, chain: str) -> dict:
    """ /v1/getBaseApys/{chain} → address -> weekly apy (в долях) """
    url = f"{API}/getBaseApys/{chain}"
    js = await _get_json(session, url)
    rows = (js.get("data") or {}).get("baseApys") or js.get("baseApys") or []
    by = {}
    for r in rows:
        addr = (r.get("address") or "").lower()
        if not addr: continue
        # weekly лучше согласуется с сайтом; это проценты, делим на 100 → доли
        apy_pct = r.get("latestWeeklyApyPcent")
        apy = float(apy_pct or 0)/100.0
        by[addr] = apy
    return by

async def fetch_pools(session: aiohttp.ClientSession, chain: str) -> dict:
    """
    /v1/getPools/{chain}/{registry} → собираем все poolData и отдаём:
    address -> {name, tvl, swap_url}
    """
    by = {}
    for reg in REGISTRIES:
        url = f"{API}/getPools/{chain}/{reg}"
        js = await _get_json(session, url)
        pdata = (js.get("data") or {}).get("poolData") or []
        for p in pdata:
            addr = (p.get("address") or "").lower()
            if not addr: continue
            name = p.get("name") or p.get("symbol") or addr[:8]
            tvl  = p.get("usdTotal") or p.get("usdTotalExcludingBasePool") or 0
            # ссылка: стараемся брать dex-link, иначе classic, иначе универсальный хэш
            swap_urls = ((p.get("poolUrls") or {}).get("swap") or [])
            link = None
            for s in swap_urls:
                if "dex/#" in s: link = s; break
            if not link and swap_urls:
                link = swap_urls[0]
            if not link:
                link = f"https://curve.fi/#/{chain}/pool/{addr}"
            # сохраняем/обновляем — если один и тот же пул есть в нескольких реестрах
            by.setdefault(addr, {})
            by[addr]["name"] = name
            by[addr]["tvl"]  = float(tvl or 0)
            by[addr]["link"] = link
    return by

async def build_snapshot(session: aiohttp.ClientSession, chain: str) -> list:
    """Склейка pools + volumes + base apys, возврат списка dict-ов"""
    vols, apys, pools = await asyncio.gather(
        fetch_volumes(session, chain),
        fetch_base_apys(session, chain),
        fetch_pools(session, chain),
    )
    items = []
    for addr, meta in pools.items():
        it = {
            "chain": chain,
            "address": addr,
            "name": meta.get("name"),
            "tvl": float(meta.get("tvl") or 0),
            "volume": float(vols.get(addr) or 0),
            "baseApy": float(apys.get(addr) or 0.0),
            "link": meta.get("link"),
        }
        if HIDE_ZERO and it["volume"] == 0 and it["tvl"] == 0:
            continue
        items.append(it)
    return items

def render_list(title: str, rows: list, sort_key: str, limit: int) -> str:
    rows = sorted(rows, key=lambda x: float(x.get(sort_key) or 0), reverse=True)[:limit]
    out = [f"{title} — Top {len(rows)} by {sort_key}"]
    n = 1
    for r in rows:
        out.append(
            f"{n}. {r['name']} — Base {pct(r['baseApy'])} — Volume {usd_short(r['volume'])} — TVL {usd_short(r['tvl'])}\n{r['link']}"
        )
        n += 1
    out.append("updated by curve-api v1")
    return "\n".join(out)

HELP = (
    "Commands:\n"
    "/ping\n"
    "/chains\n"
    "/<chain> [limit] [sort]   e.g. /ethereum 25 volume\n"
    "   sort = volume | tvl | apy\n"
    "/top <sort> all [limit]   e.g. /top volume all 40\n"
)

async def safe_send(text: str):
    global CHAT_ID
    try:
        await bot.send_message(CHAT_ID, text, disable_web_page_preview=True)
    except ChatMigrated as e:
        CHAT_ID = str(e.new_chat_id)
        await bot.send_message(CHAT_ID, text, disable_web_page_preview=True)

async def handle_chain(session, chain: str, limit: int, sort_key: str):
    data = await build_snapshot(session, chain)
    if not data:
        return f"⚠️ Нет данных по сети {chain} сейчас. Попробуй позже."
    return render_list(chain.capitalize(), data, sort_key, limit)

async def handle_top_all(session, limit: int, sort_key: str):
    all_rows = []
    for ch in CHAINS:
        rows = await build_snapshot(session, ch)
        for r in rows:
            r = dict(r)
            r["name"] = f"{ch} {r['name']}"
            all_rows.append(r)
    if not all_rows:
        return "⚠️ No data"
    return render_list("All Chains", all_rows, sort_key, limit)

async def main():
    print("✅ Bot running…")
    try:
        await safe_send("✅ bot online")
    except Exception:
        pass

    offset = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=POLL_INTERVAL)
                for u in updates or []:
                    offset = u.update_id + 1
                    m = getattr(u, "message", None)
                    if not m or not m.text:
                        continue
                    text = m.text.strip()
                    print(">", text)

                    if text == "/ping":
                        await safe_send("pong"); continue
                    if text == "/help":
                        await safe_send(HELP); continue
                    if text == "/chains":
                        await safe_send(", ".join(CHAINS)); continue

                    if text.startswith("/top"):
                        parts = text.split()
                        if len(parts) >= 3 and parts[1] in ("volume","tvl","apy") and parts[2] == "all":
                            limit = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 25
                            msg = await handle_top_all(session, limit, parts[1])
                            await safe_send(msg); continue
                        else:
                            await safe_send("Usage: /top <volume|tvl|apy> all [limit]"); continue

                    if text.startswith("/"):
                        parts = text.split()
                        chain = parts[0][1:].lower()
                        if chain in CHAINS:
                            limit = 25
                            sort_key = "volume"
                            if len(parts) >= 2 and parts[1].isdigit():
                                limit = max(1, min(50, int(parts[1])))
                            if len(parts) >= 3 and parts[2] in ("volume","tvl","apy"):
                                sort_key = parts[2]
                            msg = await handle_chain(session, chain, limit, sort_key)
                            await safe_send(msg); continue
                        else:
                            # подсказка
                            ex = " • /ethereum 25 tvl\n • /ethereum 25 volume\n • /polygon 40 tvl"
                            await safe_send(ex); continue
            except Exception as e:
                print("loop error:", e)
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())