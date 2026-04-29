#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import os
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


DEX_BASE = "https://api.dexscreener.com"
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

CHAIN_ID_MAP = {
    "ethereum": 1,
    "eth": 1,
    "bsc": 56,
    "bnb": 56,
    "base": 8453,
    "polygon": 137,
    "arbitrum": 42161,
    "optimism": 10,
}

BUY_AMOUNTS = [1000, 1500, 2000, 5000, 10000, 20000]

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

DEFAULT_CHAIN = os.getenv("CHAIN", "base")
DEFAULT_TOKEN_ADDRESS = os.getenv("TOKEN_ADDRESS", "")
DEFAULT_PAIR_ADDRESS = os.getenv("PAIR_ADDRESS", "")
DEFAULT_QUERY = os.getenv("QUERY", "")

PROJECT_NAME = os.getenv("PROJECT_NAME", "IRVUS")

BURNED_SUPPLY = float(os.getenv("BURNED_SUPPLY", "71070000"))
LOCKED_SUPPLY = float(os.getenv("LOCKED_SUPPLY", "200000000"))
TOTAL_SUPPLY = float(os.getenv("TOTAL_SUPPLY", "1000000000"))

ALERT_LIQUIDITY_BELOW = float(os.getenv("ALERT_LIQUIDITY_BELOW", "90000"))
ALERT_SLIP_5K = float(os.getenv("ALERT_SLIP_5K", "12"))
ALERT_SLIP_20K = float(os.getenv("ALERT_SLIP_20K", "35"))
ALERT_RATIO_BELOW = float(os.getenv("ALERT_RATIO_BELOW", "0.70"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("irvus-risk-bot")


@dataclass
class PairSnapshot:
    chain_id: str
    dex_id: str
    pair_address: str
    pair_url: Optional[str]
    base_symbol: str
    base_address: str
    quote_symbol: str
    quote_address: str
    price_usd: float
    liquidity_usd: float
    fdv: Optional[float]
    market_cap: Optional[float]
    txns_m5_buys: int
    txns_m5_sells: int
    txns_h1_buys: int
    txns_h1_sells: int
    volume_m5: float
    volume_h1: float
    volume_h24: float
    price_change_m5: Optional[float]
    price_change_h1: Optional[float]
    price_change_h24: Optional[float]


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.10f}"


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:.2f}%"


def fmt_token_amount(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    if abs(x) >= 1_000_000:
        return f"{x:,.0f}"
    if abs(x) >= 1_000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}"
    return f"{x:.8f}"


def fmt_number(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.0f}"


def current_price(reserve_token: float, reserve_quote: float) -> float:
    if reserve_token <= 0:
        raise ValueError("reserve_token must be > 0")
    return reserve_quote / reserve_token


def simulate_buy_constant_product(
    reserve_token: float,
    reserve_quote: float,
    buy_quote: float,
    fee_pct: float = 0.003,
) -> Dict[str, float]:
    start_p = current_price(reserve_token, reserve_quote)
    k = reserve_token * reserve_quote

    effective_quote_in = buy_quote * (1 - fee_pct)
    new_reserve_quote = reserve_quote + effective_quote_in
    new_reserve_token = k / new_reserve_quote

    token_out = reserve_token - new_reserve_token
    end_p = current_price(new_reserve_token, new_reserve_quote)
    avg_p = buy_quote / token_out

    slippage_pct = ((avg_p / start_p) - 1.0) * 100.0
    impact_pct = ((end_p / start_p) - 1.0) * 100.0

    return {
        "input_quote": buy_quote,
        "token_out": token_out,
        "start_price": start_p,
        "avg_price": avg_p,
        "end_price": end_p,
        "slippage_pct": slippage_pct,
        "impact_pct": impact_pct,
    }


