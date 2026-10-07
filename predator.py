#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD PREDATOR v8.7 (SERVERLESS WITH AUTO CHART GENERATOR)
------------------------------------------------------------------
Features:
1. Multi-Timeframe SMC (D1 Bias -> H4 Sweep -> M15 ChoCh -> H1 FVG)
2. Auto Generate Candlestick Chart (Entry, SL, TP Lines)
3. Send Telegram Photo + Caption Signal
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
import mplfinance as mpf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

class Config:
    BOT_NAME = "XAU/USD PREDATOR"
    VERSION = "8.7-CHART"
    SYMBOL = "GC=F"
    
    RISK_PERCENT = 1.0
    MIN_RR_RATIO = 2.0
    COOLDOWN_HOURS = 4
    
    H4_LOOKBACK_SWING = 5
    M15_LOOKBACK_CHOCH = 3
    FVG_MIN_ATR_RATIO = 0.35
    SL_BUFFER_ATR_MULT = 0.5
    STATE_FILE = "predator_state.json"
    CHART_FILE = "chart_signal.png"
    
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("PREDATOR")

# ==============================================================================
# STATE MANAGER
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
        
        if hours_passed < Config.COOLDOWN_HOURS:
            if self.data.get("last_signal") == signal_type:
                price_diff = abs(price - self.data.get("last_price", 0.0)) / price
                if price_diff < 0.0015:
                    return True
        return False

# ==============================================================================
# CHART GENERATOR ENGINE
# ==============================================================================
def generate_chart(df_m15: pd.DataFrame, setup: Dict[str, Any]) -> str:
    """Membuat grafik Candlestick M15 dengan garis Entry, SL, dan TP"""
    try:
        # Ambil 40 candle terakhir
        df_plot = df_m15.tail(40).copy()
        
        # Rename kolom untuk mplfinance
        df_plot.rename(columns={
            'open': 'Open', 'high': 'High', 
            'low': 'Low', 'close': 'Close', 'volume': 'Volume'
        }, inplace=True)

        # Style grafik (Dark Mode Theme)
        mc = mpf.make_marketcolors(
            up='#00B57C', down='#FF3B30',
            edge='inherit', wick='inherit', volume='in'
        )
        s = mpf.make_mpf_style(
            marketcolors=mc, gridstyle=':', 
            y_on_right=True, rc={'font.size': 9}
        )

        # Hapus penanda garis harga horizontal
        hlines = dict(
            hlines=[setup['price'], setup['tp'], setup['sl']],
            colors=['#0088CC', '#00B57C', '#FF3B30'],
            linestyle='--', linewidths=1.5
        )

        # Simpan chart sebagai file PNG
        chart_path = Config.CHART_FILE
        mpf.plot(
            df_plot,
            type='candle',
            style=s,
            hlines=hlines,
            title=f"\nXAU/USD ({setup['signal']}) - M15 Chart",
            savefig=dict(fname=chart_path, dpi=150, bbox_inches='tight'),
            figscale=1.2
        )
        return chart_path
    except Exception as e:
        log.error(f"Gagal membuat chart gambar: {e}")
        return ""

# ==============================================================================
# TELEGRAM PHOTO SENDER
# ==============================================================================
def send_telegram_photo(caption: str, image_path: str) -> bool:
    token, chat_id = Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.warning("Telegram Credentials tidak lengkap.")
        return False
        
    try:
        # Jika gambar gagal dibuat, kirim pesan teks biasa
        if not image_path or not os.path.exists(image_path):
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"}
            r = requests.post(url, json=payload, timeout=15)
            return r.status_code == 200

        # Send Photo API Endpoint
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        
        with open(image_path, 'rb') as photo:
            files = {'photo': photo}
            r = requests.post(url, data=payload, files=files, timeout=20)
            
        return r.status_code == 200
    except Exception as e:
        log.error(f"Error kirim photo ke Telegram: {e}")
        return False

# ==============================================================================
# DATA ENGINE & SMC CALCULATOR
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
                log.warning(f"Attempt {attempt+1} gagal download {interval}: {e}")
                time.sleep(2)
        return pd.DataFrame()

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

