# bot.py — Curve Pools Telegram Bot (site-like view, filters, zero-filter)
# Команды:
#   /ping
#   /help
#   /chains
#   /<chain> [limit] [sort]        → /ethereum 25 volume
#   /vol [limit]  | /apy [limit] | /tvl [limit] | /rewards [limit]   (по умолчанию chain=ethereum)
#   /top <sort> all [limit]        → /top volume all 40
#
# Переменные в .env:
# TELEGRAM_TOKEN=xxxxxxxx:yyyyyyyyyyyy
# CHAT_ID=-100xxxxxxxxx
# CHAINS=ethereum,arbitrum,polygon
# POLL_INTERVAL=60
# REQUEST_TIMEOUT=25
# INSECURE_SSL=0          # 1 — игнорировать проверку SSL (как временный фолбэк)
# HIDE_ZERO=1             # скрывать пулы с нулевыми полями
# MIN_TVL=1000000         # фильтр по минимальному TVL в USD
# DEFAULT_CHAIN=ethereum  # для коротких команд /vol, /apy, /tvl, /rewards

import os, ssl, certifi, asyncio, aiohttp, math
from aiohttp import ClientTimeout
from datetime import datetime, timezone
from telegram import Bot, constants
from telegram.error import ChatMigrated
from dotenv import load_dotenv

# ---------- ENV ----------
load_dotenv(override=True)
TOKEN   = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
CHAINS  = [c.strip().lower() for c in (os.getenv("CHAINS") or "ethereum,arbitrum,polygon").split(",") if c.strip()]
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
INSECURE_SSL    = (os.getenv("INSECURE_SSL", "0") == "1")
HIDE_ZERO       = (os.getenv("HIDE_ZERO", "1") == "1")
MIN_TVL         = float(os.getenv("MIN_TVL", "1000000"))   # 1M по умолчанию
DEFAULT_CHAIN   = (os.getenv("DEFAULT_CHAIN") or "ethereum").lower()

if not TOKEN or ":" not in TOKEN:
    raise SystemExit("Bad TELEGRAM_TOKEN in .env")
if not CHAT_ID:
    raise SystemExit("Set CHAT_ID in .env")

bot = Bot(TOKEN)

# ---------- SSL ----------
SSL_CTX_VERIFIED = ssl.create_default_context(cafile=certifi.where())
SSL_CTX_INSECURE = ssl.create_default_context()
SSL_CTX_INSECURE.check_hostname = False
SSL_CTX_INSECURE.verify_mode = ssl.CERT_NONE
def _ctx():
    return SSL_CTX_INSECURE if INSECURE_SSL else SSL_CTX_VERIFIED

# ---------- API ----------
API = "https://api.curve.finance/v1"
HEADERS = {"User-Agent": "curve-bot/1.0"}

REGISTRIES = [
    "main", "factory", "crypto", "factory-crypto", "factory-crvusd",
    "factory-twocrypto", "factory-tricrypto", "factory-eywa", "factory-stable-ng"
]

# ---------- utils ----------
def usd_short(x):
    try:
        v = float(x)
    except Exception:
        return "-"
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.2f}M"
    if v >= 1e3:  return f"${v/1e3:.2f}k"
    return f"${v:.0f}"

def pct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        try:
            return f"{float(x)*100:.2f}%"
        except Exception:
            return "0.00%"

def safe_name(s: str) -> str:
    s = (s or "").replace("[", "").replace("]", "").replace("(", "").replace(")", "")
    return s.replace("_", "\\_")  # чтобы Markdown не ломался

def pool_link(chain: str, pool):
    # 1) предпочитаем «dex/#/...», если API отдал poolUrls
    urls = (pool.get("poolUrls") or {})
    for cat in ("swap", "deposit", "withdraw"):
        arr = urls.get(cat) or []
        for u in arr:
            if "dex/#" in u:
                return u
    # 2) иначе универсальная ссылка по адресу (не всегда откроет красивый slug, но работает)
    addr = pool.get("address") or pool.get("_addr") or ""
    return f"https://curve.fi/#/{chain}/pool/{addr}"

