# bot.py — Curve Pools Telegram Bot (robust, SSL fallback, compact output)
# Команды:
# /ping
# /help
# /chains
# /<chain> [limit] [sort]  → /ethereum 25 volume  | sort: volume|tvl|apy|rewards
# /top volume all [limit]  → сводный топ по всем сетям
#
# .env:
# TELEGRAM_TOKEN=xxxxxxxx:yyyyyyyyyyyyyyyy
# CHAT_ID=-100xxxxxxxxx
# POLL_INTERVAL=60
# REQUEST_TIMEOUT=25
# CHAINS=ethereum,arbitrum,polygon
# INSECURE_SSL=0       # поставить 1, чтобы всегда игнорировать верификацию (в крайнем случае)

import os, asyncio, aiohttp, ssl, certifi, re
from aiohttp import ClientTimeout
from aiohttp.client_exceptions import ClientConnectorCertificateError
from dotenv import load_dotenv
from telegram import Bot, constants
from telegram.error import ChatMigrated

# ---------- ENV ----------
load_dotenv(override=True)
TOKEN   = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
CHAINS  = [c.strip() for c in (os.getenv("CHAINS") or "ethereum,arbitrum,polygon").split(",") if c.strip()]
INSECURE_SSL = (os.getenv("INSECURE_SSL", "0") == "1")

if not TOKEN or ":" not in TOKEN:
    raise SystemExit(f"Bad TELEGRAM_TOKEN: {repr(TOKEN)}")
if not CHAT_ID:
    raise SystemExit("Set CHAT_ID in .env")

bot = Bot(TOKEN)

# ---------- SSL contexts ----------
SSL_CTX_VERIFIED = ssl.create_default_context(cafile=certifi.where())
SSL_CTX_INSECURE = ssl.create_default_context()
SSL_CTX_INSECURE.check_hostname = False
SSL_CTX_INSECURE.verify_mode = ssl.CERT_NONE

# ---------- Curve endpoints (order matters) ----------
CURVE_CANDIDATES = {
    "ethereum": [
        "https://api.curve.finance/api/getPools/ethereum/main",
        "https://api.curve.finance/api/getPools/ethereum/crypto",
        "https://api.curve.finance/api/getPools/ethereum/factory",
        "https://api.curve.finance/api/getPools/ethereum/factory-crypto",
        "https://api.curve.finance/api/getPools/ethereum/factory-tricrypto",
        "https://api.curve.finance/api/getPools/ethereum/factory-crvusd",
        "https://api.curve.fi/api/getPools/ethereum/main",
        "https://api.curve.fi/api/getPools/ethereum/crypto",
        "https://api.curve.fi/api/getPools/ethereum/factory",
        "https://api.curve.finance/v1/getFactoryAPYs/ethereum/1",
        "https://api.curve.fi/api/getFactoryAPYs?chain=ethereum",
    ],
    "arbitrum": [
        "https://api.curve.finance/getPools/arbitrum/main",
        "https://api.curve.finance/getPools/arbitrum/crypto",
        "https://api.curve.finance/getPools/arbitrum/factory",
        "https://api.curve.fi/api/getPools/arbitrum/main",
        "https://api.curve.fi/api/getPools/arbitrum/crypto",
        "https://api.curve.fi/api/getPools/arbitrum/factory",
        "https://api.curve.finance/v1/getFactoryAPYs/arbitrum/1",
        "https://api.curve.fi/api/getFactoryAPYs?chain=arbitrum",
    ],
    "polygon": [
        "https://api.curve.finance/getPools/polygon/main",
        "https://api.curve.finance/getPools/polygon/crypto",
        "https://api.curve.finance/getPools/polygon/factory",
        "https://api.curve.fi/api/getPools/polygon/main",
        "https://api.curve.fi/api/getPools/polygon/crypto",
        "https://api.curve.fi/api/getPools/polygon/factory",
        "https://api.curve.finance/v1/getFactoryAPYs/polygon/1",
        "https://api.curve.fi/api/getFactoryAPYs?chain=polygon",
    ],
    # добавишь при желании:
    # "optimism": [...], "base": [...], "avalanche": [...], "fantom": [...], "bsc": [...], "gnosis": [...]
}