class LiveDexMonitor:
    def __init__(self, timeout: int = 20) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "irvus-risk-bot/3.0"})
        self.timeout = timeout

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def search_pairs(self, query: str) -> List[Dict[str, Any]]:
        data = self._get(f"{DEX_BASE}/latest/dex/search", params={"q": query})
        return data.get("pairs", []) or []

    def get_pair(self, chain_id: str, pair_address: str) -> Optional[Dict[str, Any]]:
        data = self._get(f"{DEX_BASE}/latest/dex/pairs/{chain_id}/{pair_address}")
        pairs = data.get("pairs", []) or []
        return pairs[0] if pairs else None

    def get_token_pairs(self, chain_id: str, token_address: str) -> List[Dict[str, Any]]:
        data = self._get(f"{DEX_BASE}/token-pairs/v1/{chain_id}/{token_address}")
        if isinstance(data, list):
            return data
        return data.get("pairs", []) or []

    def choose_best_pair(self, pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not pairs:
            raise ValueError("Pair bulunamadı.")

        def score(p: Dict[str, Any]) -> float:
            liq = safe_float((p.get("liquidity") or {}).get("usd"), 0.0)
            vol = safe_float((p.get("volume") or {}).get("h24"), 0.0)
            return liq * 1000 + vol

        return sorted(pairs, key=score, reverse=True)[0]

    def normalize_pair(self, p: Dict[str, Any]) -> PairSnapshot:
        txns = p.get("txns") or {}
        volume = p.get("volume") or {}
        price_change = p.get("priceChange") or {}
        liquidity = p.get("liquidity") or {}
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}

        return PairSnapshot(
            chain_id=str(p.get("chainId") or ""),
            dex_id=str(p.get("dexId") or ""),
            pair_address=str(p.get("pairAddress") or ""),
            pair_url=p.get("url"),
            base_symbol=str(base.get("symbol") or ""),
            base_address=str(base.get("address") or ""),
            quote_symbol=str(quote.get("symbol") or ""),
            quote_address=str(quote.get("address") or ""),
            price_usd=safe_float(p.get("priceUsd"), 0.0),
            liquidity_usd=safe_float(liquidity.get("usd"), 0.0),
            fdv=safe_float(p.get("fdv"), 0.0) if p.get("fdv") is not None else None,
            market_cap=safe_float(p.get("marketCap"), 0.0) if p.get("marketCap") is not None else None,
            txns_m5_buys=safe_int((txns.get("m5") or {}).get("buys"), 0),
            txns_m5_sells=safe_int((txns.get("m5") or {}).get("sells"), 0),
            txns_h1_buys=safe_int((txns.get("h1") or {}).get("buys"), 0),
            txns_h1_sells=safe_int((txns.get("h1") or {}).get("sells"), 0),
            volume_m5=safe_float(volume.get("m5"), 0.0),
            volume_h1=safe_float(volume.get("h1"), 0.0),
            volume_h24=safe_float(volume.get("h24"), 0.0),
            price_change_m5=safe_float(price_change.get("m5"), 0.0) if price_change.get("m5") is not None else None,
            price_change_h1=safe_float(price_change.get("h1"), 0.0) if price_change.get("h1") is not None else None,
            price_change_h24=safe_float(price_change.get("h24"), 0.0) if price_change.get("h24") is not None else None,
        )

    def resolve_pair(self) -> Dict[str, Any]:
        if DEFAULT_PAIR_ADDRESS and DEFAULT_CHAIN:
            pair = self.get_pair(DEFAULT_CHAIN, DEFAULT_PAIR_ADDRESS)
            if not pair:
                raise ValueError("Verilen pair bulunamadı.")
            return pair

        if DEFAULT_TOKEN_ADDRESS and DEFAULT_CHAIN:
            pairs = self.get_token_pairs(DEFAULT_CHAIN, DEFAULT_TOKEN_ADDRESS)
            return self.choose_best_pair(pairs)

        if DEFAULT_QUERY:
            pairs = self.search_pairs(DEFAULT_QUERY)
            return self.choose_best_pair(pairs)

        raise ValueError("PAIR_ADDRESS veya TOKEN_ADDRESS veya QUERY gerekli.")

    def estimate_reserves(self, snap: PairSnapshot) -> Dict[str, float]:
        if snap.price_usd <= 0:
            raise ValueError("Canlı price_usd alınamadı.")
        if snap.liquidity_usd <= 0:
            raise ValueError("Canlı liquidity_usd alınamadı.")

        reserve_quote = snap.liquidity_usd / 2.0
        reserve_token = reserve_quote / snap.price_usd

        return {
            "reserve_quote": reserve_quote,
            "reserve_token": reserve_token,
        }


