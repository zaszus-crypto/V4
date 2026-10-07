#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD PREDATOR v9.0 "ULTIMATE" - Single File Edition
Multi-Logic Confluence: SMC + Session + EMA Trend + Volatility + RSI Divergence
"""

import os
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
import mplfinance as mpf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

try:
    import yfinance as yf
except ImportError:
    print("❌ Install yfinance dulu: pip install yfinance")
    exit(1)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
class Config:
    BOT_NAME = "XAU/USD PREDATOR"
    VERSION = "9.0-ULTIMATE"
    SYMBOL = os.getenv("SYMBOL", "GC=F")

    # Risk Management
    ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "10000.0"))
    RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))
    CONTRACT_SIZE = float(os.getenv("CONTRACT_SIZE", "100.0"))
    MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "2.0"))
    COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "4"))

    # SMC Parameters
    H4_LOOKBACK_SWING = 5
    M15_LOOKBACK_CHOCH = 3
    FVG_MIN_ATR_RATIO = 0.35
    FVG_TOLERANCE = 0.002
    SL_BUFFER_ATR_MULT = 0.5

    # Session Filter (WIB)
    SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "13"))
    SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "23"))

    # Trend & Volatility
    EMA_FAST = 50
    EMA_SLOW = 200
    RSI_PERIOD = 14
    RSI_DIV_LOOKBACK = 20
    MIN_ATR_PERCENTILE = 30

    # Files & Credentials
    STATE_FILE = "predator_state.json"
    CHART_FILE = "chart_signal.png"
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
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
            "last_price": float(price),
            "last_time": datetime.now(timezone.utc).isoformat()
        }
        try:
            with open(self.file_path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            log.error(f"Gagal menyimpan state: {e}")

    def is_spam(self, signal_type: str, price: float) -> bool:
        if "last_time" not in self.data or price <= 0:
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
# TECHNICAL INDICATORS
# ==============================================================================
class Indicators:
    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

# ==============================================================================
# MARKET FILTERS
# ==============================================================================
class MarketFilters:
    @staticmethod
    def check_session() -> Tuple[bool, str]:
        now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
        hour = now_wib.hour
        if Config.SESSION_START_HOUR <= hour <= Config.SESSION_END_HOUR:
            return True, f"Sesi Aktif ({hour}:00 WIB)"
        return False, f"Di luar sesi optimal ({hour}:00 WIB)"

    @staticmethod
    def check_trend_alignment(df_h4: pd.DataFrame, bias: str) -> Tuple[bool, str]:
        df = df_h4.copy()
        df['ema_fast'] = Indicators.ema(df['close'], Config.EMA_FAST)
        df['ema_slow'] = Indicators.ema(df['close'], Config.EMA_SLOW)
        last = df.iloc[-1]
        if bias == "BULLISH":
            if last['close'] > last['ema_fast'] > last['ema_slow']:
                return True, "H4 Trend Bullish (EMA 50 > 200)"
            return False, "H4 Trend tidak mendukung BUY"
        else:
            if last['close'] < last['ema_fast'] < last['ema_slow']:
                return True, "H4 Trend Bearish (EMA 50 < 200)"
            return False, "H4 Trend tidak mendukung SELL"

    @staticmethod
    def check_volatility(df_h1: pd.DataFrame) -> Tuple[bool, str]:
        atr = Indicators.atr(df_h1, 14)
        current_atr = atr.iloc[-1]
        percentile = (atr < current_atr).mean() * 100
        if percentile >= Config.MIN_ATR_PERCENTILE:
            return True, f"Volatilitas Aktif (ATR P{percentile:.0f}%)"
        return False, f"Pasar terlalu sepi (ATR P{percentile:.0f}%)"

    @staticmethod
    def check_rsi_divergence(df_m15: pd.DataFrame, direction: str) -> Tuple[bool, str]:
        df = df_m15.copy()
        df['rsi'] = Indicators.rsi(df['close'], Config.RSI_PERIOD)
        recent = df.tail(Config.RSI_DIV_LOOKBACK)
        if len(recent) < 10:
            return False, "Data RSI tidak cukup"
        first_half = recent.iloc[:10]
        second_half = recent.iloc[10:]
        if direction == "BULLISH":
            p_prev, p_curr = first_half['low'].min(), second_half['low'].min()
            r_prev, r_curr = first_half['rsi'].min(), second_half['rsi'].min()
            if p_curr < p_prev and r_curr > r_prev:
                return True, f"Bullish RSI Div (RSI: {r_curr:.1f})"
            return False, "Tidak ada Bullish RSI Div"
        else:
            p_prev, p_curr = first_half['high'].max(), second_half['high'].max()
            r_prev, r_curr = first_half['rsi'].max(), second_half['rsi'].max()
            if p_curr > p_prev and r_curr < r_prev:
                return True, f"Bearish RSI Div (RSI: {r_curr:.1f})"
            return False, "Tidak ada Bearish RSI Div"

# ==============================================================================
# SMC QUANT ENGINE
# ==============================================================================
class SMCQuant:
    @staticmethod
    def detect_confirmed_swings(df: pd.DataFrame, lookback: int = 5) -> Tuple[pd.Series, pd.Series]:
        highs, lows = df['high'], df['low']
        rolling_high = highs.rolling(window=2*lookback+1, center=True).max()
        rolling_low = lows.rolling(window=2*lookback+1, center=True).min()
        sh = (highs.shift(lookback) == rolling_high.shift(lookback))
        sl = (lows.shift(lookback) == rolling_low.shift(lookback))
        return sh.fillna(False), sl.fillna(False)

    @staticmethod
    def detect_choch(df_m15: pd.DataFrame, direction: str) -> bool:
        sh, sl = SMCQuant.detect_confirmed_swings(df_m15, Config.M15_LOOKBACK_CHOCH)
        recent = df_m15.tail(8)
        if direction == "BULLISH":
            highs = df_m15[sh]['high'].dropna()
            if highs.empty: return False
            return bool((recent['close'] > highs.iloc[-1]).any())
        else:
            lows = df_m15[sl]['low'].dropna()
            if lows.empty: return False
            return bool((recent['close'] < lows.iloc[-1]).any())

    @staticmethod
    def find_fvg(df_h1: pd.DataFrame, bias: str, sweep_level: float, atr_h1: float) -> Tuple[bool, float, float]:
        for i in range(len(df_h1) - 1, 2, -1):
            c1, c2, c3 = df_h1.iloc[i-2], df_h1.iloc[i-1], df_h1.iloc[i]
            if bias == "BULLISH" and c3['low'] > c1['high']:
                if (c3['low'] - c1['high']) >= (atr_h1 * Config.FVG_MIN_ATR_RATIO):
                    if c1['high'] >= sweep_level:
                        return True, c3['low'], c1['high']
            elif bias == "BEARISH" and c3['high'] < c1['low']:
                if (c1['low'] - c3['high']) >= (atr_h1 * Config.FVG_MIN_ATR_RATIO):
                    if c1['low'] <= sweep_level:
                        return True, c1['low'], c3['high']
        return False, 0.0, 0.0

# ==============================================================================
# DATA ENGINE
# ==============================================================================
class MarketDataEngine:
    @staticmethod
    def fetch(interval: str, period: str, retries: int = 3) -> pd.DataFrame:
        for attempt in range(retries):
            try:
                df = yf.Ticker(Config.SYMBOL).history(period=period, interval=interval)
                if not df.empty and len(df) >= 10:
                    df.columns = [c.lower() for c in df.columns]
                    required = ['open', 'high', 'low', 'close', 'volume']
                    if all(col in df.columns for col in required):
                        df = df[required].dropna()
                        df.index = pd.to_datetime(df.index, utc=True)
                        return df
            except Exception as e:
                log.warning(f"Attempt {attempt+1} gagal {interval}: {e}")
                time.sleep(2)
        return pd.DataFrame()

# ==============================================================================
# CHART & TELEGRAM
# ==============================================================================
def generate_chart(df_m15: pd.DataFrame, setup: Dict[str, Any]) -> str:
    try:
        df_plot = df_m15.tail(40).copy()
        df_plot.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'}, inplace=True)
        mc = mpf.make_marketcolors(up='#00B57C', down='#FF3B30', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True, rc={'font.size': 9})
        hlines = dict(
            hlines=[setup['price'], setup['tp'], setup['sl']],
            colors=['#0088CC', '#00B57C', '#FF3B30'],
            linestyle='--', linewidths=1.5
        )
        chart_path = Config.CHART_FILE
        mpf.plot(df_plot, type='candle', style=s, hlines=hlines,
                 title=f"\nXAU/USD ({setup['signal']}) - M15",
                 savefig=dict(fname=chart_path, dpi=150, bbox_inches='tight'), figscale=1.2)
        return chart_path
    except Exception as e:
        log.error(f"Gagal membuat chart: {e}")
        return ""

def send_telegram(caption: str, image_path: str) -> bool:
    token, chat_id = Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.warning("Telegram credentials tidak lengkap.")
        return False
    url_photo = f"https://api.telegram.org/bot{token}/sendPhoto"
    url_text = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            if not image_path or not os.path.exists(image_path):
                r = requests.post(url_text, json={"chat_id": chat_id, "text": caption, "parse_mode": "HTML"}, timeout=15)
            else:
                with open(image_path, 'rb') as photo:
                    r = requests.post(url_photo, data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                                      files={'photo': photo}, timeout=20)
            if r.status_code == 200:
                return True
            log.warning(f"Telegram gagal (Attempt {attempt+1}): {r.status_code}")
            time.sleep(2)
        except Exception as e:
            log.error(f"Error Telegram (Attempt {attempt+1}): {e}")
            time.sleep(2)
    return False

# ==============================================================================
# MAIN ANALYSIS PIPELINE
# ==============================================================================
def analyze_market() -> Tuple[Optional[Dict[str, Any]], pd.DataFrame, list]:
    filters_passed = []

    ok, msg = MarketFilters.check_session()
    log.info(f"[FILTER 1 - Session] {msg}")
    if not ok:
        return None, pd.DataFrame(), []
    filters_passed.append(msg)

    df_d1 = MarketDataEngine.fetch("1d", "1y")
    df_h1 = MarketDataEngine.fetch("1h", "30d")
    df_m15 = MarketDataEngine.fetch("15m", "5d")
    if df_d1.empty or df_h1.empty or df_m15.empty:
        log.error("Datafeed tidak lengkap.")
        return None, pd.DataFrame(), []

    df_h4 = df_h1.resample("4h", offset="0h").agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    if len(df_h4) < 10:
        return None, df_m15, []

    live_price = float(df_m15['close'].iloc[-1])

    df_d1['ema50'] = Indicators.ema(df_d1['close'], 50)
    df_d1['ema200'] = Indicators.ema(df_d1['close'], 200)
    last_d1 = df_d1.iloc[-1]
    if last_d1['close'] > last_d1['ema50'] > last_d1['ema200']:
        bias = "BULLISH"
    elif last_d1['close'] < last_d1['ema50'] < last_d1['ema200']:
        bias = "BEARISH"
    else:
        log.info("D1 Bias Netral.")
        return None, df_m15, []
    filters_passed.append(f"D1 Bias: {bias}")

    ok, msg = MarketFilters.check_trend_alignment(df_h4, bias)
    log.info(f"[FILTER 2 - Trend] {msg}")
    if not ok:
        return None, df_m15, []
    filters_passed.append(msg)

    sh_h4, sl_h4 = SMCQuant.detect_confirmed_swings(df_h4, Config.H4_LOOKBACK_SWING)
    recent_h4 = df_h4.tail(8)
    sweep_valid, sweep_level = False, 0.0
    if bias == "BULLISH":
        valid_lows = df_h4[sl_h4]['low'].dropna()
        if valid_lows.empty: return None, df_m15, []
        key_low = valid_lows.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['low'] < key_low and row['close'] > key_low:
                sweep_valid, sweep_level = True, row['low']; break
    else:
        valid_highs = df_h4[sh_h4]['high'].dropna()
        if valid_highs.empty: return None, df_m15, []
        key_high = valid_highs.iloc[-1]
        for _, row in recent_h4.iterrows():
            if row['high'] > key_high and row['close'] < key_high:
                sweep_valid, sweep_level = True, row['high']; break
    if not sweep_valid:
        log.info("Tidak ada H4 Sweep.")
        return None, df_m15, []
    filters_passed.append(f"H4 Sweep @ {sweep_level:.2f}")

    ok, msg = MarketFilters.check_volatility(df_h1)
    log.info(f"[FILTER 3 - Volatility] {msg}")
    if not ok:
        return None, df_m15, []
    filters_passed.append(msg)

    if not SMCQuant.detect_choch(df_m15, bias):
        log.info("Tidak ada M15 ChoCh.")
        return None, df_m15, []
    filters_passed.append("M15 ChoCh Confirmed")

    ok, msg = MarketFilters.check_rsi_divergence(df_m15, bias)
    log.info(f"[FILTER 4 - RSI] {msg}")
    if not ok:
        return None, df_m15, []
    filters_passed.append(msg)

    df_h1['atr'] = Indicators.atr(df_h1, 14)
    atr_h1 = df_h1['atr'].iloc[-1]
    fvg_found, fvg_top, fvg_bottom = SMCQuant.find_fvg(df_h1, bias, sweep_level, atr_h1)
    tolerance = Config.FVG_TOLERANCE
    price_near_fvg = fvg_found and (fvg_bottom * (1 - tolerance) <= live_price <= fvg_top * (1 + tolerance))
    if not price_near_fvg:
        log.info("Tidak ada FVG valid.")
        return None, df_m15, []
    filters_passed.append(f"H1 FVG [{fvg_bottom:.2f}-{fvg_top:.2f}]")

    buffer = atr_h1 * Config.SL_BUFFER_ATR_MULT
    if bias == "BULLISH":
        sl = sweep_level - buffer
        sl_dist = live_price - sl
        h4_highs = df_h4[sh_h4]['high'].dropna()
        tp = h4_highs.iloc[-1] if not h4_highs.empty and h4_highs.iloc[-1] > live_price else live_price + (sl_dist * Config.MIN_RR_RATIO)
        tp_dist = tp - live_price
        signal_type = "BUY"
    else:
        sl = sweep_level + buffer
        sl_dist = sl - live_price
        h4_lows = df_h4[sl_h4]['low'].dropna()
        tp = h4_lows.iloc[-1] if not h4_lows.empty and h4_lows.iloc[-1] < live_price else live_price - (sl_dist * Config.MIN_RR_RATIO)
        tp_dist = live_price - tp
        signal_type = "SELL"

    rr = tp_dist / sl_dist if sl_dist > 0 else 0.0
    if rr < Config.MIN_RR_RATIO:
        log.info(f"RR terlalu rendah ({rr:.2f}).")
        return None, df_m15, []
    filters_passed.append(f"RR 1:{rr:.2f}")

    risk_amount = Config.ACCOUNT_BALANCE * (Config.RISK_PERCENT / 100.0)
    risk_per_lot = sl_dist * Config.CONTRACT_SIZE
    lot_size = risk_amount / risk_per_lot if risk_per_lot > 0 else 0.01
    lot_size = round(max(0.01, min(lot_size, 10.0)), 2)

    setup_data = {
        "signal": signal_type, "price": live_price, "sl": sl, "tp": tp,
        "sl_dist": sl_dist, "tp_dist": tp_dist, "rr": rr, "lot": lot_size
    }
    return setup_data, df_m15, filters_passed

# ==============================================================================
# MAIN ENTRY
# ==============================================================================
def main():
    try:
        state_mgr = StateManager()
        setup, df_m15, filters_passed = analyze_market()

        if setup:
            if state_mgr.is_spam(setup['signal'], setup['price']):
                log.info("Sinyal sama sudah terkirim (Anti-Spam).")
                return

            chart_file = generate_chart(df_m15, setup)
            now_wib = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d %b %Y, %H:%M WIB")
            emoji = "🟢" if setup['signal'] == "BUY" else "🔴"
            filters_text = "\n".join([f"  ✅ {f}" for f in filters_passed])

            caption = (
                f"{emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
                f"⚡ <b>SINYAL MULTI-CONFLUENCE</b>\n"
                f"📅 Time: {now_wib}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>AKSI: {setup['signal']} XAU/USD</b>\n"
                f"🔵 <b>Entry:</b> ${setup['price']:.2f}\n"
                f"🟢 <b>TP:</b> ${setup['tp']:.2f} (+${setup['tp_dist']:.2f})\n"
                f"🔴 <b>SL:</b> ${setup['sl']:.2f} (-${setup['sl_dist']:.2f})\n"
                f"⚖️ <b>RR:</b> 1:{setup['rr']:.2f}\n"
                f"📏 <b>Lot (${Config.ACCOUNT_BALANCE:.0f}):</b> {setup['lot']}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>🧠 CONFLUENCE CHECKLIST:</b>\n"
                f"{filters_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>🔵 Entry | 🟢 TP | 🔴 SL</i>"
            )

            if send_telegram(caption, chart_file):
                state_mgr.save(setup['signal'], setup['price'])
                log.info(f"✅ Sinyal {setup['signal']} berhasil dikirim!")
        else:
            log.info("Tidak ada setup valid.")
    except Exception as e:
        log.critical(f"CRITICAL ERROR: {e}", exc_info=True)

if __name__ == "__main__":
    main()
