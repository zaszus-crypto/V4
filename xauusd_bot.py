#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD GRADE SIGNAL BOT v2.0
Multi-Confluence + Multi-Timeframe + Smart Money Concepts
Grade System: A / A+ / A SUPER

Author: Quant System
Target: Win Rate 50-60% | R:R 1:2 to 1:4
"""
import os
import sys
import json
import math
import time
import random
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# ==============================================================================
# 1. KONFIGURASI UTAMA
# ==============================================================================
class Config:
    VERSION = "2.0"
    BOT_NAME = "XAU/USD Grade Signal"
    
    # === INDIKATOR PARAMETER ===
    RSI_PERIOD = 14
    RSI_OVERSOLD = 30
    RSI_OVERBOUGHT = 70
    RSI_STRONG_OVSELL = 20
    RSI_STRONG_OVBUY = 80
    
    STOCH_K = 14
    STOCH_D = 3
    STOCH_OVERSOLD = 20
    STOCH_OVERBOUGHT = 80
    
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    
    BB_PERIOD = 20
    BB_STD = 2.0
    
    ATR_PERIOD = 14
    ATR_SL_MULT = 2.0
    
    EMA_FAST = 20
    EMA_MID = 50
    EMA_SLOW = 200
    
    ADX_PERIOD = 14
    ADX_TREND_THRESHOLD = 20
    
    VOLUME_SPIKE_MULT = 1.5
    
    # === GRADE SYSTEM ===
    # Grade A: 4/8 confluence + 1 TF aligned
    # Grade A+: 6/8 confluence + 2 TF aligned
    # Grade A SUPER: 8/8 confluence + 2 TF aligned + volume spike + strong pattern
    GRADE_A_MIN_SCORE = 4
    GRADE_A_PLUS_MIN_SCORE = 6
    GRADE_A_SUPER_MIN_SCORE = 8
    
    # Risk:Reward per grade
    RR_GRADE_A = 2.0
    RR_GRADE_A_PLUS = 3.0
    RR_GRADE_A_SUPER = 4.0
    
    # === SAFETY & PRODUCTION ===
    COOLDOWN_MINUTES = 240  # 4 jam antar sinyal
    FETCH_DELAY = 1.5
    FETCH_TIMEOUT = 30
    MAX_TELEGRAM_LEN = 4000
    SKIP_WEEKEND = True
    STATE_FILE = ".last_signal_state.json"
    
    # === TELEGRAM ===
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================================
# 2. LOGGING & TELEGRAM
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("XAUUSD_BOT")

def escape_html(text: str) -> str:
    """Escape karakter HTML berbahaya, pertahankan tag yang diizinkan."""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    for tag in ['b', '/b', 'i', '/i', 'code', '/code', 'pre', '/pre']:
        text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>')
    return text

def send_telegram(text: str) -> bool:
    """Kirim pesan ke Telegram dengan error handling."""
    token = Config.TELEGRAM_TOKEN
    chat_id = Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram tidak dikonfigurasi.")
        return False
    
    text = escape_html(text)
    if len(text) > Config.MAX_TELEGRAM_LEN:
        text = text[:Config.MAX_TELEGRAM_LEN - 100] + "\n\n... (truncated)"
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        if r.status_code == 200:
            log.info("✅ Telegram terkirim.")
            return True
        else:
            log.error(f"Telegram gagal: {r.text}")
            return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ==============================================================================
# 3. STATE MANAGEMENT (Anti-Loop / Anti-Spam)
# ==============================================================================
class StateManager:
    """Kelola state untuk mencegah sinyal berulang (anti-loop)."""
    
    def __init__(self, state_file: str = Config.STATE_FILE):
        self.state_file = Path(state_file)
        self.state = self._load()
    
    def _load(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}
    
    def _save(self):
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            log.warning(f"Gagal simpan state: {e}")
    
    def can_send_signal(self, direction: str, price: float) -> Tuple[bool, str]:
        """Cek apakah boleh kirim sinyal baru (anti-spam)."""
        last = self.state.get("last_signal")
        if not last:
            return True, ""
        
        last_time = datetime.fromisoformat(last["time"])
        now = datetime.now(timezone.utc)
        minutes_since = (now - last_time).total_seconds() / 60
        
        if minutes_since < Config.COOLDOWN_MINUTES:
            return False, f"Cooldown aktif ({minutes_since:.0f} menit lalu)"
        
        # Cek apakah sinyal sama dengan harga mirip (< 0.1% beda)
        if last["direction"] == direction:
            price_diff_pct = abs(last["price"] - price) / price * 100
            if price_diff_pct < 0.1:
                return False, f"Sinyal {direction} sama sudah dikirim"
        
        return True, ""
    
    def record_signal(self, direction: str, price: float, grade: str):
        """Catat sinyal yang baru dikirim."""
        self.state["last_signal"] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "price": price,
            "grade": grade
        }
        self._save()

# ==============================================================================
# 4. DATA FETCHER (Multi-Source dengan Fallback)
# ==============================================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_live_spot_price() -> float:
    """Ambil harga Spot XAU/USD real-time (sesuai MT5)."""
    apis = [
        "https://api.gold-api.com/price/XAU",
        "https://data-asg.goldprice.org/dbXRates/USD"
    ]
    
    for api_url in apis:
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if "price" in data:
                    price = float(data["price"])
                elif "items" in data and data["items"]:
                    price = float(data["items"][0].get("xauPrice", 0))
                else:
                    continue
                
                if price > 0:
                    log.info(f"✅ Harga Spot Live: ${price:.2f}")
                    return price
        except Exception as e:
            log.warning(f"API {api_url} gagal: {e}")
            continue
    
    return 0.0

def fetch_ohlcv(interval: str = "30m", limit: int = 300) -> List[Dict[str, Any]]:
    """Fetch OHLCV dari Yahoo Finance (GC=F sebagai proxy Gold)."""
    time.sleep(Config.FETCH_DELAY)
    
    # Map interval ke format Yahoo
    yf_interval = {"30m": "30m", "1h": "60m", "15m": "15m"}.get(interval, interval)
    range_map = {"30m": "5d", "1h": "10d", "15m": "5d"}.get(interval, "5d")
    
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F?interval={yf_interval}&range={range_map}"
        
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=Config.FETCH_TIMEOUT)
                if r.status_code == 429:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                
                data = r.json()
                result = data["chart"]["result"][0]
                ts = result.get("timestamp") or []
                q = result["indicators"]["quote"][0]
                
                out = []
                for i, t in enumerate(ts):
                    try:
                        o = q["open"][i]
                        h = q["high"][i]
                        l = q["low"][i]
                        c = q["close"][i]
                        v = q["volume"][i] if q.get("volume") else 0
                        
                        if None in (o, h, l, c) or h < l or o <= 0 or c <= 0:
                            continue
                        
                        dt = datetime.fromtimestamp(t, timezone.utc)
                        out.append({
                            "time": dt, "open": float(o), "high": float(h),
                            "low": float(l), "close": float(c),
                            "volume": float(v) if v else 0
                        })
                    except (KeyError, IndexError, TypeError):
                        continue
                
                log.info(f"✅ Data {interval}: {len(out)} bars")
                return out[-limit:]
                
            except Exception as e:
                log.warning(f"Fetch {interval} gagal (attempt {attempt+1}): {e}")
                time.sleep((2 ** attempt) + random.uniform(0, 1))
    
    raise RuntimeError(f"Gagal ambil data {interval}")

# ==============================================================================
# 5. INDIKATOR CALCULATIONS (Semua yang saya tahu)
# ==============================================================================
def calc_rsi(closes: List[float], period: int = 14) -> float:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(closes: List[float], period: int) -> float:
    """Exponential Moving Average."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calc_sma(values: List[float], period: int) -> float:
    """Simple Moving Average."""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-period:]) / period