def get_holder_count(chain: str, token_address: str, api_key: str) -> Optional[int]:
    if not api_key or not token_address:
        return None

    chain_id = CHAIN_ID_MAP.get(chain.lower())
    if not chain_id:
        return None

    params = {
        "chainid": chain_id,
        "module": "token",
        "action": "tokenholdercount",
        "contractaddress": token_address,
        "apikey": api_key,
    }

    try:
        r = requests.get(ETHERSCAN_V2_BASE, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        if str(data.get("status")) == "1":
            return int(data.get("result"))

        return None
    except Exception as e:
        logger.warning("Holder count alınamadı: %s", e)
        return None


def calculate_risk_level(
    liquidity_usd: float,
    slip_5k: float,
    slip_20k: float,
    buy_sell_ratio_h1: Optional[float],
) -> str:
    score = 0

    if liquidity_usd < ALERT_LIQUIDITY_BELOW:
        score += 2

    if slip_5k > ALERT_SLIP_5K:
        score += 2

    if slip_20k > ALERT_SLIP_20K:
        score += 2

    if buy_sell_ratio_h1 is not None and buy_sell_ratio_h1 < ALERT_RATIO_BELOW:
        score += 1

    if score >= 5:
        return "🔴 HIGH"
    if score >= 3:
        return "🟠 MEDIUM"
    return "🟢 LOW"


def build_premium_message(
    snap: PairSnapshot,
    holders: Optional[int],
    burned_supply: float,
    locked_supply: float,
    total_supply: float,
) -> str:
    monitor = LiveDexMonitor()
    reserves = monitor.estimate_reserves(snap)

    reserve_quote = reserves["reserve_quote"]
    reserve_token = reserves["reserve_token"]

    simulations = {
        amount: simulate_buy_constant_product(
            reserve_token=reserve_token,
            reserve_quote=reserve_quote,
            buy_quote=float(amount),
        )
        for amount in BUY_AMOUNTS
    }

    buy_sell_ratio_h1 = None
    if snap.txns_h1_sells > 0:
        buy_sell_ratio_h1 = snap.txns_h1_buys / snap.txns_h1_sells

    risk_level = calculate_risk_level(
        liquidity_usd=snap.liquidity_usd,
        slip_5k=simulations[5000]["slippage_pct"],
        slip_20k=simulations[20000]["slippage_pct"],
        buy_sell_ratio_h1=buy_sell_ratio_h1,
    )

    circulating_after_burn_locked = max(total_supply - burned_supply - locked_supply, 0)

    burned_pct = (burned_supply / total_supply * 100) if total_supply > 0 else 0
    locked_pct = (locked_supply / total_supply * 100) if total_supply > 0 else 0
    circulating_pct = (circulating_after_burn_locked / total_supply * 100) if total_supply > 0 else 0

    lines: List[str] = []

    lines.append(f"🟢 {PROJECT_NAME} LIVE RISK PANEL")
    lines.append("")
    lines.append(f"⛓️ Chain: {snap.chain_id}")
    lines.append(f"🏪 DEX: {snap.dex_id}")
    lines.append(f"💵 Price: {fmt_money(snap.price_usd)}")
    lines.append(f"💧 Liquidity: {fmt_money(snap.liquidity_usd)}")
    lines.append(f"📊 FDV: {fmt_money(snap.fdv)}")
    lines.append(f"🏦 Market Cap: {fmt_money(snap.market_cap)}")

    if holders is not None:
        lines.append(f"👥 Holders: {holders:,}")
    else:
        lines.append("👥 Holders: n/a")

    lines.append("")
    lines.append("🔥 Supply Status")
    lines.append(f"• Total Supply: {fmt_number(total_supply)}")
    lines.append(f"• Burned: {fmt_number(burned_supply)} ({burned_pct:.2f}%)")
    lines.append(f"• Locked: {fmt_number(locked_supply)} ({locked_pct:.2f}%)")
    lines.append(f"• Free Circulation Est.: {fmt_number(circulating_after_burn_locked)} ({circulating_pct:.2f}%)")

    lines.append("")
    lines.append("📈 Market Activity")
    lines.append(
        f"• Price Change m5/h1/h24: {fmt_pct(snap.price_change_m5)} / {fmt_pct(snap.price_change_h1)} / {fmt_pct(snap.price_change_h24)}"
    )
    lines.append(f"• m5 Buys/Sells: {snap.txns_m5_buys}/{snap.txns_m5_sells}")
    lines.append(f"• h1 Buys/Sells: {snap.txns_h1_buys}/{snap.txns_h1_sells}")
    lines.append(
        f"• Volume m5/h1/h24: {fmt_money(snap.volume_m5)} / {fmt_money(snap.volume_h1)} / {fmt_money(snap.volume_h24)}"
    )

    if buy_sell_ratio_h1 is not None:
        lines.append(f"• h1 Buy/Sell Ratio: {buy_sell_ratio_h1:.2f}")

    lines.append("")
    lines.append("🧪 Buy Simulation")

    for amount in BUY_AMOUNTS:
        sim = simulations[amount]
        label = f"${amount / 1000:g}K"

        lines.append(
            f"• {label:<5} → {fmt_token_amount(sim['token_out'])} {snap.base_symbol} | "
            f"avg {fmt_money(sim['avg_price'])} | "
            f"slip {sim['slippage_pct']:.2f}% | "
            f"impact {sim['impact_pct']:.2f}%"
        )

    lines.append("")
    lines.append("🚨 Risk Summary")
    lines.append(f"• Risk Level: {risk_level}")

    if simulations[5000]["slippage_pct"] > ALERT_SLIP_5K:
        lines.append(f"• 5K slip high: {simulations[5000]['slippage_pct']:.2f}%")

    if simulations[20000]["slippage_pct"] > ALERT_SLIP_20K:
        lines.append(f"• 20K slip very high: {simulations[20000]['slippage_pct']:.2f}%")

    if snap.liquidity_usd < ALERT_LIQUIDITY_BELOW:
        lines.append(f"• Low liquidity: {fmt_money(snap.liquidity_usd)}")

    if buy_sell_ratio_h1 is not None and buy_sell_ratio_h1 < ALERT_RATIO_BELOW:
        lines.append(f"• Sell pressure: ratio {buy_sell_ratio_h1:.2f}")

    if snap.pair_url:
        lines.append("")
        lines.append(f"🔗 {snap.pair_url}")

    return "\n".join(lines)


async def send_risk_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.effective_message

    try:
        if msg_obj:
            await msg_obj.reply_text("⏳ Live risk panel hazırlanıyor...")

        monitor = LiveDexMonitor()

        raw_pair = monitor.resolve_pair()
        snap = monitor.normalize_pair(raw_pair)

        token_for_holder = DEFAULT_TOKEN_ADDRESS or snap.base_address

        holders = get_holder_count(
            chain=snap.chain_id or DEFAULT_CHAIN,
            token_address=token_for_holder,
            api_key=ETHERSCAN_API_KEY,
        )

        msg = build_premium_message(
            snap=snap,
            holders=holders,
            burned_supply=BURNED_SUPPLY,
            locked_supply=LOCKED_SUPPLY,
            total_supply=TOTAL_SUPPLY,
        )

        if msg_obj:
            await msg_obj.reply_text(msg, disable_web_page_preview=True)

    except Exception as e:
        logger.exception("Risk panel hata verdi")
        if msg_obj:
            await msg_obj.reply_text(f"⚠️ Hata: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.effective_message
    if msg_obj:
        await msg_obj.reply_text(
            "Bot aktif ✅\n\n"
            "Komutlar:\n"
            "/risk - Canlı risk paneli\n"
            "/help - Yardım",
            disable_web_page_preview=True,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.effective_message
    if msg_obj:
        await msg_obj.reply_text(
            "Kullanım:\n\n"
            "/risk\n\n"
            "Private chat, grup veya kanal içinde çalışır.\n"
            "Kanalda botun admin olması gerekir.",
            disable_web_page_preview=True,
        )


async def channel_risk_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_obj = update.effective_message

    if not msg_obj or not msg_obj.text:
        return

    text = msg_obj.text.strip().lower()

    if text.startswith("/risk"):
        await send_risk_panel(update, context)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN boş. Railway Variables içine BOT_TOKEN girmelisin.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Private chat ve grup komutları
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("risk", send_risk_panel))

    # Kanal postları için: kanalda /risk yazılırsa yakalar
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.TEXT,
            channel_risk_message,
        )
    )

    logger.info("Bot başladı.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
