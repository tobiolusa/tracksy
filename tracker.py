#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║        SOLANA WALLET TRACKER — tracker.py        ║
║    Run: python tracker.py  |  Stop: Ctrl+C       ║
╚══════════════════════════════════════════════════╝

Telegram commands:
  /list                    — show all tracked wallets
  /add <addr> <label>      — start tracking a new wallet
  /remove <label>          — stop tracking (by label)
  /rename <old> <new>      — rename a wallet label
  /status                  — tracker health & SOL price
  /help                    — show all commands
"""

import asyncio
import json
import time
import sys
from datetime import datetime
from pathlib import Path

import aiohttp
import websockets

from config import HELIUS_API_KEY, BOT_TOKEN, CHAT_ID, MIN_SOL, SOL_PRICE_SOURCE

# ── Paths ─────────────────────────────────────────────────────────────────────
WALLETS_FILE = Path(__file__).parent / "wallets.json"

# ── URLs ──────────────────────────────────────────────────────────────────────
HELIUS_WS       = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_ENHANCED = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
DEXSCREENER     = "https://api.dexscreener.com/latest/dex/tokens/"
COINGECKO_SOL   = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
TELEGRAM_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── DEX program IDs ───────────────────────────────────────────────────────────
DEX_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
    "5quBtoiQqxF9Jv6KYKctB59NT3gtFD2SqzeAgzvASmfJ": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":  "Orca",
}

SOURCE_MAP = {
    "PUMP_FUN_AMM": "PumpSwap",
    "PUMP_FUN":     "Pump.fun",
    "RAYDIUM":      "Raydium",
    "ORCA":         "Orca",
    "JUPITER":      "Jupiter",
}

# ── Shared state ──────────────────────────────────────────────────────────────
wallets: dict         = {}   # { address: label }
ws_task: asyncio.Task | None = None   # single WebSocket task (replaces wallet_tasks)
processed_sigs: set   = set()
sol_price_cache: dict = {"price": 150.0, "ts": 0}
start_time: float     = time.time()
alert_count: int      = 0
update_offset: int    = 0
_session: aiohttp.ClientSession | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  WALLET PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def load_wallets() -> dict:
    if WALLETS_FILE.exists():
        with open(WALLETS_FILE) as f:
            return json.load(f)
    return {}

def save_wallets():
    with open(WALLETS_FILE, "w") as f:
        json.dump(wallets, f, indent=4)


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)

def fmt_number(n: float) -> str:
    return f"{n:,.2f}" if n >= 1_000 else f"{n:.6f}"

def fmt_mc(mc: float) -> str:
    if mc >= 1_000_000_000:
        return f"${mc/1_000_000_000:.2f}B"
    if mc >= 1_000_000:
        return f"${mc/1_000_000:.2f}M"
    if mc >= 1_000:
        return f"${mc/1_000:.2f}K"
    return f"${mc:.2f}"

def fmt_age(created_at_ms: int) -> str:
    if not created_at_ms:
        return "?"
    age_s = int(time.time()) - (created_at_ms // 1000)
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86400:
        return f"{age_s // 3600}h"
    return f"{age_s // 86400}d"

def fmt_uptime() -> str:
    s = int(time.time() - start_time)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s"

def is_valid_solana_address(addr: str) -> bool:
    import re
    return bool(re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', addr))

def detect_dex(account_keys: list) -> str:
    for key in account_keys:
        if key in DEX_PROGRAMS:
            return DEX_PROGRAMS[key]
    return "DEX"

def build_links(mint: str) -> str:
    be    = f'<a href="https://birdeye.so/token/{mint}?chain=solana">BE</a>'
    ds    = f'<a href="https://dexscreener.com/solana/{mint}">DS</a>'
    ph    = f'<a href="https://photon-sol.tinyastro.io/en/lp/{mint}">PH</a>'
    bullx = f'<a href="https://bullx.io/terminal?chainId=1399811149&address={mint}">Bullx</a>'
    gmgn  = f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a>'
    axi   = f'<a href="https://axiom.trade/t/{mint}">AXI</a>'
    padre = f'<a href="https://trade.padre.gg/trade/solana/{mint}">PADRE</a>'
    pump  = f'<a href="https://pump.fun/{mint}">Pump</a>'
    info  = f'<a href="https://t.me/TrenchesInfoBot?start={mint}">👥INFO</a>'
    return f"{be} | {ds} | {ph} | {bullx} | {gmgn} | {axi} | {info} | {padre} | {pump}"


# ══════════════════════════════════════════════════════════════════════════════
#  API CALLS
# ══════════════════════════════════════════════════════════════════════════════

async def get_sol_price(session: aiohttp.ClientSession) -> float:
    global sol_price_cache
    if SOL_PRICE_SOURCE != "auto":
        return float(SOL_PRICE_SOURCE)
    if time.time() - sol_price_cache["ts"] < 60:
        return sol_price_cache["price"]
    try:
        async with session.get(COINGECKO_SOL, timeout=aiohttp.ClientTimeout(total=4)) as r:
            data = await r.json()
            price = data["solana"]["usd"]
            sol_price_cache = {"price": price, "ts": time.time()}
            return price
    except Exception:
        return sol_price_cache["price"]


async def get_token_info(session: aiohttp.ClientSession, mint: str):
    """Returns (symbol, price_usd, mc_str, age_str)"""
    try:
        async with session.get(
            f"{DEXSCREENER}{mint}",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            data = await r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return None, 0.0, "N/A", "?"
            pair    = sorted(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0), reverse=True)[0]
            symbol  = pair.get("baseToken", {}).get("symbol", "???")
            price   = float(pair.get("priceUsd") or 0)
            mc      = float(pair.get("marketCap") or 0)
            created = pair.get("pairCreatedAt") or 0
            return symbol, price, fmt_mc(mc), fmt_age(created)
    except Exception as e:
        log(f"⚠️  DexScreener error: {e}")
        return None, 0.0, "N/A", "?"


async def fetch_enhanced_tx(session: aiohttp.ClientSession, signature: str) -> dict | None:
    try:
        async with session.post(
            HELIUS_ENHANCED,
            json={"transactions": [signature]},
            timeout=aiohttp.ClientTimeout(total=7)
        ) as r:
            data = await r.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception as e:
        log(f"⚠️  Enhanced TX error: {e}")
    return None


async def send_telegram(session: aiohttp.ClientSession, message: str, chat_id: str = CHAT_ID):
    try:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with session.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            result = await r.json()
            if not result.get("ok"):
                log(f"❌ Telegram error: {result.get('description')}")
    except Exception as e:
        log(f"❌ Telegram send failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET — SINGLE CONNECTION FOR ALL WALLETS
# ══════════════════════════════════════════════════════════════════════════════

def restart_watcher(session: aiohttp.ClientSession):
    """Cancel current WebSocket task and start a fresh one with all wallets."""
    global ws_task
    if ws_task and not ws_task.done():
        ws_task.cancel()
    ws_task = asyncio.create_task(watch_all_wallets(session))


async def watch_all_wallets(session: aiohttp.ClientSession):
    """
    ONE WebSocket connection that subscribes every wallet in `wallets`.
    This replaces per-wallet tasks and avoids HTTP 429 from Helius.

    Flow:
      1. Connect once
      2. Send logsSubscribe for every wallet (each gets a unique request id)
      3. Map server-confirmed subscription IDs → (address, label)
      4. Route logsNotification to the right wallet by subscription ID
      5. On disconnect: exponential backoff, then reconnect and re-subscribe all
    """
    backoff = 3

    while True:
        if not wallets:
            await asyncio.sleep(5)
            continue

        try:
            async with websockets.connect(
                HELIUS_WS,
                ping_interval=20,
                ping_timeout=15,
                close_timeout=5,
            ) as ws:
                backoff = 3  # reset on successful connect

                # ── Subscribe all wallets ─────────────────────────────────────
                # pending maps request-id → (address, label) until Helius confirms
                pending: dict[int, tuple[str, str]] = {}
                for req_id, (addr, label) in enumerate(wallets.items(), start=1):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [addr]},
                            {"commitment": "confirmed"}
                        ]
                    }))
                    pending[req_id] = (addr, label)

                log(f"📡 Subscribed {len(wallets)} wallet(s) on a single WebSocket")

                # confirmed maps server subscription-id → (address, label)
                confirmed: dict[int, tuple[str, str]] = {}

                # ── Listen ────────────────────────────────────────────────────
                async for raw in ws:
                    msg = json.loads(raw)

                    # Subscription confirmation: {"id": N, "result": <sub_id>}
                    if "result" in msg and isinstance(msg.get("result"), int) and "id" in msg:
                        req_id = msg["id"]
                        sub_id = msg["result"]
                        if req_id in pending:
                            addr, label = pending.pop(req_id)
                            confirmed[sub_id] = (addr, label)
                            log(f"✅ Watching [{label}]  {addr[:12]}...  (sub #{sub_id})")
                        continue

                    # Notification
                    if msg.get("method") == "logsNotification":
                        sub_id = msg["params"]["subscription"]
                        value  = msg["params"]["result"]["value"]
                        sig    = value.get("signature", "")
                        err    = value.get("err")
                        logs   = value.get("logs", [])

                        if err or not sig:
                            continue

                        wallet_info = confirmed.get(sub_id)
                        if not wallet_info:
                            continue

                        wallet_address, wallet_label = wallet_info
                        log_blob = " ".join(logs).lower()
                        if any(kw in log_blob for kw in [
                            "swap", "transfer", "buy", "sell",
                            "pumpswap", "raydium", "jupiter", "orca"
                        ]):
                            asyncio.create_task(
                                process_transaction(session, sig, wallet_address, wallet_label)
                            )

        except asyncio.CancelledError:
            log("🛑 WebSocket stopped")
            break
        except websockets.exceptions.ConnectionClosedError as e:
            log(f"⚠️  WebSocket disconnected ({e.code}), reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            log(f"❌ WebSocket: {type(e).__name__}: {e}, reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_command(session: aiohttp.ClientSession, text: str, from_chat_id: str):
    global wallets, ws_task, alert_count

    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().lstrip("/").split("@")[0]

    # /help or /start
    if cmd in ("help", "start"):
        msg = (
            "🤖 <b>Solana Wallet Tracker</b>\n\n"
            "<b>Commands:</b>\n\n"
            "📋 /list\n"
            "    Show all tracked wallets\n\n"
            "➕ /add &lt;address&gt; &lt;label&gt;\n"
            "    Add a new wallet to track\n"
            "    <i>e.g. /add ABC123... MyWhale</i>\n\n"
            "🗑️ /remove &lt;label&gt;\n"
            "    Remove wallet by label\n"
            "    <i>e.g. /remove Rue ALpha</i>\n\n"
            "✏️ /rename &lt;label&gt; &lt;newlabel&gt;\n"
            "    Rename a wallet\n"
            "    <i>e.g. /rename Lunar LunarV2</i>\n\n"
            "📊 /status\n"
            "    Tracker health + SOL price"
        )
        await send_telegram(session, msg, from_chat_id)

    # /list
    elif cmd == "list":
        if not wallets:
            await send_telegram(session, "📭 No wallets being tracked.\n\nUse /add &lt;address&gt; &lt;label&gt; to add one.", from_chat_id)
            return
        ws_alive = ws_task and not ws_task.done()
        lines = [f"👀 <b>Tracked Wallets</b> ({len(wallets)})\n"]
        for i, (addr, label) in enumerate(wallets.items(), 1):
            status = "🟢" if ws_alive else "🔴"
            lines.append(f"{status} <b>{label}</b>\n<code>{addr}</code>\n")
        await send_telegram(session, "\n".join(lines), from_chat_id)

    # /add <address> <label>
    elif cmd == "add":
        if len(parts) < 3:
            await send_telegram(session,
                "❌ <b>Usage:</b> <code>/add &lt;address&gt; &lt;label&gt;</code>\n\n"
                "Example:\n<code>/add VJSDW6S74YXR4rRR9P4xwhMvLZJQMhrUb8XMFirUsy1 BEAN</code>",
                from_chat_id)
            return
        addr  = parts[1].strip()
        label = " ".join(parts[2:]).strip()

        if not is_valid_solana_address(addr):
            await send_telegram(session, "❌ That doesn't look like a valid Solana address.\nDouble-check and try again.", from_chat_id)
            return
        if addr in wallets:
            await send_telegram(session, f"⚠️ Already tracking that wallet as <b>{wallets[addr]}</b>.", from_chat_id)
            return
        if label.lower() in [v.lower() for v in wallets.values()]:
            await send_telegram(session, f"⚠️ Label <b>{label}</b> already exists. Use a different name.", from_chat_id)
            return

        wallets[addr] = label
        save_wallets()

        # Restart the single WebSocket so it picks up the new wallet
        restart_watcher(session)

        log(f"➕ Added [{label}] {addr}")
        await send_telegram(session,
            f"✅ <b>Now tracking: {label}</b>\n<code>{addr}</code>", from_chat_id)

    # /remove <label>
    elif cmd == "remove":
        if len(parts) < 2:
            await send_telegram(session,
                "❌ <b>Usage:</b> <code>/remove &lt;label&gt;</code>\n\n"
                "Example: <code>/remove Lunar</code>",
                from_chat_id)
            return
        target = " ".join(parts[1:]).strip().lower()
        addr_found = next((a for a, l in wallets.items() if l.lower() == target), None)

        if not addr_found:
            await send_telegram(session,
                f"❌ No wallet with label <b>{' '.join(parts[1:])}</b>\n"
                "Use /list to see exact labels.", from_chat_id)
            return

        removed_label = wallets.pop(addr_found)
        save_wallets()

        # Restart the single WebSocket without the removed wallet
        restart_watcher(session)

        log(f"➖ Removed [{removed_label}]")
        await send_telegram(session, f"🗑️ Removed <b>{removed_label}</b> from tracking.", from_chat_id)

    # /rename <old label> <new label>
    elif cmd == "rename":
        if len(parts) < 3:
            await send_telegram(session,
                "❌ <b>Usage:</b> <code>/rename &lt;old label&gt; &lt;new label&gt;</code>\n\n"
                "Example: <code>/rename Lunar LunarV2</code>",
                from_chat_id)
            return

        new_label = parts[-1].strip()
        old_label_guess = " ".join(parts[1:-1]).strip()

        addr_found = next((a for a, l in wallets.items() if l.lower() == old_label_guess.lower()), None)

        if not addr_found:
            addr_found = next((a for a, l in wallets.items() if l.lower() == parts[1].lower()), None)
            if addr_found:
                new_label = " ".join(parts[2:]).strip()
                old_label_guess = parts[1]

        if not addr_found:
            await send_telegram(session,
                f"❌ No wallet with label <b>{old_label_guess}</b>\n"
                "Use /list to see exact labels.", from_chat_id)
            return

        old_name = wallets[addr_found]
        wallets[addr_found] = new_label
        save_wallets()

        # Restart with updated label
        restart_watcher(session)

        log(f"✏️  Renamed [{old_name}] → [{new_label}]")
        await send_telegram(session, f"✏️ Renamed <b>{old_name}</b> → <b>{new_label}</b>", from_chat_id)

    # /status
    elif cmd == "status":
        price    = await get_sol_price(session)
        ws_alive = ws_task and not ws_task.done()
        msg = (
            f"📊 <b>Tracker Status</b>\n\n"
            f"🟢 Uptime: <b>{fmt_uptime()}</b>\n"
            f"📡 WebSocket: <b>{'🟢 connected' if ws_alive else '🔴 reconnecting'}</b>\n"
            f"👀 Wallets: <b>{len(wallets)} tracked</b>\n"
            f"🔔 Alerts sent: <b>{alert_count}</b>\n"
            f"💰 SOL price: <b>${price:,.2f}</b>\n"
            f"🔑 Min SOL: <b>{MIN_SOL} SOL</b>"
        )
        await send_telegram(session, msg, from_chat_id)

    else:
        await send_telegram(session,
            f"❓ Unknown command <b>/{cmd}</b>\nSend /help to see all commands.", from_chat_id)


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM LONG-POLL (commands listener)
# ══════════════════════════════════════════════════════════════════════════════

async def poll_commands(session: aiohttp.ClientSession):
    global update_offset
    log("💬 Command listener ready — send /help to your bot on Telegram")

    while True:
        try:
            params = {
                "timeout": 30,
                "offset": update_offset,
                "allowed_updates": ["message"]
            }
            async with session.get(
                f"{TELEGRAM_API}/getUpdates",
                params=params,
                timeout=aiohttp.ClientTimeout(total=35)
            ) as r:
                data = await r.json()

            if not data.get("ok"):
                await asyncio.sleep(3)
                continue

            for update in data.get("result", []):
                update_offset = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if text.startswith("/") and chat_id == CHAT_ID:
                    log(f"📩 Command: {text}")
                    asyncio.create_task(handle_command(session, text, chat_id))

        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"⚠️  Poll error: {e}")
            await asyncio.sleep(3)


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSACTION PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

async def process_transaction(session: aiohttp.ClientSession, signature: str, wallet_address: str, wallet_label: str):
    global alert_count

    if signature in processed_sigs:
        return
    processed_sigs.add(signature)

    await asyncio.sleep(0.8)

    tx = await fetch_enhanced_tx(session, signature)
    if not tx or tx.get("transactionError"):
        return

    tx_type   = tx.get("type", "UNKNOWN")
    tx_source = tx.get("source", "")
    dex_name  = SOURCE_MAP.get(tx_source, tx_source.replace("_", " ").title() if tx_source else "DEX")

    if dex_name in ("DEX", ""):
        account_keys = [a.get("account", "") for a in tx.get("accountData", [])]
        dex_name = detect_dex(account_keys)

    sol_price = await get_sol_price(session)

    # ── SWAP ──────────────────────────────────────────────────────────────────
    if tx_type == "SWAP":
        native_transfers = tx.get("nativeTransfers", [])
        token_transfers  = tx.get("tokenTransfers", [])

        sol_delta = 0.0
        for nt in native_transfers:
            if nt.get("fromUserAccount") == wallet_address:
                sol_delta -= nt.get("amount", 0) / 1e9
            if nt.get("toUserAccount") == wallet_address:
                sol_delta += nt.get("amount", 0) / 1e9

        token_in, token_out = None, None
        for tt in token_transfers:
            amt = float(tt.get("tokenAmount") or 0)
            if tt.get("toUserAccount") == wallet_address and amt > 0:
                token_in = tt
            if tt.get("fromUserAccount") == wallet_address and amt > 0:
                token_out = tt

        sol_amount = abs(sol_delta)

        if sol_delta < 0:
            action = "BUY"
            main_token = token_in or token_out
        else:
            action = "SELL"
            main_token = token_out or token_in

        if not main_token:
            return

        mint         = main_token.get("mint", "")
        token_amount = float(main_token.get("tokenAmount") or 0)

        if sol_amount < MIN_SOL:
            return

        symbol, price_usd, mc_str, age_str = await get_token_info(session, mint)
        symbol = symbol or mint[:6] + "..."

        usd_value     = token_amount * price_usd if price_usd > 0 else sol_amount * sol_price
        price_display = f"${price_usd:.8f}".rstrip('0') if price_usd > 0 else "N/A"
        usd_display   = f"${usd_value:,.2f}"
        emoji         = "🟢" if action == "BUY" else "🔴"
        links         = build_links(mint)
        padre_link    = f"https://trade.padre.gg/trade/solana/{mint}"

        if action == "BUY":
            swap_line = (
                f"🔹{wallet_label} swapped <b>{sol_amount:.4f} SOL</b> for "
                f"<b>{fmt_number(token_amount)}</b> ({usd_display}) "
                f"{symbol} @{price_display}"
            )
        else:
            swap_line = (
                f"🔹{wallet_label} swapped <b>{fmt_number(token_amount)}</b> "
                f"({usd_display}) {symbol} for <b>{sol_amount:.4f} SOL</b> "
                f"@{price_display}"
            )

        message = (
            f"{emoji} <b>{action} {symbol} on {dex_name}</b>\n"
            f"🔹 {wallet_label}\n\n"
            f"{swap_line}\n\n"
            f"💊 #{symbol} | MC: {mc_str} | Seen: {age_str}\n"
            f"{links}\n"
            f'🔗 <a href="{padre_link}">Trade on PADRE</a>\n'
            f"<code>{mint}</code>"
        )

        alert_count += 1
        log(f"{'🟢 BUY' if action=='BUY' else '🔴 SELL'} | {wallet_label} | {sol_amount:.3f} SOL ↔ {symbol} | {usd_display}")
        await send_telegram(session, message)

    # ── TRANSFER ──────────────────────────────────────────────────────────────
    elif tx_type == "TRANSFER":
        for nt in tx.get("nativeTransfers", []):
            amount_sol = nt.get("amount", 0) / 1e9
            if amount_sol < MIN_SOL:
                continue
            if nt.get("fromUserAccount") == wallet_address:
                usd = amount_sol * sol_price
                msg = (
                    f"↗️ <b>SOL TRANSFER</b>\n"
                    f"🔹 {wallet_label}\n\n"
                    f"🔹{wallet_label} sent <b>{amount_sol:.4f} SOL</b> (${usd:,.2f})\n"
                    f"To: <code>{nt.get('toUserAccount','?')}</code>"
                )
                alert_count += 1
                log(f"↗️ TRANSFER | {wallet_label} | {amount_sol:.4f} SOL")
                await send_telegram(session, msg)

        for tt in tx.get("tokenTransfers", []):
            if tt.get("fromUserAccount") == wallet_address:
                mint   = tt.get("mint", "")
                amount = float(tt.get("tokenAmount") or 0)
                symbol, price_usd, mc_str, _ = await get_token_info(session, mint)
                symbol = symbol or mint[:6] + "..."
                usd    = amount * price_usd if price_usd else 0
                msg = (
                    f"↗️ <b>TOKEN TRANSFER — {symbol}</b>\n"
                    f"🔹 {wallet_label}\n\n"
                    f"🔹{wallet_label} sent <b>{fmt_number(amount)} {symbol}</b>"
                    f"{f' (${usd:,.2f})' if usd else ''}\n"
                    f"To: <code>{tt.get('toUserAccount','?')}</code>\n"
                    f"<code>{mint}</code>"
                )
                alert_count += 1
                log(f"↗️ TOKEN TRANSFER | {wallet_label} | {symbol}")
                await send_telegram(session, msg)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    global wallets, ws_task

    print("""