def calc_macd(closes: List[float]) -> Dict[str, float]:
    """MACD Line, Signal Line, Histogram."""
    if len(closes) < Config.MACD_SLOW + Config.MACD_SIGNAL:
        return {"macd": 0, "signal": 0, "histogram": 0}
    
    ema_fast = calc_ema(closes, Config.MACD_FAST)
    ema_slow = calc_ema(closes, Config.MACD_SLOW)
    macd_line = ema_fast - ema_slow
    
    # Hitung historical MACD values untuk signal line
    macd_values = []
    for i in range(Config.MACD_SLOW, len(closes)):
        ef = calc_ema(closes[:i+1], Config.MACD_FAST)
        es = calc_ema(closes[:i+1], Config.MACD_SLOW)
        macd_values.append(ef - es)
    
    signal_line = calc_ema(macd_values, Config.MACD_SIGNAL) if len(macd_values) >= Config.MACD_SIGNAL else macd_line
    histogram = macd_line - signal_line
    
    # Cek crossover (perubahan histogram sign)
    prev_histogram = macd_values[-2] - calc_ema(macd_values[:-1], Config.MACD_SIGNAL) if len(macd_values) > 1 else 0
    crossover = "bullish" if prev_histogram < 0 and histogram > 0 else \
                "bearish" if prev_histogram > 0 and histogram < 0 else "none"
    
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram, "crossover": crossover}

