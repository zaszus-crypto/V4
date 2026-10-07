#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD PREDATOR v6.1 (SUPER GRADE - FIXED)
Logika: Liquidity Sweep -> ChoCh -> Retest FVG -> Entry
SL: Di bawah/atas Wick Sweep (Anti-Hunt)
TP: Di Likuiditas Swing berikutnya
Aktif: 24 jam Senin-Jumat | Libur: Sabtu-Minggu
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
# 1. KONFIGURASI
# ==============================================================================
class Config:
    VERSION = "6.1"
    BOT_NAME = "XAU/USD PREDATOR"
    
    # === JADWAL OPERASIONAL ===
    # Aktif 24 jam Senin-Jumat, libur total Sabtu-Minggu
    SKIP_WEEKEND = True
    
    # === PARAMETER TEKNIS ===
    SWING_LOOKBACK = 5
    FVG_MIN_SIZE_ATR = 0.5
    SL_WICK_BUFFER_ATR = 0.3
    SWEEP_LOOKBACK_CANDLES = 5  # Cek 5 candle terakhir untuk sweep
    
    # === SAFETY ===
    COOLDOWN_HOURS = 8
    FETCH_DELAY = 1.0
    FETCH_TIMEOUT = 30
    MAX_TELEGRAM_LEN = 4000
    STATE_FILE = ".predator_state.json"
    
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==============================================================================
# 2. LOGGING & TELEGRAM
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("PREDATOR")

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
        except Exception: pass
    
    def can_send(self, direction: str, price: float) -> Tuple[bool, str]:
        last = self.state.get("last_signal")
        if not last: return True, ""
        last_time = datetime.fromisoformat(last["time"])
        now = datetime.now(timezone.utc)
        hours_since = (now - last_time).total_seconds() / 3600
        if hours_since < Config.COOLDOWN_HOURS:
            return False, f"Cooldown ({hours_since:.1f} jam lalu)"
        if last["direction"] == direction and abs(last["price"] - price) / price * 100 < 0.1:
            return False, "Sinyal sama sudah dikirim"
        return True, ""
    
    def record(self, direction: str, price: float):
        self.state["last_signal"] = {"time": datetime.now(timezone.utc).isoformat(), "direction": direction, "price": price}
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
                if price > 0: return price
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
                return out[-limit:]
            except Exception:
                time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"Gagal ambil data {interval}")

def aggregate_ohlcv(data: List[Dict], n: int) -> List[Dict]:
    result = []
    for i in range(0, len(data) - n + 1, n):
        chunk = data[i:i+n]
        if len(chunk) < n: break
        result.append({"time": chunk[0]["time"], "open": chunk[0]["open"],
                       "high": max(c["high"] for c in chunk), "low": min(c["low"] for c in chunk),
                       "close": chunk[-1]["close"], "volume": sum(c["volume"] for c in chunk)})
    return result

# ==============================================================================
# 5. INDICATOR CALCULATIONS (FIXED)
# ==============================================================================
def calc_atr(data: List[Dict], period: int = 14) -> float:
    if len(data) < 2: return 0.0
    trs = [max(data[i]["high"] - data[i]["low"], abs(data[i]["high"] - data[i-1]["close"]), abs(data[i]["low"] - data[i-1]["close"])) for i in range(1, len(data))]
    return sum(trs[-period:]) / period if len(trs) >= period else (sum(trs) / len(trs) if trs else 0.0)

def calc_ema(values: List[float], period: int) -> float:
    """EMA calculation yang BENAR"""
    if len(values) < period: return values[-1] if values else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def find_swing_points(data: List[Dict], lookback: int = 5) -> Dict[str, List[Dict]]:
    highs, lows = [], []
    for i in range(lookback, len(data) - lookback):
        if all(data[i]["high"] > data[i-j]["high"] and data[i]["high"] > data[i+j]["high"] for j in range(1, lookback+1)):
            highs.append({"idx": i, "price": data[i]["high"], "time": data[i]["time"]})
        if all(data[i]["low"] < data[i-j]["low"] and data[i]["low"] < data[i+j]["low"] for j in range(1, lookback+1)):
            lows.append({"idx": i, "price": data[i]["low"], "time": data[i]["time"]})
    return {"highs": highs, "lows": lows}

