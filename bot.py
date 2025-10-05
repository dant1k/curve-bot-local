# bot.py — финальная версия (основной бот)
import os, asyncio, math
import aiohttp
from aiohttp import ClientTimeout
from telegram import Bot
from dotenv import load_dotenv

load_dotenv(override=True)
TOKEN  = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
CHAINS  = [c.strip() for c in (os.getenv("CHAINS") or "ethereum,arbitrum,polygon").split(",") if c.strip()]
TIMEOUT = ClientTimeout(total=25)
if not TOKEN or ":" not in TOKEN: raise SystemExit(f"Bad TELEGRAM_TOKEN: {repr(TOKEN)}")
if not CHAT_ID: raise SystemExit("Set CHAT_ID in .env")
bot = Bot(token=TOKEN)

API_BASES = ["https://api.curve.finance/v1","https://api.curve.fi/api"]
CHAIN_ALIASES = {
    "eth":"ethereum","ethereum":"ethereum",
    "arb":"arbitrum","arbitrum":"arbitrum",
    "poly":"polygon","polygon":"polygon",
    "op":"optimism","optimism":"optimism",
    "base":"base","avax":"avalanche","avalanche":"avalanche",
    "ftm":"fantom","fantom":"fantom","bsc":"bsc","gnosis":"gnosis","gno":"gnosis",
}
def candidate_urls(chain:str):
    urls=[]
    for b in API_BASES:
        urls+=[f"{b}/getFactoryAPYs/{chain}/1",f"{b}/getFactoryAPYs/{chain}/main",f"{b}/getFactoryAPYs?chain={chain}",
               f"{b}/getPools/{chain}/1",f"{b}/getPools/{chain}/main",
               f"{b}/getVolumes/{chain}/1",f"{b}/getVolumes/{chain}/main"]
    return list(dict.fromkeys(urls))
async def fetch_json(s,url,tries=2):
    for t in range(tries):
        try:
            async with s.get(url,timeout=TIMEOUT,ssl=False) as r:
                r.raise_for_status(); return await r.json()
        except Exception as e:
            if t==tries-1: print("fetch error:",url,e)
    return None
def num(x):
    try: return float(x)
    except: return 0.0
def norm_pooldetails(items):
    out=[]
    for p in items:
        out.append({"name":p.get("poolSymbol") or p.get("name") or "?",
                    "addr":p.get("poolAddress") or p.get("id") or "",
                    "apy": p.get("apyFormatted") or p.get("apy") or "-",
                    "tvl": num(p.get("tvl") or p.get("tvlUsd") or p.get("usdTotal") or 0),
                    "vol": num(p.get("volume") or p.get("volumeUSD") or p.get("usdVolume") or 0)})
    return out
def norm_pools(items):
    out=[]
    for p in items:
        out.append({"name":p.get("poolSymbol") or p.get("name") or p.get("symbol") or "?",
                    "addr":p.get("poolAddress") or p.get("id") or p.get("address") or "",
                    "apy": p.get("apyFormatted") or p.get("apy") or p.get("baseApy") or "-",
                    "tvl": num(p.get("tvl") or p.get("tvlUSD") or p.get("usdTotal") or p.get("totalLiquidityUSD") or 0),
                    "vol": num(p.get("volume") or p.get("volumeUSD") or p.get("usdVolume") or p.get("volume_24h") or 0)})
    return out
def chunk_text(lines,max_len=3800):
    out,buf,cur=[],[],0
    for ln in lines:
        L=len(ln)+1
        if cur+L>max_len and buf: out.append("\n".join(buf)); buf=[]; cur=0
        buf.append(ln); cur+=L
    if buf: out.append("\n".join(buf))
    return out
async def fetch_chain_top(session,chain,metric,limit):
    for url in candidate_urls(chain):
        data=await fetch_json(session,url)
        if not isinstance(data,dict): print(f"[..] {chain}: invalid at {url}"); continue
        blk=data.get("data") or {}
        pools=None
        if isinstance(blk.get("poolDetails"),list) and blk["poolDetails"]:
            pools=norm_pooldetails(blk["poolDetails"])
        elif isinstance(blk.get("pools"),list) and blk["pools"]:
            pools=norm_pools(blk["pools"])
        elif isinstance(blk,list) and blk:
            pools=norm_pools(blk)
        if pools:
            key="vol" if metric=="volume" else "tvl"
            pools.sort(key=lambda r:r.get(key) or 0, reverse=True)
            print(f"[OK] {chain}: {url} -> {len(pools)} pools")
            return pools[:limit]
        else:
            print(f"[..] {chain}: no data at {url}")
    print(f"[FAIL] {chain}: no pools from any URL"); return []
def format_money(x:float):
    if x>=1_000_000_000: return f"${x/1_000_000_000:.2f}b"
    if x>=1_000_000:     return f"${x/1_000_000:.2f}m"
    if x>=1_000:         return f"${x/1_000:.2f}k"
    return f"${x:.2f}"
async def build_and_send_top(session,chains,metric,limit):
    title="Volume" if metric=="volume" else "TVL"
    lines=[f"🏦 Top {limit} by {title}:"]
    for ch in chains:
        rows=await fetch_chain_top(session,ch,metric,limit)
        if not rows: lines.append(f"\n⚠️ No data for {ch}"); continue
        lines.append(f"\n🌐 {ch.upper()}")
        for i,r in enumerate(rows,1):
            val = r["vol"] if metric=="volume" else r["tvl"]
            lines.append(f"{i:>2}. {r['name']} — {format_money(val)} — APY {r['apy']}")
    for c in chunk_text(lines): await bot.send_message(CHAT_ID,c)
async def loop():
    offset=None
    async with aiohttp.ClientSession() as session:
        print(">> bot ready. /ping, /top20, /top <volume|tvl> [all|eth|arb|poly] [limit]")
        await bot.send_message(CHAT_ID,"✅ bot online")
        while True:
            upds=await bot.get_updates(offset=offset,timeout=30)
            for u in upds:
                offset=u.update_id+1
                m=u.message
                if not m or not m.text: continue
                text=m.text.strip()
                if str(m.chat.id)!=str(CHAT_ID): continue
                print("incoming:",m.chat.id,text)
                if text.startswith("/ping"):
                    await bot.send_message(CHAT_ID,"pong"); continue
                if text.startswith("/top20"):
                    await bot.send_message(CHAT_ID,f"⏳ Gathering top-20 by Volume for: {', '.join(CHAINS)} ...")
                    await build_and_send_top(session,CHAINS,"volume",20); continue
                if text.startswith("/top"):
                    parts=text.split()
                    metric=(parts[1].lower() if len(parts)>1 else "volume")
                    if metric not in ("volume","tvl"): metric="volume"
                    chain_arg=(parts[2].lower() if len(parts)>2 else "all")
                    try: limit=max(1,min(50,int(parts[3]))) if len(parts)>3 else 20
                    except: limit=20
                    if chain_arg=="all": use=CHAINS
                    else:
                        ch=CHAIN_ALIASES.get(chain_arg)
                        if not ch:
                            await bot.send_message(CHAT_ID,"Unknown chain. Use: all / eth / arb / poly / base / op / avax / ftm / bsc / gnosis"); continue
                        use=[ch]
                    await bot.send_message(CHAT_ID,f"⏳ Gathering top-{limit} by {metric.title()} for: {', '.join(use)} ...")
                    await build_and_send_top(session,use,metric,limit); continue
            await asyncio.sleep(2)
if __name__=="__main__":
    asyncio.run(loop())