# ---------- helpers ----------
MD_LINK_UNSAFE = re.compile(r"[][()_]")

def md_text(s: str) -> str:
    # лёгкая экранизация для markdown-ссылок
    return MD_LINK_UNSAFE.sub("", str(s or ""))

def pick(v: dict, *keys, default=None):
    for k in keys:
        if isinstance(v, dict) and k in v and v[k] not in (None, "", "null"):
            return v[k]
    return default

def f(x):
    try: return float(x)
    except: return None

def fmt_pct(x):
    if x is None: return "—"
    return f"{x*100:.2f}%"

def fmt_money(x):
    if x is None: return "—"
    v = float(x or 0)
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.2f}k"
    return f"${v:.0f}"

# ---------- HTTP with SSL fallback ----------
async def fetch_json(session: aiohttp.ClientSession, url: str):
    timeout = ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        ctx = SSL_CTX_INSECURE if INSECURE_SSL else SSL_CTX_VERIFIED
        async with session.get(url, timeout=timeout, ssl=ctx) as r:
            r.raise_for_status()
            return await r.json()
    except ClientConnectorCertificateError:
        # авто-ретрай без проверки (точечный, только если упала верификация)
        try:
            print(f"[SSL] verify failed at {url}; retry insecure…")
            async with session.get(url, timeout=timeout, ssl=SSL_CTX_INSECURE) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as e2:
            print(f"[SSL] insecure retry failed {url}: {e2}")
            return None
    except Exception as e:
        print(f"[..] fetch fail {url}: {e}")
        return None

# ---------- normalize ----------
def normalize_pool(chain: str, raw: dict) -> dict:
    name = pick(raw, "name", "poolSymbol", "symbol", "lpToken", "id", default="unknown")
    addr = pick(raw, "address", "poolAddress", "pool", "lpTokenAddress", default=None)

    base = f(pick(raw, "baseApy", "base_apr")) or f(pick(raw, "apy", "vAPY"))
    crv  = f(pick(raw, "crvApr", "crvApy", "crvAprDaily")) or 0.0
    extr = 0.0
    rewards = raw.get("rewards") or []
    if isinstance(rewards, list):
        for it in rewards:
            extr += f(pick(it, "apy", "apr", "tAPR")) or 0.0
    rewards_tapr = (crv or 0.0) + (extr or 0.0)

    volume = f(pick(raw, "volume", "volumeUSD", "volume24h")) or 0.0
    tvl    = f(pick(raw, "tvl", "tvlUSD", "tvlUsd", "usdTotal")) or 0.0

    link = f"https://curve.fi/#/pool/{addr}" if addr else f"https://curve.fi/#/pools/{chain}"

    return {
        "chain": chain,
        "pool": name,
        "address": addr,
        "base_vapy": base,          # доля
        "rewards_tapr": rewards_tapr,
        "volume": volume,
        "tvl": tvl,
        "link": link,
        "raw": raw,
    }

# ---------- fetch per chain ----------
async def fetch_chain_pools(session: aiohttp.ClientSession, chain: str):
    urls = CURVE_CANDIDATES.get(chain, [])
    for u in urls:
        data = await fetch_json(session, u)
        if not data:
            continue
        items = (
            data.get("data", {}).get("poolData") or      # getPools
            data.get("data", {}).get("poolDetails") or   # getFactoryAPYs v1
            data.get("data") or
            data.get("poolDetails") or
            data.get("apys")
        )
        if not items and isinstance(data, list):
            items = data
        if not items:
            print(f"[..] {chain}: empty at {u}")
            continue

        out = [normalize_pool(chain, it) for it in items]
        if out:
            print(f"[OK] {chain}: {u} -> {len(out)} pools")
            return out
    print(f"[!!] {chain}: nothing found")
    return []

# ---------- sorting / formatting ----------
def sort_key(kind: str):
    if kind == "tvl":     return lambda p: (p["tvl"] or 0.0)
    if kind == "apy":     return lambda p: (p["base_vapy"] or 0.0)
    if kind == "rewards": return lambda p: (p["rewards_tapr"] or 0.0)
    return lambda p: (p["volume"] or 0.0)  # default volume