def find_fvg(data: List[Dict], atr: float) -> List[Dict]:
    fvgs = []
    for i in range(2, len(data)):
        c1, c2, c3 = data[i-2], data[i-1], data[i]
        if c3["low"] > c1["high"] and (c3["low"] - c1["high"]) > (atr * Config.FVG_MIN_SIZE_ATR):
            fvgs.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"], "idx": i-1})
        elif c3["high"] < c1["low"] and (c1["low"] - c3["high"]) > (atr * Config.FVG_MIN_SIZE_ATR):
            fvgs.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"], "idx": i-1})
    return fvgs[-10:]  # Ambil 10 FVG terakhir

def is_weekend() -> bool:
    """Cek apakah sekarang Sabtu atau Minggu"""
    now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
    return now_wib.weekday() >= 5  # 5 = Saturday, 6 = Sunday

# ==============================================================================
# 6. CORE LOGIC: THE PREDATOR (FIXED)
# ==============================================================================
def analyze_predator_setup(data_d1: List[Dict], data_h4: List[Dict], data_h1: List[Dict], live_price: float) -> Dict[str, Any]:
    atr_h1 = calc_atr(data_h1, 14)
    if atr_h1 == 0: return {"signal": "WAIT", "reason": "ATR tidak valid"}
    
    # 1. D1 Trend Filter (EMA yang BENAR)
    closes_d1 = [d["close"] for d in data_d1]
    if len(closes_d1) < 200: return {"signal": "WAIT", "reason": "Data D1 kurang"}
    ema50 = calc_ema(closes_d1, 50)
    ema200 = calc_ema(closes_d1, 200)
    
    if live_price > ema50 > ema200:
        bias = "bullish"
    elif live_price < ema50 < ema200:
        bias = "bearish"
    else:
        return {"signal": "WAIT", "reason": "D1 Netral, tidak ada arah jelas"}
    
    # 2. H4 Liquidity Sweep Detection (FIXED: cek beberapa candle terakhir)
    swings_h4 = find_swing_points(data_h4, 3)
    sweep_valid = False
    sweep_level = 0.0
    
    if bias == "bullish" and swings_h4["lows"]:
        last_low = swings_h4["lows"][-1]["price"]
        # Cek 5 candle terakhir untuk sweep
        for i in range(1, min(Config.SWEEP_LOOKBACK_CANDLES + 1, len(data_h4))):
            candle = data_h4[-i]
            if candle["low"] < last_low and candle["close"] > last_low:
                sweep_valid = True
                sweep_level = candle["low"]
                break
            
    elif bias == "bearish" and swings_h4["highs"]:
        last_high = swings_h4["highs"][-1]["price"]
        for i in range(1, min(Config.SWEEP_LOOKBACK_CANDLES + 1, len(data_h4))):
            candle = data_h4[-i]
            if candle["high"] > last_high and candle["close"] < last_high:
                sweep_valid = True
                sweep_level = candle["high"]
                break
            
    if not sweep_valid:
        return {"signal": "WAIT", "reason": "Belum ada Liquidity Sweep di H4"}
    
    # 3. H1 FVG Detection (FIXED: retest pattern)
    fvgs_h1 = find_fvg(data_h1, atr_h1)
    
    target_fvg = None
    if bias == "bullish":
        # Cari FVG Bullish yang sudah pernah di-retest
        for fvg in fvgs_h1:
            if fvg["type"] == "bullish" and fvg["bottom"] > sweep_level:
                # Cek apakah harga pernah masuk FVG dan sekarang di dekat FVG
                if live_price >= fvg["bottom"] and live_price <= fvg["top"] * 1.002:  # 0.2% tolerance
                    target_fvg = fvg
                    break
    else:
        for fvg in fvgs_h1:
            if fvg["type"] == "bearish" and fvg["top"] < sweep_level:
                if live_price <= fvg["top"] and live_price >= fvg["bottom"] * 0.998:
                    target_fvg = fvg
                    break
                
    if not target_fvg:
        return {"signal": "WAIT", "reason": "Harga belum retest ke area FVG valid"}
    
    # 4. Kalkulasi SL & TP (Anti-Hunt)
    if bias == "bullish":
        sl_price = sweep_level - (atr_h1 * Config.SL_WICK_BUFFER_ATR)
        sl_distance = live_price - sl_price
        
        # TP di swing high H4 berikutnya (dengan validasi)
        if swings_h4["highs"]:
            tp_price = swings_h4["highs"][-1]["price"]
        else:
            tp_price = live_price + (sl_distance * 2.5)
        
        tp_distance = tp_price - live_price
        direction = "BUY"
    else:
        sl_price = sweep_level + (atr_h1 * Config.SL_WICK_BUFFER_ATR)
        sl_distance = sl_price - live_price
        
        if swings_h4["lows"]:
            tp_price = swings_h4["lows"][-1]["price"]
        else:
            tp_price = live_price - (sl_distance * 2.5)
        
        tp_distance = live_price - tp_price
        direction = "SELL"
        
    rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0
    
    if rr_ratio < 2.0:
        return {"signal": "WAIT", "reason": f"R:R tidak memenuhi syarat (1:{rr_ratio:.1f})"}
    
    return {
        "signal": direction,
        "entry": live_price,
        "sl": sl_price,
        "tp": tp_price,
        "sl_dist": sl_distance,
        "tp_dist": tp_distance,
        "rr": rr_ratio,
        "reason": f"D1 {bias.upper()} | H4 Sweep @ {sweep_level:.2f} | H1 Retest FVG"
    }