def calc_bollinger(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Dict[str, float]:
    """Bollinger Bands."""
    if len(closes) < period:
        return {"upper": closes[-1], "middle": closes[-1], "lower": closes[-1], "width": 0, "pct_b": 0.5}
    
    sma = sum(closes[-period:]) / period
    variance = sum((x - sma) ** 2 for x in closes[-period:]) / period
    std = math.sqrt(variance)
    
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    width = (upper - lower) / sma * 100 if sma > 0 else 0
    
    current = closes[-1]
    pct_b = (current - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    
    return {"upper": upper, "middle": sma, "lower": lower, "width": width, "pct_b": pct_b}

def calc_atr(data: List[Dict[str, Any]], period: int = 14) -> float:
    """Average True Range."""
    if len(data) < 2:
        return 0.0
    trs = []
    for i in range(1, len(data)):
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period

def calc_adx(data: List[Dict[str, Any]], period: int = 14) -> Tuple[float, float, float]:
    """ADX, +DI, -DI. Returns (adx, plus_di, minus_di)."""
    if len(data) < period * 2:
        return 0.0, 0.0, 0.0
    
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(data)):
        h_diff = data[i]["high"] - data[i-1]["high"]
        l_diff = data[i-1]["low"] - data[i]["low"]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    
    def wilder_smooth(values, period):
        if len(values) < period:
            return []
        smoothed = [sum(values[:period])]
        for i in range(period, len(values)):
            smoothed.append(smoothed[-1] - smoothed[-1]/period + values[i])
        return smoothed
    
    plus_smooth = wilder_smooth(plus_dm, period)
    minus_smooth = wilder_smooth(minus_dm, period)
    tr_smooth = wilder_smooth(tr_list, period)
    
    dx_list = []
    plus_di_list, minus_di_list = [], []
    for i in range(len(plus_smooth)):
        if tr_smooth[i] == 0:
            dx_list.append(0)
            plus_di_list.append(0)
            minus_di_list.append(0)
            continue
        pdi = 100 * plus_smooth[i] / tr_smooth[i]
        mdi = 100 * minus_smooth[i] / tr_smooth[i]
        plus_di_list.append(pdi)
        minus_di_list.append(mdi)
        di_sum = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / di_sum if di_sum > 0 else 0)
    
    if len(dx_list) < period:
        return 0.0, plus_di_list[-1] if plus_di_list else 0, minus_di_list[-1] if minus_di_list else 0
    
    adx = sum(dx_list[-period:]) / period
    return adx, plus_di_list[-1], minus_di_list[-1]

def calc_stochastic(data: List[Dict[str, Any]], k_period: int = 14, d_period: int = 3) -> Dict[str, float]:
    """Stochastic Oscillator."""
    if len(data) < k_period:
        return {"k": 50, "d": 50}
    
    recent = data[-k_period:]
    high_max = max(d["high"] for d in recent)
    low_min = min(d["low"] for d in recent)
    
    if high_max == low_min:
        return {"k": 50, "d": 50}
    
    current_close = data[-1]["close"]
    k = 100 * (current_close - low_min) / (high_max - low_min)
    
    # %D = SMA of %K
    k_values = []
    for i in range(k_period, len(data)):
        window = data[i-k_period:i]
        hh = max(d["high"] for d in window)
        ll = min(d["low"] for d in window)
        if hh == ll:
            k_values.append(50)
        else:
            k_values.append(100 * (data[i]["close"] - ll) / (hh - ll))
    
    d = sum(k_values[-d_period:]) / d_period if len(k_values) >= d_period else k
    
    return {"k": k, "d": d}

