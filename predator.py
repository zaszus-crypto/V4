#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD PREDATOR v8.6 (FINAL PRODUCTION GRADE)
------------------------------------------------------------------
Fixes:
1. State Persistence (Memori lokal agar tidak spam Telegram)
2. Robust Yahoo Finance Fetching with Retry Logic & Fallbacks
3. Timeframe Resampling Fixes for Cloud Runners
------------------------------------------------------------------
"""

import os
import sys
import json
import time
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

class Config:
    BOT_NAME = "XAU/USD PREDATOR"
    VERSION = "8.6-FINAL"
    SYMBOL = "GC=F"
    
    RISK_PERCENT = 1.0
    MIN_RR_RATIO = 2.0
    COOLDOWN_HOURS = 4
    
    H4_LOOKBACK_SWING = 5
    M15_LOOKBACK_CHOCH = 3
    FVG_MIN_ATR_RATIO = 0.35
    SL_BUFFER_ATR_MULT = 0.5
    STATE_FILE = "predator_state.json"
    
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("PREDATOR")

# ==============================================================================
# STATE MANAGER (MEMORI ANTI-SPAM)
# ==============================================================================
class StateManager:
    def __init__(self):
        self.file_path = Path(Config.STATE_FILE)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                with open(self.file_path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self, signal_type: str, price: float):
        self.data = {
            "last_signal": signal_type,
            "last_price": price,
            "last_time": datetime.now(timezone.utc).isoformat()
        }
        try:
            with open(self.file_path, "w") as f:
                json.dump(self.data, f)
        except Exception as e:
            log.error(f"Gagal menyimpan state: {e}")

    def is_spam(self, signal_type: str, price: float) -> bool:
        if "last_time" not in self.data:
            return False
            
        last_time = datetime.fromisoformat(self.data["last_time"])
        now = datetime.now(timezone.utc)
        hours_passed = (now - last_time).total_seconds() / 3600.0
        
        # Jika kurang dari durasi cooldown DAN arahnya sama dengan toleransi harga 0.15%
        if hours_passed < Config.COOLDOWN_HOURS:
            if self.data.get("last_signal") == signal_type:
                price_diff = abs(price - self.data.get("last_price", 0.0)) / price
                if price_diff < 0.0015:
                    return True
        return False

def send_telegram(text: str) -> bool:
    token, chat_id = Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.warning("Telegram Credentials tidak dikonfigurasi.")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Error Telegram: {e}")
        return False

# ==============================================================================
# DATA ENGINE WITH RETRY & FALLBACK
# ==============================================================================
class MarketDataEngine:
    @staticmethod
    def fetch_ohlcv_with_retry(interval: str, period: str, retries: int = 3) -> pd.DataFrame:
        for attempt in range(retries):
            try:
                ticker = yf.Ticker(Config.SYMBOL)
                df = ticker.history(period=period, interval=interval)
                if not df.empty and len(df) >= 10:
                    df.columns = [c.lower() for c in df.columns]
                    df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
                    df.index = pd.to_datetime(df.index, utc=True)
                    return df
            except Exception as e:
                log.warning(f"Attempt {attempt+1} gagal mengambil data {interval}: {e}")
                time.sleep(2)
        return pd.DataFrame()

# ==============================================================================
# CALCULATOR & LOGIC ENGINE
# ==============================================================================
class SMCQuant:
    @staticmethod
    def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        df = df.copy()
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.ewm(alpha=1/period, adjust=False).mean()
        return df

    @staticmethod
    def detect_swings(df: pd.DataFrame, lookback: int = 5) -> Tuple[pd.Series, pd.Series]:
        highs, lows = df['high'], df['low']
        sh = pd.Series(True, index=df.index)
        sl = pd.Series(True, index=df.index)
        for i in range(1, lookback + 1):
            sh &= (highs > highs.shift(i)) & (highs > highs.shift(-i))
            sl &= (lows < lows.shift(i)) & (lows < lows.shift(-i))
        return sh, sl

    @staticmethod
    def detect_choch(df_m15: pd.DataFrame, direction: str) -> bool:
        sh, sl = SMCQuant.detect_swings(df_m15, Config.M15_LOOKBACK_CHOCH)
        recent = df_m15.tail(8)
        if direction == "BULLISH":
            highs = df_m15[sh]['high'].dropna()
            return bool((recent['close'] > highs.iloc[-1]).any()) if not highs.empty else False
        else:
            lows = df_m15[sl]['low'].dropna()
            return bool((recent['close'] < lows.iloc[-1]).any()) if not lows.empty else False

def analyze_market() -> Optional[Dict[str, Any]]:
    df_d1 = MarketDataEngine.fetch_ohlcv_with_retry("1d", "1y")
    df_h1 = MarketDataEngine.fetch_ohlcv_with_retry("1h", "30d")
    df_m15 = MarketDataEngine.fetch_ohlcv_with_retry("15m", "5d")

    if df_d1.empty or df_h1.empty or df_m15.empty:
        log.error("Datafeed tidak lengkap.")
        return None

    # Resample H1 ke H4
    df_h4 = df_h1.resample("4h").agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()

    live_price = float(df_m15['close'].iloc[-1])

    # 1. Trend Filter D1
    df_d1['ema50'] = df_d1['close'].ewm(span=50, adjust=False).mean()
    df_d1['ema200'] = df_d1['close'].ewm(span=200, adjust=False).mean()
    
    last_d1 = df_d1.iloc[-1]
    if last_d1['close'] > last_d1['ema50'] > last_d1['ema200']:
        bias = "BULLISH"
    elif last_d1['close'] < last_d1['ema50'] < last_d1['ema200']:
        bias = "BEARISH"
    else:
        return None

    # 2. H4 Liquidity Sweep
    sh_h4, sl_h4 = SMCQuant.detect_swings(df_h4, Config.H4_LOOKBACK_SWING)
    recent_h4 = df_h4.tail(8)
    sweep_valid, sweep_level = False, 0.0

    if bias == "BULLISH":
        valid_lows = df_h4[sl_h4]['low'].dropna()
        if valid_lows.empty: return None
        key_low = valid_lows.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['low'] < key_low and row['close'] > key_low:
                sweep_valid, sweep_level = True, row['low']
                break
    else:
        valid_highs = df_h4[sh_h4]['high'].dropna()
        if valid_highs.empty: return None
        key_high = valid_highs.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['high'] > key_high and row['close'] < key_high:
                sweep_valid, sweep_level = True, row['high']
                break

    if not sweep_valid: return None

    # 3. M15 ChoCh
    if not SMCQuant.detect_choch(df_m15, bias): return None

    # 4. H1 FVG Retest
    df_h1 = SMCQuant.add_atr(df_h1)
    atr_h1 = df_h1['atr'].iloc[-1]
    
    fvg_found, fvg_top, fvg_bottom = False, 0.0, 0.0
    for i in range(len(df_h1) - 1, 2, -1):
        c1, c2, c3 = df_h1.iloc[i-2], df_h1.iloc[i-1], df_h1.iloc[i]
        if bias == "BULLISH" and c3['low'] > c1['high']:
            if (c3['low'] - c1['high']) >= (atr_h1 * Config.FVG_MIN_ATR_RATIO):
                if c1['high'] >= sweep_level:
                    fvg_top, fvg_bottom = c3['low'], c1['high']
                    fvg_found = True
                    break
        elif bias == "BEARISH" and c3['high'] < c1['low']:
            if (c1['low'] - c3['high']) >= (atr_h1 * Config.FVG_MIN_ATR_RATIO):
                if c1['low'] <= sweep_level:
                    fvg_top, fvg_bottom = c1['low'], c3['high']
                    fvg_found = True
                    break

    if not fvg_found or not (fvg_bottom * 0.999 <= live_price <= fvg_top * 1.001):
        return None

    # 5. Risk & Reward Calculation
    buffer = atr_h1 * Config.SL_BUFFER_ATR_MULT
    if bias == "BULLISH":
        sl = sweep_level - buffer
        sl_dist = live_price - sl
        h4_highs = df_h4[sh_h4]['high'].dropna()
        tp = h4_highs.iloc[-1] if not h4_highs.empty and h4_highs.iloc[-1] > live_price else live_price + (sl_dist * 2.5)
        tp_dist = tp - live_price
        signal_type = "BUY"
    else:
        sl = sweep_level + buffer
        sl_dist = sl - live_price
        h4_lows = df_h4[sl_h4]['low'].dropna()
        tp = h4_lows.iloc[-1] if not h4_lows.empty and h4_lows.iloc[-1] < live_price else live_price - (sl_dist * 2.5)
        tp_dist = live_price - tp
        signal_type = "SELL"

    rr = tp_dist / sl_dist if sl_dist > 0 else 0.0
    if rr < Config.MIN_RR_RATIO: return None

    lot_size = round(max(0.01, min((10000.0 * 0.01) / (sl_dist * 100), 10.0)), 2)

    return {
        "signal": signal_type,
        "price": live_price,
        "sl": sl,
        "tp": tp,
        "sl_dist": sl_dist,
        "tp_dist": tp_dist,
        "rr": rr,
        "lot": lot_size,
        "reason": f"D1 {bias} | H4 Sweep @ {sweep_level:.2f} | M15 ChoCh | H1 FVG [{fvg_bottom:.2f}-{fvg_top:.2f}]"
    }

def main():
    state_mgr = StateManager()
    setup = analyze_market()
    
    if setup:
        # Cek apakah sinyal ini spam / duplikat
        if state_mgr.is_spam(setup['signal'], setup['price']):
            log.info("Sinyal sama sudah pernah dikirim sebelumnya (Spam Prevention Active).")
            return

        now_wib = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d %b %Y, %H:%M WIB")
        emoji = "🟢" if setup['signal'] == "BUY" else "🔴"
        
        msg = (
            f"{emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
            f"⚡ <b>SINYAL MANUAL ENTRY (SMC)</b>\n"
            f"📅 Time: {now_wib}\n"
            f"──────────────────────\n"
            f"<b>AKSI: {setup['signal']} XAU/USD</b>\n"
            f"🎯 <b>Entry Zone:</b> ${setup['price']:.2f}\n"
            f"🎯 <b>Take Profit:</b> ${setup['tp']:.2f} (+${setup['tp_dist']:.2f})\n"
            f"🛑 <b>Stop Loss:</b> ${setup['sl']:.2f} (-${setup['sl_dist']:.2f})\n"
            f"⚖️ <b>Risk/Reward:</b> 1:{setup['rr']:.2f}\n"
            f"📏 <b>Saran Lot ($10k Acc):</b> {setup['lot']} Lot\n"
            f"──────────────────────\n"
            f"<b>🧠 Logika SMC:</b>\n"
            f"• {setup['reason']}\n"
            f"──────────────────────\n"
            f"<i>GitHub Actions Cloud Scanner</i>"
        )
        
        if send_telegram(msg):
            state_mgr.save(setup['signal'], setup['price'])
            log.info(f"✅ Sinyal {setup['signal']} terkirim & state berhasil disimpan!")

if __name__ == "__main__":
    main()