# ==============================================================================
# 7. MAIN EXECUTION
# ==============================================================================
def run():
    log.info(f"=== 🚀 {Config.BOT_NAME} v{Config.VERSION} ===")
    
    # Cek weekend
    if Config.SKIP_WEEKEND and is_weekend():
        log.info("Weekend (Sabtu/Minggu) — bot libur")
        return
    
    live_price = get_live_spot_price()
    if live_price == 0:
        send_telegram(f"<b>⚠️ {Config.BOT_NAME}</b>\nGagal ambil harga live.")
        return
    
    try:
        data_d1 = fetch_ohlcv("1d", 300)
        data_h1_raw = fetch_ohlcv("1h", 300)
        data_h4 = aggregate_ohlcv(data_h1_raw, 4)
        data_h1 = data_h1_raw
    except Exception as e:
        send_telegram(f"<b>⚠️ {Config.BOT_NAME}</b>\nError fetch data: {e}")
        return
    
    result = analyze_predator_setup(data_d1, data_h4, data_h1, live_price)
    
    state_mgr = StateManager()
    if result["signal"] in ["BUY", "SELL"]:
        can_send, reason = state_mgr.can_send(result["signal"], live_price)
        if not can_send:
            log.info(f"Blocked: {reason}")
            return
        
        now_wib = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%d %b %Y, %H:%M WIB")
        dir_emoji = "🟢" if result["signal"] == "BUY" else "🔴"
        
        msg = (
            f"{dir_emoji} <b>{Config.BOT_NAME} v{Config.VERSION}</b>\n"
            f"🔥 <b>SETUP VALID</b>\n"
            f"📅 {now_wib}\n"
            f"──────────────────────\n"
            f"<b>SINYAL: {result['signal']}</b>\n"
            f"💰 Entry: ${result['entry']:.2f}\n"
            f"🎯 TP: ${result['tp']:.2f} (+${result['tp_dist']:.2f})\n"
            f"🛑 SL: ${result['sl']:.2f} (-${result['sl_dist']:.2f})\n"
            f"📊 R:R = 1:{result['rr']:.1f}\n"
            f"──────────────────────\n"
            f"<b>🧠 Logika Predator:</b>\n"
            f"  • {result['reason']}\n"
            f"  • SL di luar jangkauan 'Wick Hunt'\n"
            f"  • Entry saat retest FVG\n"
            f"──────────────────────\n"
            f"<b>⚠️ ATURAN MUTLAK:</b>\n"
            f"• Pasang SL di ${result['sl']:.2f} (JANGAN DIGESER)\n"
            f"• Risiko maksimal 1% dari modal\n"
            f"• Jika harga belum masuk area FVG, JANGAN ENTRY\n"
            f"──────────────────────\n"
            f"<i>Eksekusi berdasarkan jejak institusi.</i>"
        )
        
        if send_telegram(msg):
            state_mgr.record(result["signal"], live_price)
            log.info(f"✅ Sinyal {result['signal']} terkirim.")
    else:
        log.info(f"No setup: {result['reason']}")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        import traceback
        log.critical(f"FATAL: {e}\n{traceback.format_exc()}")
        send_telegram(f"<b>🚨 FATAL ERROR</b>\n<code>{str(e)[:200]}</code>")