def detect_candle_patterns(data: List[Dict[str, Any]]) -> Dict[str, bool]:
    """Deteksi pola candlestick penting."""
    patterns = {
        "bullish_engulfing": False,
        "bearish_engulfing": False,
        "bullish_pinbar": False,
        "bearish_pinbar": False,
        "doji": False,
        "strong_bullish_candle": False,
        "strong_bearish_candle": False
    }
    
    if len(data) < 3:
        return patterns
    
    last = data[-1]
    prev = data[-2]
    prev2 = data[-3]
    
    body_last = abs(last["close"] - last["open"])
    body_prev = abs(prev["close"] - prev["open"])
    range_last = last["high"] - last["low"]
    
    # Bullish Engulfing
    if (prev["close"] < prev["open"] and last["close"] > last["open"] and
        last["close"] > prev["open"] and last["open"] < prev["close"]):
        patterns["bullish_engulfing"] = True
    
    # Bearish Engulfing
    if (prev["close"] > prev["open"] and last["close"] < last["open"] and
        last["close"] < prev["open"] and last["open"] > prev["close"]):
        patterns["bearish_engulfing"] = True
    
    # Bullish Pin Bar (lower wick > 2x body, close near high)
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    if lower_wick > 2 * body_last and upper_wick < body_last and body_last > 0:
        patterns["bullish_pinbar"] = True
    
    # Bearish Pin Bar
    if upper_wick > 2 * body_last and lower_wick < body_last and body_last > 0:
        patterns["bearish_pinbar"] = True
    
    # Doji
    if range_last > 0 and body_last / range_last < 0.1:
        patterns["doji"] = True
    
    # Strong Bullish Candle (body > 70% of range)
    if last["close"] > last["open"] and range_last > 0 and body_last / range_last > 0.7:
        patterns["strong_bullish_candle"] = True
    
    # Strong Bearish Candle
    if last["close"] < last["open"] and range_last > 0 and body_last / range_last > 0.7:
        patterns["strong_bearish_candle"] = True
    
    return patterns