╔══════════════════════════════════════════════════╗
║        🚀  SOLANA WALLET TRACKER  🚀             ║
║         Press Ctrl+C to stop                     ║
╚══════════════════════════════════════════════════╝
""", flush=True)

    wallets = load_wallets()

    connector = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:

        price = await get_sol_price(session)
        log(f"💰 SOL price: ${price:,.2f}")

        log(f"📡 Tracking {len(wallets)} wallet(s):")
        for addr, label in wallets.items():
            log(f"    🔹 {label}  ({addr[:12]}...)")
        print(flush=True)

        # Start single WebSocket for all wallets
        ws_task = asyncio.create_task(watch_all_wallets(session))

        # Startup Telegram message
        wallet_list = "\n".join(f"🔹 <b>{label}</b>" for label in wallets.values())
        startup_msg = (
            f"🚀 <b>Wallet Tracker Online</b>\n\n"
            f"👀 Tracking <b>{len(wallets)}</b> wallet(s):\n"
            f"{wallet_list}\n\n"
            f"💰 SOL: <b>${price:,.2f}</b>\n"
            f"🔋 <a href=\"https://dashboard.helius.dev\">Check credits →</a>\n\n"
            f"Send /help for commands"
        )
        await send_telegram(session, startup_msg)

        # Start command listener
        poll_task = asyncio.create_task(poll_commands(session))

        try:
            await asyncio.gather(poll_task, ws_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋  Tracker stopped. Goodbye!\n")
        