async def _get_json(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, headers=HEADERS, ssl=_ctx(), timeout=ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()
    except Exception:
        if not INSECURE_SSL:  # пробуем фолбэк на insecure только если он ещё не включён
            try:
                async with session.get(url, headers=HEADERS, ssl=SSL_CTX_INSECURE, timeout=ClientTimeout(total=REQUEST_TIMEOUT)) as r:
                    r.raise_for_status()
                    return await r.json()
            except Exception:
                return {"error": "fetch_failed"}
        return {"error": "fetch_failed"}

# ---------- fetch & merge ----------
async def fetch_chain_snapshot(session: aiohttp.ClientSession, chain: str):
    """
    Собираем:
      - TVL + имя + ссылки из /getPools/{registry}
      - Volume из /getVolumes/{chain}
      - Base vAPY из /getBaseApys/{chain}
      - CRV APR из поля gaugeCrvApy в /getPools
    Склейка по адресу пула (lowercase).
    """
    # 1) объёмы (по адресу)
    vols = await _get_json(session, f"{API}/getVolumes/{chain}")
    volumes = {}
    for r in (vols.get("data", {}).get("pools") or []):
        addr = (r.get("address") or "").lower()
        if addr:
            volumes[addr] = float(r.get("volumeUSD") or 0)

    # 2) базовые APY
    apys = await _get_json(session, f"{API}/getBaseApys/{chain}")
    base_apy = {}
    for r in (apys.get("data", {}).get("baseApys") or []):
        addr = (r.get("address") or "").lower()
        if addr:
            # берем weekly, если есть, иначе daily
            ap = r.get("latestWeeklyApyPcent")
            if ap is None:
                ap = r.get("latestDailyApyPcent")
            base_apy[addr] = float(ap or 0)

    # 3) справочник пулов по всем реестрам (TVL, имя, ссылки, CRV APR)
    pools_by_addr = {}
    for reg in REGISTRIES:
        data = await _get_json(session, f"{API}/getPools/{chain}/{reg}")
        for p in (data.get("data", {}).get("poolData") or []):
            addr = (p.get("address") or "").lower()
            if not addr:
                continue
            pools_by_addr.setdefault(addr, {})
            pools_by_addr[addr].update({
                "name": p.get("name") or p.get("symbol"),
                "tvl": float(p.get("usdTotal") or 0),
                "poolUrls": p.get("poolUrls") or {},
                "address": p.get("address"),
            })
            # CRV APR (в API это массив [min,max]; возьмём max)
            crv_arr = p.get("gaugeCrvApy") or []
            if isinstance(crv_arr, list) and crv_arr:
                pools_by_addr[addr]["crvApr"] = max([float(x or 0) for x in crv_arr])
            else:
                pools_by_addr[addr]["crvApr"] = 0.0

    # 4) финальная склейка
    result = []
    for addr, info in pools_by_addr.items():
        v = volumes.get(addr, 0.0)
        apy = base_apy.get(addr, 0.0)
        tvl = float(info.get("tvl") or 0)
        crv = float(info.get("crvApr") or 0.0)

        if HIDE_ZERO and (tvl <= 0 or (abs(apy) < 1e-9 and v <= 1.0)):   # фильтр «нулевых»
            continue
        if tvl < MIN_TVL:
            continue

        result.append({
            "address": addr,
            "name": info.get("name") or addr[:8],
            "tvl": tvl,
            "volume": v,
            "baseApy": apy,            # уже в процентах (из API)
            "rewardsApr": crv,         # пока только CRV; внешние инсентивы можно добавить из /getAllGauges
            "link": pool_link(chain, info),
        })
    return result

# ---------- presentation ----------
def format_pool_block(p, rank=None):
    name  = safe_name(p["name"])
    base  = pct(p["baseApy"])
    rew   = pct(p.get("rewardsApr") or 0.0)
    vol   = usd_short(p["volume"])
    tvl   = usd_short(p["tvl"])
    link  = p["link"]
    num   = f"{rank}. " if rank is not None else ""
    return (
        f"{num}*{name}*  \n"
        f"📊 *Base vAPY:* {base}  \n"
        f"🎯 *Rewards tAPR (CRV + Incentives):* {rew}  \n"
        f"💰 *Volume 24h:* {vol}  \n"
        f"💎 *TVL:* {tvl}  \n"
        f"🔗 {link}"
    )

def format_chain_table(chain:str, rows, limit:int, sort_key:str):
    keyf = {
        "volume":  lambda x: float(x.get("volume") or 0),
        "tvl":     lambda x: float(x.get("tvl") or 0),
        "apy":     lambda x: float(x.get("baseApy") or 0),
        "rewards": lambda x: float(x.get("rewardsApr") or 0),
    }.get(sort_key, lambda x: float(x.get("volume") or 0))
    rows = sorted(rows, key=keyf, reverse=True)[:limit]

    if not rows:
        return "⚠️ По текущим фильтрам ничего не найдено."

    head = f"*{chain.title()} — Top {len(rows)} by {sort_key}*"
    blocks = [head] + [format_pool_block(p, i+1) for i, p in enumerate(rows)]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks.append(f"_updated by curve-api v1 • {ts}_")
    return "\n\n".join(blocks)

# ---------- handlers ----------
HELP = (
    "Commands:\n"
    "/ping — check bot\n"
    "/chains — list chains\n"
    "/<chain> [limit] [sort] — e.g. /ethereum 25 volume\n"
    "   sort = volume | tvl | apy | rewards\n"
    "/vol [limit] — top by volume (default chain)\n"
    "/apy [limit] — top by base APY (default chain)\n"
    "/tvl [limit] — top by TVL (default chain)\n"
    "/rewards [limit] — top by CRV rewards (default chain)\n"
    "/top <sort> all [limit] — cross-chain top (same sort keys)\n"
)

async def safe_send(text: str, *, md: bool=True):
    global CHAT_ID
    try:
        await bot.send_message(
            CHAT_ID, text,
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except ChatMigrated as e:
        CHAT_ID = str(e.new_chat_id)
        await bot.send_message(
            CHAT_ID, text,
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

async def handle_chain(session, chain: str, limit=25, sort_key="volume"):
    snap = await fetch_chain_snapshot(session, chain)
    if not snap:
        return f"⚠️ Нет данных по сети *{chain}* сейчас. Попробуй позже."
    return format_chain_table(chain, snap, limit, sort_key)

async def handle_top_all(session, limit=25, sort_key="volume"):
    acc = []
    for ch in CHAINS:
        rows = await fetch_chain_snapshot(session, ch)
        for r in rows:
            r = dict(r)
            r["name"] = f"[{ch}] {r['name']}"
            acc.append(r)
    if not acc:
        return "⚠️ No data."
    return format_chain_table("All Chains", acc, limit, sort_key)

# ---------- main loop ----------
async def updates_loop():
    print("✅ Bot running…")
    default_hint = (
        f" • /{DEFAULT_CHAIN} 25 tvl\n"
        f" • /{DEFAULT_CHAIN} 25 volume\n"
        f" • /polygon 40 tvl"
    )
    print(default_hint)

    offset = 0
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        try:
            await safe_send("✅ bot online")
        except Exception:
            pass

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
                        await safe_send(", ".join(CHAINS), md=False); continue

                    # быстрые алиасы для дефолтной сети
                    if text.startswith("/vol") or text.startswith("/apy") or text.startswith("/tvl") or text.startswith("/rewards"):
                        parts = text.split()
                        limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 25
                        sort  = "volume" if text.startswith("/vol") else \
                                "apy" if text.startswith("/apy") else \
                                "tvl" if text.startswith("/tvl") else "rewards"
                        out = await handle_chain(session, DEFAULT_CHAIN, limit=limit, sort_key=sort)
                        await safe_send(out); continue

                    # /top volume all [limit]
                    if text.startswith("/top"):
                        parts = text.split()
                        if len(parts) >= 3 and parts[2] == "all":
                            sort  = parts[1] if parts[1] in ("volume","tvl","apy","rewards") else "volume"
                            limit = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 25
                            out = await handle_top_all(session, limit=limit, sort_key=sort)
                            await safe_send(out); continue
                        await safe_send("Usage: /top <volume|tvl|apy|rewards> all [limit]"); continue

                    # /<chain> [limit] [sort]
                    if text.startswith("/"):
                        parts = text.split()
                        chain = parts[0][1:].lower()
                        if chain in CHAINS:
                            limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 25
                            sort  = parts[2] if len(parts) >= 3 and parts[2] in ("volume","tvl","apy","rewards") else "volume"
                            out = await handle_chain(session, chain, limit=limit, sort_key=sort)
                            await safe_send(out); continue
            except Exception as e:
                print("loop error:", e)
            await asyncio.sleep(1.5)

if __name__ == "__main__":
    asyncio.run(updates_loop())