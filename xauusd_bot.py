#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD HIGH PROBABILITY SCANNER v5.0
Bukan bot sinyal, tapi scanner setup langka dengan probabilitas tinggi.
Hanya memberi sinyal jika SEMUA kriteria ketat terpenuhi.

Target: 2-4 sinyal per bulan, win rate 65-70%, R:R minimal 1:2.5
"""
import os
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
# 1. KONFIGURASI (SANGAT KETAT)
# ==============================================================================
class Config:
    VERSION = "5.0"
    BOT_NAME = "XAU/USD HP Scanner"
    
    # === KRITERIA KETAT (SEMUA HARUS TERPENUHI) ===
    # 1. Daily trend harus SANGAT jelas (EMA 50 > EMA 200 atau sebaliknya)
    MIN_DAILY_EMA_SPREAD = 50.0  # Minimal $50 spread antara EMA 50 & 200
    
    # 2. H4 harus konfirmasi dengan BOS/ChoCh yang jelas
    MIN_H4_SWING_SIZE_ATR = 2.0  # Minimal 2x ATR untuk swing yang valid
    
    # 3. H1 entry harus di area liquidity sweep + Order Block
    MIN_LIQUIDITY_SWEEP_ATR = 1.5  # Sweep minimal 1.5x ATR
    
    # 4. Kill Zone WAJIB (London atau NY)
    KILL_ZONE_REQUIRED = True
    
    # 5. Volume spike WAJIB
    MIN_VOLUME_SPIKE = 1.5  # Minimal 1.5x average volume
    
    # 6. Risk:Reward minimal 1:2.5
    MIN_RR_RATIO = 2.5
    
    # === RISK MANAGEMENT ===
    ATR_PERIOD = 14
    ATR_SL_MULT = 2.0  # SL = 2x ATR (lebih ketat)
    
    # === KILL ZONE (WIB = UTC+7) ===
    KZ_LONDON = (7, 10)   # 14:00-17:00 WIB
    KZ_NY = (12, 16)      # 19:00-23:00 WIB
    
    # === SAFETY ===
    COOLDOWN_HOURS = 24  # Minimal 24 jam antar sinyal
    FETCH_DELAY = 1.5
    FETCH_TIMEOUT = 30
    MAX_TELEGRAM_LEN = 4000
    SKIP_WEEKEND = True
    STATE_FILE = ".last_signal_state.json"
    
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================================
# 2. LOGGING & TELEGRAM
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("HP_SCANNER")

def escape_html(text: str) -> str:
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    for tag in ['b', '/b', 'i', '/i', 'code', '/code']:
        text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>')
    return text

def send_telegram(text: str) -> bool:
    token, chat_id = Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram tidak dikonfigurasi.")
        return False
    text = escape_html(text)
    if len(text) > Config.MAX_TELEGRAM_LEN:
        text = text[:Config.MAX_TELEGRAM_LEN - 100] + "\n\n... (truncated)"
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False

# ==============================================================================
# 3. STATE MANAGEMENT
# ==============================================================================
class StateManager:
    def __init__(self):
        self.state_file = Path(Config.STATE_FILE)
        self.state = self._load()
    
    def _load(self) -> Dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f: return json.load(f)
            except Exception: return {}
        return {}
    
    def _save(self):
        try:
            with open(self.state_file, 'w') as f: json.dump(self.state, f, indent=2)
        except Exception as e: log.warning(f"Gagal simpan state: {e}")
    
    def can_send(self, direction: str, price: float) -> Tuple[bool, str]:
        last = self.state.get("last_signal")
        if not last: return True, ""
        last_time = datetime.fromisoformat(last["time"])
        now = datetime.now(timezone.utc)
        hours_since = (now - last_time).total_seconds() / 3600
        if hours_since < Config.COOLDOWN_HOURS:
            return False, f"Cooldown ({hours_since:.1f} jam lalu)"
        if last["direction"] == direction and abs(last["price"] - price) / price * 100 < 0.1:
            return False, f"Sinyal {direction} sama sudah dikirim"
        return True, ""
    
    def record(self, direction: str, price: float):
        self.state["last_signal"] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "direction": direction, "price": price
        }
        self._save()

# ==============================================================================
# 4. DATA FETCHER
# ==============================================================================
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def get_live_spot_price() -> float:
    for api_url in ["https://api.gold-api.com/price/XAU", "https://data-asg.goldprice.org/dbXRates/USD"]:
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                price = float(data.get("price", 0)) if "price" in data else float(data.get("items", [{}])[0].get("xauPrice", 0))
                if price > 0:
                    log.info(f"✅ Harga Spot Live: ${price:.2f}")
                    return price
        except Exception: continue
    return 0.0

def fetch_ohlcv(interval: str, limit: int = 300) -> List[Dict[str, Any]]:
    time.sleep(Config.FETCH_DELAY)
    yf_interval = {"15m": "15m", "1h": "60m", "4h": "60m", "1d": "1d"}.get(interval, interval)
    range_map = {"15m": "5d", "1h": "10d", "4h": "60d", "1d": "2y"}.get(interval, "60d")
    
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/GC=F?interval={yf_interval}&range={range_map}"
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=Config.FETCH_TIMEOUT)
                if r.status_code == 429:
                    time.sleep((2 ** attempt) + random.uniform(0, 1)); continue
                r.raise_for_status()
                data = r.json()
                result = data["chart"]["result"][0]
                ts = result.get("timestamp") or []
                q = result["indicators"]["quote"][0]
                out = []
                for i, t in enumerate(ts):
                    try:
                        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                        v = q["volume"][i] if q.get("volume") else 0
                        if None in (o, h, l, c) or h < l or o <= 0 or c <= 0: continue
                        out.append({"time": datetime.fromtimestamp(t, timezone.utc),
                                    "open": float(o), "high": float(h), "low": float(l),
                                    "close": float(c), "volume": float(v) if v else 0})
                    except (KeyError, IndexError, TypeError): continue
                
                if interval == "4h" and len(out) > 4:
                    out = aggregate_ohlcv(out, 4)
                
                log.info(f"✅ Data {interval}: {len(out)} bars")
                return out[-limit:]
            except Exception as e:
                log.warning(f"Fetch {interval} gagal (attempt {attempt+1}): {e}")
                time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"Gagal ambil data {interval}")

def aggregate_ohlcv(data: List[Dict], n: int) -> List[Dict]:
    result = []
    for i in range(0, len(data) - n + 1, n):
        chunk = data[i:i+n]
        if len(chunk) < n: break
        result.append({
            "time": chunk[0]["time"],
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(c["volume"] for c in chunk)
        })
    return result

# ==============================================================================
# 5. HELPER INDICATORS
# ==============================================================================
def calc_atr(data: List[Dict], period: int = 14) -> float:
    if len(data) < 2: return 0.0
    trs = []
    for i in range(1, len(data)):
        h, l, pc = data[i]["high"], data[i]["low"], data[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period: return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period

def calc_ema(values: List[float], period: int) -> float:
    if len(values) < period: return values[-1] if values else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def find_swing_points(data: List[Dict], lookback: int = 5) -> Dict[str, List[Dict]]:
    swing_highs, swing_lows = [], []
    for i in range(lookback, len(data) - lookback):
        is_sh = all(data[i]["high"] > data[i-j]["high"] and data[i]["high"] > data[i+j]["high"] for j in range(1, lookback+1))
        is_sl = all(data[i]["low"] < data[i-j]["low"] and data[i]["low"] < data[i+j]["low"] for j in range(1, lookback+1))
        if is_sh: swing_highs.append({"idx": i, "price": data[i]["high"], "time": data[i]["time"]})
        if is_sl: swing_lows.append({"idx": i, "price": data[i]["low"], "time": data[i]["time"]})
    return {"highs": swing_highs, "lows": swing_lows}

def is_kill_zone() -> Tuple[bool, str]:
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if Config.KZ_LONDON[0] <= hour < Config.KZ_LONDON[1]:
        return True, "London Kill Zone"
    if Config.KZ_NY[0] <= hour < Config.KZ_NY[1]:
        return True, "New York Kill Zone"
    return False, f"Outside Kill Zone (UTC {hour}:00)"

# ==============================================================================
# 6. HIGH PROBABILITY SETUP DETECTION
# ==============================================================================
def check_daily_trend(data_d1: List[Dict], live_price: float) -> Tuple[bool, str, float]:
    """KRITERIA 1: Daily trend SANGAT jelas (EMA spread minimal $50)"""
    if len(data_d1) < 200:
        return False, "Data D1 tidak cukup", 0.0
    
    closes = [d["close"] for d in data_d1]
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    spread = abs(ema50 - ema200)
    
    if spread < Config.MIN_DAILY_EMA_SPREAD:
        return False, f"Daily EMA spread terlalu kecil (${spread:.2f} < ${Config.MIN_DAILY_EMA_SPREAD})", spread
    
    if live_price > ema50 > ema200:
        return True, "Daily BULLISH kuat", spread
    elif live_price < ema50 < ema200:
        return True, "Daily BEARISH kuat", spread
    else:
        return False, "Daily tidak trending jelas", spread

def check_h4_structure(data_h4: List[Dict], daily_bias: str, atr: float) -> Tuple[bool, str]:
    """KRITERIA 2: H4 harus ada BOS/ChoCh yang jelas (minimal 2x ATR)"""
    swings = find_swing_points(data_h4, 3)
    
    if len(swings["highs"]) < 2 or len(swings["lows"]) < 2:
        return False, "H4 swing tidak cukup"
    
    last_high = swings["highs"][-1]["price"]
    prev_high = swings["highs"][-2]["price"]
    last_low = swings["lows"][-1]["price"]
    prev_low = swings["lows"][-2]["price"]
    
    current_price = data_h4[-1]["close"]
    
    if daily_bias == "bullish":
        # Cari bullish BOS: current > last_high, dan last_high - prev_high > 2x ATR
        if current_price > last_high:
            swing_size = last_high - prev_high
            if swing_size > atr * Config.MIN_H4_SWING_SIZE_ATR:
                return True, f"H4 Bullish BOS valid (${swing_size:.2f})"
        return False, "H4 tidak ada bullish BOS valid"
    
    if daily_bias == "bearish":
        # Cari bearish BOS: current < last_low, dan prev_low - last_low > 2x ATR
        if current_price < last_low:
            swing_size = prev_low - last_low
            if swing_size > atr * Config.MIN_H4_SWING_SIZE_ATR:
                return True, f"H4 Bearish BOS valid (${swing_size:.2f})"
        return False, "H4 tidak ada bearish BOS valid"
    
    return False, "Daily bias tidak jelas"

def check_h1_liquidity_sweep(data_h1: List[Dict], daily_bias: str, atr: float) -> Tuple[bool, str, float]:
    """KRITERIA 3: H1 harus ada liquidity sweep yang jelas"""
    swings = find_swing_points(data_h1, 3)
    
    if not swings["highs"] or not swings["lows"]:
        return False, "H1 swing tidak cukup", 0.0
    
    if len(data_h1) < 3:
        return False, "Data H1 tidak cukup", 0.0
    
    last = data_h1[-1]
    prev = data_h1[-2]
    
    if daily_bias == "bullish":
        # Cari sweep swing low
        last_swing_low = swings["lows"][-1]["price"]
        if prev["low"] < last_swing_low and last["close"] > last_swing_low:
            sweep_size = last_swing_low - prev["low"]
            if sweep_size > atr * Config.MIN_LIQUIDITY_SWEEP_ATR:
                # Cari Order Block bullish terdekat
                ob_level = find_nearest_bullish_ob(data_h1, last["close"], atr)
                if ob_level > 0:
                    return True, f"Liquidity sweep low ${last_swing_low:.2f} + OB ${ob_level:.2f}", ob_level
        return False, "Tidak ada liquidity sweep bullish valid", 0.0
    
    if daily_bias == "bearish":
        # Cari sweep swing high
        last_swing_high = swings["highs"][-1]["price"]
        if prev["high"] > last_swing_high and last["close"] < last_swing_high:
            sweep_size = prev["high"] - last_swing_high
            if sweep_size > atr * Config.MIN_LIQUIDITY_SWEEP_ATR:
                # Cari Order Block bearish terdekat
                ob_level = find_nearest_bearish_ob(data_h1, last["close"], atr)
                if ob_level > 0:
                    return True, f"Liquidity sweep high ${last_swing_high:.2f} + OB ${ob_level:.2f}", ob_level
        return False, "Tidak ada liquidity sweep bearish valid", 0.0
    
    return False, "Daily bias tidak jelas", 0.0

def find_nearest_bullish_ob(data: List[Dict], current_price: float, atr: float) -> float:
    """Cari Order Block bullish terdekat di bawah harga saat ini"""
    min_impulse = atr * 1.5
    for i in range(len(data) - 2, 1, -1):
        prev, curr = data[i-1], data[i]
        if prev["close"] < prev["open"] and curr["close"] - prev["low"] > min_impulse and curr["close"] > curr["open"]:
            if prev["low"] < current_price and prev["high"] > current_price - (atr * 3):
                return prev["low"]
    return 0.0

def find_nearest_bearish_ob(data: List[Dict], current_price: float, atr: float) -> float:
    """Cari Order Block bearish terdekat di atas harga saat ini"""
    min_impulse = atr * 1.5
    for i in range(len(data) - 2, 1, -1):
        prev, curr = data[i-1], data[i]
        if prev["close"] > prev["open"] and prev["high"] - curr["close"] > min_impulse and curr["close"] < curr["open"]:
            if prev["high"] > current_price and prev["low"] < current_price + (atr * 3):
                return prev["high"]
    return 0.0

def check_volume_spike(data_h1: List[Dict]) -> Tuple[bool, str]:
    """KRITERIA 5: Volume spike minimal 1.5x average"""
    if len(data_h1) < 20:
        return False, "Data volume tidak cukup"
    
    recent_vol = sum(d["volume"] for d in data_h1[-3:]) / 3
    avg_vol = sum(d["volume"] for d in data_h1[-20:]) / 20
    
    if avg_vol == 0:
        return False, "Volume nol"
    
    ratio = recent_vol / avg_vol
    if ratio >= Config.MIN_VOLUME_SPIKE:
        return True, f"Volume spike {ratio:.1f}x"
    return False, f"Volume tidak spike ({ratio:.1f}x)"

# ==============================================================================
# 7. MAIN SCANNER
# ==============================================================================
def scan_high_probability_setup() -> Dict[str, Any]:
    """Scan setup dengan kriteria SANGAT KETAT."""
    
    # Cek Kill Zone
    in_kz, kz_name = is_kill_zone()
    if Config.KILL_ZONE_REQUIRED and not in_kz:
        return {"signal": "WAIT", "reason": f"Bukan Kill Zone ({kz_name})", "checks": []}
    
    # Ambil harga live
    live_price = get_live_spot_price()
    if live_price == 0:
        return {"signal": "ERROR", "reason": "Gagal ambil harga live", "checks": []}
    
    checks = []
    
    # Fetch data
    try:
        data_d1 = fetch_ohlcv("1d", 300)
        data_h1_raw = fetch_ohlcv("1h", 300)
        data_h4 = aggregate_ohlcv(data_h1_raw, 4)
        data_h1 = data_h1_raw
    except Exception as e:
        return {"signal": "ERROR", "reason": f"Gagal fetch data: {e}", "checks": []}
    
    atr = calc_atr(data_h1, Config.ATR_PERIOD)
    
    # KRITERIA 1: Daily trend
    daily_ok, daily_reason, ema_spread = check_daily_trend(data_d1, live_price)
    checks.append({"name": "Daily Trend", "pass": daily_ok, "reason": daily_reason})
    if not daily_ok:
        return {"signal": "WAIT", "reason": daily_reason, "checks": checks, "price": live_price}
    
    daily_bias = "bullish" if "BULLISH" in daily_reason else "bearish"
    
    # KRITERIA 2: H4 structure
    h4_ok, h4_reason = check_h4_structure(data_h4, daily_bias, atr)
    checks.append({"name": "H4 Structure", "pass": h4_ok, "reason": h4_reason})
    if not h4_ok:
        return {"signal": "WAIT", "reason": h4_reason, "checks": checks, "price": live_price}
    
    # KRITERIA 3: H1 liquidity sweep
    sweep_ok, sweep_reason, ob_level = check_h1_liquidity_sweep(data_h1, daily_bias, atr)
    checks.append({"name": "H1 Sweep + OB", "pass": sweep_ok, "reason": sweep_reason})
    if not sweep_ok:
        return {"signal": "WAIT", "reason": sweep_reason, "checks": checks, "price": live_price}
    
    # KRITERIA 4: Kill Zone (sudah dicek di awal)
    checks.append({"name": "Kill Zone", "pass": True, "reason": kz_name})
    
    # KRITERIA 5: Volume spike
    vol_ok, vol_reason = check_volume_spike(data_h1)
    checks.append({"name": "Volume Spike", "pass": vol_ok, "reason": vol_reason})
    if not vol_ok:
        return {"signal": "WAIT", "reason": vol_reason, "checks": checks, "price": live_price}
    
    # SEMUA KRITERIA TERPENUHI → Hitung SL/TP
    sl_distance = atr * Config.ATR_SL_MULT
    
    if daily_bias == "bullish":
        sl_price = ob_level if ob_level > 0 else live_price - sl_distance
        sl_distance = live_price - sl_price
        tp_price = live_price + (sl_distance * Config.MIN_RR_RATIO)
        direction = "BUY"
    else:
        sl_price = ob_level if ob_level > 0 else live_price + sl_distance
        sl_distance = sl_price - live_price
        tp_price = live_price - (sl_distance * Config.MIN_RR_RATIO)
        direction = "SELL"
    
    tp_distance = abs(tp_price - live_price)
    rr_ratio = tp_distance / sl_distance if sl_distance > 0 else Config.MIN_RR_RATIO
    
    # KRITERIA 6: R:R minimal 1:2.5
    if rr_ratio < Config.MIN_RR_RATIO:
        return {
            "signal": "WAIT",
            "reason": f"R:R terlalu kecil (1:{rr_ratio:.1f} < 1:{Config.MIN_RR_RATIO})",
            "checks": checks,
            "price": live_price
        }
    
    checks.append({"name": "R:R Ratio", "pass": True, "reason": f"1:{rr_ratio:.1f}"})
    
    return {
        "signal": direction,
        "price": live_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_distance": sl_distance,
        "tp_distance": tp_distance,
        "rr_ratio": rr_ratio,
        "atr": atr,
        "checks": checks,
        "kill_zone": kz_name
    }

# ==============================================================================
# 8. MESSAGE FORMATTER
# ==============================================================================
def format_message(result: Dict) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d %b %Y %H:%M UTC")
    now_wib = now + timedelta(hours=7)
    time_wib = now_wib.strftime("%H:%M WIB")
    
    if result["signal"] in ["WAIT", "ERROR"]:
        msg = (
            f"<b>⏸️ {Config.BOT_NAME} v{Config.VERSION} — WAIT</b>\n"
            f"📅 {date_str} ({time_wib})\n"
        )
        if "price" in result:
            msg += f"💰 XAU/USD: ${result['price']:.2f}\n"
        msg += f"──────────────────────\n<b>Alasan:</b> {result['reason']}\n"
        
        if "checks" in result and result["checks"]:
            msg += f"──────────────────────\n<b>📋 Checklist:</b>\n"
            for check in result["checks"]:
                emoji = "✅" if check["pass"] else "❌"
                msg += f"  {emoji} <b>{check['name']}</b>: {check['reason']}\n"
        
        msg += f"──────────────────────\n<i>🎯 Scanner butuh SEMUA kriteria terpenuhi.\nSetup langka = kualitas tinggi.</i>"
        return msg
    
    # Sinyal aktif
    direction = result["signal"]
    dir_emoji = "🟢" if direction == "BUY" else "🔴"
    
    msg = (
        f"{dir_emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
        f"🔥 <b>HIGH PROBABILITY SETUP</b>\n"
        f"📅 {date_str} ({time_wib})\n"
        f"⏰ <b>{result['kill_zone']}</b>\n"
        f"──────────────────────\n"
        f"<b>SINYAL: {direction}</b>\n"
        f"💰 Entry: ${result['price']:.2f}\n"
        f"🎯 TP: ${result['tp_price']:.2f} (+${result['tp_distance']:.2f})\n"
        f"🛑 SL: ${result['sl_price']:.2f} (-${result['sl_distance']:.2f})\n"
        f"📊 R:R = 1:{result['rr_ratio']:.1f}\n"
        f"📏 ATR: ${result['atr']:.2f}\n"
        f"──────────────────────\n"
        f"<b>✅ Checklist (SEMUA terpenuhi):</b>\n"
    )
    
    for check in result["checks"]:
        msg += f"  ✅ <b>{check['name']}</b>: {check['reason']}\n"
    
    msg += (
        f"──────────────────────\n"
        f"<b>💼 Lot Size (1% risiko):</b>\n"
        f"  Modal $1,000 → 0.01 lot\n"
        f"  Modal $5,000 → 0.05 lot\n"
        f"  Modal $10,000 → 0.10 lot\n"
        f"──────────────────────\n"
        f"<b>⚠️ ATURAN WAJIB:</b>\n"
        f"• WAJIB pasang SL di ${result['sl_price']:.2f}\n"
        f"• Max 1% modal per trade\n"
        f"• Setup langka (2-4x/bulan)\n"
        f"• Konfirmasi manual dengan chart\n"
        f"──────────────────────\n"
        f"<i>🎯 High Probability Setup:\n"
        f"Semua kriteria ketat terpenuhi.\n"
        f"Tetap forward test di DEMO!</i>"
    )
    
    return msg

# ==============================================================================
# 9. MAIN EXECUTION
# ==============================================================================
def run():
    log.info(f"=== 🚀 {Config.BOT_NAME} v{Config.VERSION} (High Probability Scanner) ===")
    
    if Config.SKIP_WEEKEND:
        now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
        if now_wib.weekday() >= 5:
            log.info("Weekend — skip")
            return
    
    result = scan_high_probability_setup()
    
    # Anti-loop
    state_mgr = StateManager()
    if result["signal"] in ["BUY", "SELL"]:
        can_send, reason = state_mgr.can_send(result["signal"], result.get("price", 0))
        if not can_send:
            log.info(f"Anti-loop: {reason}")
            return
    
    # Send
    msg = format_message(result)
    log.info("\n" + msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    
    if send_telegram(msg) and result["signal"] in ["BUY", "SELL"]:
        state_mgr.record(result["signal"], result["price"])
        log.info(f"✅ Sinyal {result['signal']} tercatat")

# ==============================================================================
# 10. ENTRY POINT
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