def row_md(p: dict) -> str:
    name = md_text(p["pool"])[:40]
    return (
        f"• [{name}]({p['link']}) — "
        f"vAPY {fmt_pct(p['base_vapy'])} · 🎁 {fmt_pct(p['rewards_tapr'])} · "
        f"Vol {fmt_money(p['volume'])} · TVL {fmt_money(p['tvl'])}"
    )

async def handle_chain(session, chain: str, limit=20, kind="volume"):
    pools = await fetch_chain_pools(session, chain)
    if not pools:
        return f"⚠️ No data for *{chain}*"
    pools.sort(key=sort_key(kind), reverse=True)
    pools = pools[:limit]
    lines = [f"*{chain}* · top {limit} by {kind}"]
    lines += [row_md(p) for p in pools]
    return "\n".join(lines)

async def handle_top_all(session, limit=20, kind="volume"):
    all_pools = []
    for c in CHAINS:
        all_pools += (await fetch_chain_pools(session, c))
    if not all_pools:
        return "⚠️ No data"
    all_pools.sort(key=sort_key(kind), reverse=True)
    all_pools = all_pools[:limit]
    lines = [f"*ALL CHAINS* · top {limit} by {kind}"]
    lines += [row_md(p) for p in all_pools]
    return "\n".join(lines)

HELP = (
    "Commands:\n"
    "/ping — check bot\n"
    "/chains — list chains\n"
    "/<chain> [limit] [sort] — e.g. /ethereum 25 volume\n"
    "   sort = volume | tvl | apy | rewards\n"
    "/top volume all [limit] — cross-chain top\n"
)

# ---------- Telegram long-poll ----------
async def safe_send(text, *, md=True):
    global CHAT_ID
    try:
        await bot.send_message(
            CHAT_ID, text,
            parse_mode=constants.ParseMode.MARKDOWN if md else None,
            disable_web_page_preview=True,
        )
    except ChatMigrated as e:
        CHAT_ID = str(e.new_chat_id)
        await bot.send_message(
            CHAT_ID, text,
            parse_mode=constants.ParseMode.MARKDOWN if md else None,
            disable_web_page_preview=True,
        )

async def updates_loop():
    print(">> bot ready. Commands: /ping, /help, /chains, /ethereum 25 volume, /top volume all 25")
    offset = 0
    async with aiohttp.ClientSession() as session:
        try:
            await safe_send("✅ bot online")
        except Exception as e:
            print("warn: cannot send online msg:", e)

        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=POLL_INTERVAL)
                for u in updates or []:
                    offset = u.update_id + 1
                    m = getattr(u, "message", None)
                    if not m or not m.text:
                        continue
                    text = m.text.strip()
                    print("incoming:", text)

                    if text == "/ping":
                        await safe_send("pong"); continue
                    if text == "/help":
                        await safe_send(HELP); continue
                    if text == "/chains":
                        await safe_send(", ".join(CHAINS), md=False); continue

                    if text.startswith("/top"):
                        parts = text.split()
                        if len(parts) >= 3 and parts[1] in ("volume","tvl","apy","rewards") and parts[2] == "all":
                            limit = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 20
                            out = await handle_top_all(session, limit=limit, kind=parts[1])
                            await safe_send(out); continue
                        else:
                            await safe_send("Usage: /top volume all [limit]"); continue

                    if text.startswith("/"):
                        parts = text.split()
                        chain = parts[0][1:].lower()
                        if chain in CHAINS:
                            limit = 20
                            kind  = "volume"
                            if len(parts) >= 2 and parts[1].isdigit():
                                limit = max(1, min(50, int(parts[1])))
                            if len(parts) >= 3 and parts[2] in ("volume","tvl","apy","rewards"):
                                kind = parts[2]
                            out = await handle_chain(session, chain, limit=limit, kind=kind)
                            await safe_send(out); continue
            except Exception as e:
                print("loop error:", e)
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(updates_loop())