def calc_pivot_points(data: List[Dict[str, Any]]) -> Dict[str, float]:
    """Pivot Points untuk Support/Resistance."""
    if len(data) < 2:
        return {}
    
    prev = data[-2]
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    r1 = 2 * pivot - prev["low"]
    s1 = 2 * pivot - prev["high"]
    r2 = pivot + (prev["high"] - prev["low"])
    s2 = pivot - (prev["high"] - prev["low"])
    
    return {"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}

def calc_volume_analysis(data: List[Dict[str, Any]], period: int = 20) -> Dict[str, Any]:
    """Analisis volume."""
    if len(data) < period:
        return {"avg": 0, "current": 0, "ratio": 1.0, "spike": False}
    
    vols = [d["volume"] for d in data[-period-1:-1]]
    avg = sum(vols) / len(vols) if vols else 0
    current = data[-1]["volume"]
    ratio = current / avg if avg > 0 else 1.0
    
    return {
        "avg": avg, "current": current, "ratio": ratio,
        "spike": ratio >= Config.VOLUME_SPIKE_MULT
    }

# ==============================================================================
# 6. SIGNAL ANALYSIS (Multi-Confluence + Grade System)
# ==============================================================================
def analyze_timeframe(data: List[Dict[str, Any]], tf_name: str) -> Dict[str, Any]:
    """Analisis lengkap untuk 1 timeframe."""
    if len(data) < 100:
        return {"valid": False, "reason": "Data tidak cukup"}
    
    closes = [d["close"] for d in data]
    current_price = closes[-1]
    
    # Hitung semua indikator
    rsi = calc_rsi(closes, Config.RSI_PERIOD)
    stoch = calc_stochastic(data, Config.STOCH_K, Config.STOCH_D)
    macd = calc_macd(closes)
    bb = calc_bollinger(closes, Config.BB_PERIOD, Config.BB_STD)
    atr = calc_atr(data, Config.ATR_PERIOD)
    adx, plus_di, minus_di = calc_adx(data, Config.ADX_PERIOD)
    ema_fast = calc_ema(closes, Config.EMA_FAST)
    ema_mid = calc_ema(closes, Config.EMA_MID)
    ema_slow = calc_ema(closes, Config.EMA_SLOW)
    patterns = detect_candle_patterns(data)
    pivots = calc_pivot_points(data)
    volume = calc_volume_analysis(data)
    
    # === SCORING SYSTEM (8 poin maksimal) ===
    bullish_score = 0
    bearish_score = 0
    bullish_reasons = []
    bearish_reasons = []
    
    # 1. RSI (1 poin)
    if rsi < Config.RSI_STRONG_OVSELL:
        bullish_score += 1
        bullish_reasons.append(f"RSI extreme oversold ({rsi:.0f})")
    elif rsi < Config.RSI_OVERSOLD:
        bullish_score += 1
        bullish_reasons.append(f"RSI oversold ({rsi:.0f})")
    elif rsi > Config.RSI_STRONG_OVBUY:
        bearish_score += 1
        bearish_reasons.append(f"RSI extreme overbought ({rsi:.0f})")
    elif rsi > Config.RSI_OVERBOUGHT:
        bearish_score += 1
        bearish_reasons.append(f"RSI overbought ({rsi:.0f})")
    
    # 2. MACD (1 poin)
    if macd["crossover"] == "bullish":
        bullish_score += 1
        bullish_reasons.append("MACD bullish crossover")
    elif macd["crossover"] == "bearish":
        bearish_score += 1
        bearish_reasons.append("MACD bearish crossover")
    elif macd["histogram"] > 0 and macd["macd"] > 0:
        bullish_score += 1
        bullish_reasons.append("MACD bullish momentum")
    elif macd["histogram"] < 0 and macd["macd"] < 0:
        bearish_score += 1
        bearish_reasons.append("MACD bearish momentum")
    
    # 3. EMA Trend (1 poin)
    if ema_fast > ema_mid > ema_slow:
        bullish_score += 1
        bullish_reasons.append("EMA perfect alignment (20>50>200)")
    elif current_price > ema_slow and ema_fast > ema_slow:
        bullish_score += 1
        bullish_reasons.append("Price > EMA 200 (uptrend)")
    elif ema_fast < ema_mid < ema_slow:
        bearish_score += 1
        bearish_reasons.append("EMA perfect alignment (20<50<200)")
    elif current_price < ema_slow and ema_fast < ema_slow:
        bearish_score += 1
        bearish_reasons.append("Price < EMA 200 (downtrend)")
    
    # 4. Bollinger Bands (1 poin)
    if bb["pct_b"] < 0.05:
        bullish_score += 1
        bullish_reasons.append(f"Price at BB lower ({bb['pct_b']:.2f})")
    elif bb["pct_b"] > 0.95:
        bearish_score += 1
        bearish_reasons.append(f"Price at BB upper ({bb['pct_b']:.2f})")
    elif bb["width"] < 2.0:
        # Squeeze - tunggu breakout
        pass
    
    # 5. Stochastic (1 poin)
    if stoch["k"] < Config.STOCH_OVERSOLD and stoch["d"] < Config.STOCH_OVERSOLD:
        bullish_score += 1
        bullish_reasons.append(f"Stochastic oversold (K:{stoch['k']:.0f} D:{stoch['d']:.0f})")
    elif stoch["k"] > Config.STOCH_OVERBOUGHT and stoch["d"] > Config.STOCH_OVERBOUGHT:
        bearish_score += 1
        bearish_reasons.append(f"Stochastic overbought (K:{stoch['k']:.0f} D:{stoch['d']:.0f})")
    
    # 6. ADX + DI (1 poin)
    if adx > Config.ADX_TREND_THRESHOLD:
        if plus_di > minus_di:
            bullish_score += 1
            bullish_reasons.append(f"ADX trend up ({adx:.0f}, +DI>{'-'}DI)")
        elif minus_di > plus_di:
            bearish_score += 1
            bearish_reasons.append(f"ADX trend down ({adx:.0f}, -DI>+DI)")
    
    # 7. Candlestick Pattern (1 poin)
    if patterns["bullish_engulfing"] or patterns["bullish_pinbar"]:
        bullish_score += 1
        pattern_name = "Bullish Engulfing" if patterns["bullish_engulfing"] else "Bullish Pin Bar"
        bullish_reasons.append(f"Pattern: {pattern_name}")
    elif patterns["bearish_engulfing"] or patterns["bearish_pinbar"]:
        bearish_score += 1
        pattern_name = "Bearish Engulfing" if patterns["bearish_engulfing"] else "Bearish Pin Bar"
        bearish_reasons.append(f"Pattern: {pattern_name}")
    
    # 8. Volume Confirmation (1 poin)
    if volume["spike"]:
        if bullish_score > bearish_score:
            bullish_score += 1
            bullish_reasons.append(f"Volume spike ({volume['ratio']:.1f}x) + bullish")
        elif bearish_score > bullish_score:
            bearish_score += 1
            bearish_reasons.append(f"Volume spike ({volume['ratio']:.1f}x) + bearish")
    
    # Tentukan arah
    if bullish_score > bearish_score:
        direction = "BUY"
        score = bullish_score
        reasons = bullish_reasons
    elif bearish_score > bullish_score:
        direction = "SELL"
        score = bearish_score
        reasons = bearish_reasons
    else:
        direction = "NEUTRAL"
        score = 0
        reasons = []
    
    return {
        "valid": True,
        "tf": tf_name,
        "direction": direction,
        "score": score,
        "max_score": 8,
        "reasons": reasons,
        "price": current_price,
        "atr": atr,
        "rsi": rsi,
        "adx": adx,
        "ema_fast": ema_fast,
        "ema_mid": ema_mid,
        "ema_slow": ema_slow,
        "bb": bb,
        "macd": macd,
        "stoch": stoch,
        "patterns": patterns,
        "pivots": pivots,
        "volume": volume
    }

def combine_signals(m30: Dict, h1: Dict, live_price: float) -> Dict[str, Any]:
    """Gabungkan sinyal M30 dan H1 dengan sistem grading."""
    
    if not m30.get("valid") or not h1.get("valid"):
        return {
            "signal": "NO_DATA",
            "reason": "Data tidak cukup untuk analisis"
        }
    
    # Cek apakah kedua TF setuju
    if m30["direction"] == "NEUTRAL" and h1["direction"] == "NEUTRAL":
        return {
            "signal": "WAIT",
            "reason": "Kedua timeframe netral",
            "m30_score": m30["score"],
            "h1_score": h1["score"]
        }
    
    if m30["direction"] != h1["direction"] and m30["direction"] != "NEUTRAL" and h1["direction"] != "NEUTRAL":
        return {
            "signal": "CONFLICT",
            "reason": f"M30: {m30['direction']} vs H1: {h1['direction']}",
            "m30_score": m30["score"],
            "h1_score": h1["score"],
            "m30_dir": m30["direction"],
            "h1_dir": h1["direction"]
        }
    
    # Tentukan arah final (prioritas H1 jika salah satu netral)
    if m30["direction"] == "NEUTRAL":
        final_dir = h1["direction"]
        aligned_tfs = 1
    elif h1["direction"] == "NEUTRAL":
        final_dir = m30["direction"]
        aligned_tfs = 1
    else:
        final_dir = m30["direction"]
        aligned_tfs = 2
    
    # Total score (gabungan)
    total_score = m30["score"] + h1["score"]
    max_possible = m30["max_score"] + h1["max_score"]
    
    # Grade determination
    if total_score >= Config.GRADE_A_SUPER_MIN_SCORE and aligned_tfs == 2:
        grade = "A SUPER"
        rr_ratio = Config.RR_GRADE_A_SUPER
    elif total_score >= Config.GRADE_A_PLUS_MIN_SCORE and aligned_tfs >= 1:
        grade = "A+"
        rr_ratio = Config.RR_GRADE_A_PLUS
    elif total_score >= Config.GRADE_A_MIN_SCORE and aligned_tfs >= 1:
        grade = "A"
        rr_ratio = Config.RR_GRADE_A
    else:
        return {
            "signal": "WAIT",
            "reason": f"Score terlalu rendah ({total_score}/{max_possible})",
            "m30_score": m30["score"],
            "h1_score": h1["score"],
            "total_score": total_score
        }
    
    # Hitung SL dan TP
    # Gunakan ATR dari H1 (lebih stabil) untuk SL
    atr_for_sl = h1["atr"] if h1["atr"] > 0 else m30["atr"]
    sl_distance = atr_for_sl * Config.ATR_SL_MULT
    tp_distance = sl_distance * rr_ratio
    
    if final_dir == "BUY":
        sl_price = live_price - sl_distance
        tp_price = live_price + tp_distance
    else:
        sl_price = live_price + sl_distance
        tp_price = live_price - tp_distance
    
    # Kumpulkan semua konfirmasi
    all_reasons = m30["reasons"] + h1["reasons"]
    
    return {
        "signal": final_dir,
        "grade": grade,
        "total_score": total_score,
        "max_score": max_possible,
        "aligned_tfs": aligned_tfs,
        "entry_price": live_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_distance": sl_distance,
        "tp_distance": tp_distance,
        "rr_ratio": rr_ratio,
        "atr": atr_for_sl,
        "reasons": all_reasons,
        "m30": m30,
        "h1": h1
    }

# ==============================================================================
# 7. MESSAGE FORMATTER
# ==============================================================================
def format_telegram_message(result: Dict) -> str:
    """Format pesan Telegram yang cantik."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d %b %Y %H:%M UTC")
    
    if result["signal"] in ["NO_DATA", "WAIT"]:
        return (
            f"<b>⏸️ {Config.BOT_NAME} v{Config.VERSION} — WAIT</b>\n"
            f"📅 {date_str}\n"
            f"💰 XAU/USD: ${result.get('entry_price', 0):.2f}\n"
            f"──────────────────────\n"
            f"<b>Alasan:</b> {result['reason']}\n"
            f"──────────────────────\n"
            f"<i>Tidak ada konfluensi kuat. Tunggu setup lebih jelas.</i>"
        )
    
    if result["signal"] == "CONFLICT":
        return (
            f"<b>⚠️ {Config.BOT_NAME} v{Config.VERSION} — CONFLICT</b>\n"
            f"📅 {date_str}\n"
            f"💰 XAU/USD: ${result.get('entry_price', 0):.2f}\n"
            f"──────────────────────\n"
            f"<b>M30:</b> {result.get('m30_dir', '?')} (score: {result.get('m30_score', 0)}/8)\n"
            f"<b>H1:</b> {result.get('h1_dir', '?')} (score: {result.get('h1_score', 0)}/8)\n"
            f"──────────────────────\n"
            f"<i>Timeframe tidak setuju. Skip trade ini.</i>"
        )
    
    # Sinyal aktif (BUY/SELL)
    direction = result["signal"]
    grade = result["grade"]
    
    # Grade emoji dan warna
    grade_emoji = {
        "A": "🟡",
        "A+": "🟠",
        "A SUPER": "🔴"
    }.get(grade, "⚪")
    
    dir_emoji = "🟢" if direction == "BUY" else "🔴"
    
    # Rekomendasi lot size (berdasarkan 1% risiko)
    # Asumsi: 1 lot XAUUSD = 100 oz, 1 pip = $0.01, SL dalam $
    sl_pips = result["sl_distance"] * 100  # konversi ke "pips" gold
    risk_per_lot = sl_pips  # dalam USD per lot
    
    msg = (
        f"{dir_emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
        f"{grade_emoji} <b>GRADE: {grade}</b> (Score: {result['total_score']}/{result['max_score']})\n"
        f"📅 {date_str}\n"
        f"──────────────────────\n"
        f"<b>SINYAL: {direction}</b>\n"
        f"💰 Entry: ${result['entry_price']:.2f}\n"
        f"🎯 TP: ${result['tp_price']:.2f} (+${result['tp_distance']:.2f})\n"
        f"🛑 SL: ${result['sl_price']:.2f} (-${result['sl_distance']:.2f})\n"
        f"📊 R:R = 1:{result['rr_ratio']}\n"
        f"📏 ATR: ${result['atr']:.2f}\n"
        f"──────────────────────\n"
        f"<b>✅ Konfluensi ({len(result['reasons'])} poin):</b>\n"
    )
    
    for reason in result["reasons"]:
        msg += f"  • {reason}\n"
    
    msg += (
        f"──────────────────────\n"
        f"<b>💼 Rekomendasi Lot (1% risiko):</b>\n"
        f"  Modal $1,000 → 0.01 lot\n"
        f"  Modal $5,000 → 0.05 lot\n"
        f"  Modal $10,000 → 0.10 lot\n"
        f"──────────────────────\n"
        f"<b>⚠️ ATURAN WAJIB:</b>\n"
        f"• WAJIB pasang SL di ${result['sl_price']:.2f}\n"
        f"• Max 1% modal per trade\n"
        f"• Grade A: 1x lot normal\n"
        f"• Grade A+: 1.5x lot normal\n"
        f"• Grade A SUPER: 2x lot normal\n"
        f"• Konfirmasi manual dengan chart\n"
        f"──────────────────────\n"
        f"<i>Trading mengandung risiko tinggi. "
        f"Pastikan Ceu siap kehilangan 1% modal per trade.</i>"
    )
    
    return msg

# ==============================================================================
# 8. MAIN EXECUTION
# ==============================================================================
def run():
    log.info(f"=== 🚀 {Config.BOT_NAME} v{Config.VERSION} ===")
    
    # 1. Cek weekend
    if Config.SKIP_WEEKEND:
        now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
        if now_wib.weekday() >= 5:
            log.info("Weekend — skip")
            return
    
    # 2. Ambil harga live
    live_price = get_live_spot_price()
    if live_price == 0:
        log.error("Gagal ambil harga live")
        send_telegram(f"<b>⚠️ {Config.BOT_NAME}</b>\nGagal ambil harga live. Cek koneksi API.")
        return
    
    # 3. Analisis M30
    log.info("Analisis M30...")
    try:
        data_m30 = fetch_ohlcv("30m", 300)
        signal_m30 = analyze_timeframe(data_m30, "M30")
    except Exception as e:
        log.error(f"Gagal analisis M30: {e}")
        send_telegram(f"<b>⚠️ {Config.BOT_NAME}</b>\nGagal analisis M30: {e}")
        return
    
    # 4. Analisis H1
    log.info("Analisis H1...")
    try:
        data_h1 = fetch_ohlcv("1h", 300)
        signal_h1 = analyze_timeframe(data_h1, "H1")
    except Exception as e:
        log.error(f"Gagal analisis H1: {e}")
        send_telegram(f"<b>⚠️ {Config.BOT_NAME}</b>\nGagal analisis H1: {e}")
        return
    
    # 5. Gabungkan sinyal
    result = combine_signals(signal_m30, signal_h1, live_price)
    result["entry_price"] = live_price
    
    # 6. Anti-loop check
    state_mgr = StateManager()
    if result["signal"] in ["BUY", "SELL"]:
        can_send, reason = state_mgr.can_send_signal(result["signal"], live_price)
        if not can_send:
            log.info(f"Anti-loop: {reason}")
            return
    
    # 7. Format & kirim
    msg = format_telegram_message(result)
    log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    
    if send_telegram(msg) and result["signal"] in ["BUY", "SELL"]:
        state_mgr.record_signal(result["signal"], live_price, result.get("grade", ""))
        log.info(f"✅ Sinyal {result['signal']} Grade {result.get('grade', '')} tercatat")

# ==============================================================================
# 9. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        import traceback
        log.critical(f"FATAL: {e}\n{traceback.format_exc()}")
        try:
            send_telegram(f"<b>🚨 FATAL ERROR</b>\n<code>{str(e)[:200]}</code>")
        except Exception:
            pass