def analyze_market() -> Tuple[Optional[Dict[str, Any]], pd.DataFrame]:
    df_d1 = MarketDataEngine.fetch_ohlcv_with_retry("1d", "1y")
    df_h1 = MarketDataEngine.fetch_ohlcv_with_retry("1h", "30d")
    df_m15 = MarketDataEngine.fetch_ohlcv_with_retry("15m", "5d")

    if df_d1.empty or df_h1.empty or df_m15.empty:
        log.error("Datafeed tidak lengkap.")
        return None, pd.DataFrame()

    df_h4 = df_h1.resample("4h").agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()

    live_price = float(df_m15['close'].iloc[-1])

    # 1. Bias D1
    df_d1['ema50'] = df_d1['close'].ewm(span=50, adjust=False).mean()
    df_d1['ema200'] = df_d1['close'].ewm(span=200, adjust=False).mean()
    last_d1 = df_d1.iloc[-1]
    
    if last_d1['close'] > last_d1['ema50'] > last_d1['ema200']:
        bias = "BULLISH"
    elif last_d1['close'] < last_d1['ema50'] < last_d1['ema200']:
        bias = "BEARISH"
    else:
        return None, df_m15

    # 2. H4 Sweep
    sh_h4, sl_h4 = SMCQuant.detect_swings(df_h4, Config.H4_LOOKBACK_SWING)
    recent_h4 = df_h4.tail(8)
    sweep_valid, sweep_level = False, 0.0

    if bias == "BULLISH":
        valid_lows = df_h4[sl_h4]['low'].dropna()
        if valid_lows.empty: return None, df_m15
        key_low = valid_lows.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['low'] < key_low and row['close'] > key_low:
                sweep_valid, sweep_level = True, row['low']
                break
    else:
        valid_highs = df_h4[sh_h4]['high'].dropna()
        if valid_highs.empty: return None, df_m15
        key_high = valid_highs.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['high'] > key_high and row['close'] < key_high:
                sweep_valid, sweep_level = True, row['high']
                break

    if not sweep_valid: return None, df_m15

    # 3. M15 ChoCh
    if not SMCQuant.detect_choch(df_m15, bias): return None, df_m15

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
        return None, df_m15

    # 5. Risk & Reward
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
    if rr < Config.MIN_RR_RATIO: return None, df_m15

    lot_size = round(max(0.01, min((10000.0 * 0.01) / (sl_dist * 100), 10.0)), 2)

    setup_data = {
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
    return setup_data, df_m15

def main():
    state_mgr = StateManager()
    setup, df_m15 = analyze_market()
    
    if setup:
        if state_mgr.is_spam(setup['signal'], setup['price']):
            log.info("Sinyal sama sudah terkirim (Anti-Spam).")
            return

        # Generate Chart
        chart_file = generate_chart(df_m15, setup)

        now_wib = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d %b %Y, %H:%M WIB")
        emoji = "🟢" if setup['signal'] == "BUY" else "🔴"
        
        caption = (
            f"{emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
            f"⚡ <b>SINYAL MANUAL ENTRY (SMC)</b>\n"
            f"📅 Time: {now_wib}\n"
            f"──────────────────────\n"
            f"<b>AKSI: {setup['signal']} XAU/USD</b>\n"
            f"🔵 <b>Entry Zone:</b> ${setup['price']:.2f}\n"
            f"🟢 <b>Take Profit:</b> ${setup['tp']:.2f} (+${setup['tp_dist']:.2f})\n"
            f"🔴 <b>Stop Loss:</b> ${setup['sl']:.2f} (-${setup['sl_dist']:.2f})\n"
            f"⚖️ <b>Risk/Reward:</b> 1:{setup['rr']:.2f}\n"
            f"📏 <b>Saran Lot ($10k):</b> {setup['lot']} Lot\n"
            f"──────────────────────\n"
            f"<b>🧠 Logika SMC:</b>\n"
            f"• {setup['reason']}\n"
            f"──────────────────────\n"
            f"<i>Garis Putus-Putus: Biru=Entry | Hijau=TP | Merah=SL</i>"
        )
        
        if send_telegram_photo(caption, chart_file):
            state_mgr.save(setup['signal'], setup['price'])
            log.info(f"✅ Sinyal {setup['signal']} + Chart berhasil dikirim ke Telegram!")

if __name__ == "__main__":
    main()
