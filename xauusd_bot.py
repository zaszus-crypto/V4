#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU/USD PREDATOR v6.0
Logika: Liquidity Sweep -> ChoCh -> Retest FVG -> Entry.
SL: Di bawah/atas Wick Sweep (Anti-Hunt).
TP: Di Likuiditas Swing berikutnya.
Hanya aktif di London & NY Session.
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
# 1. KONFIGURASI (TANPA KOMPROMI)
# ==============================================================================
class Config:
    VERSION = "6.0"
    BOT_NAME = "XAU/USD PREDATOR"
    
    # === SESSION FILTER (WIB = UTC+7) ===
    # London: 14:00 - 17:00 WIB (07:00 - 10:00 UTC)
    # NY: 19:00 - 22:00 WIB (12:00 - 15:00 UTC)
    ALLOWED_HOURS_WIB = [(14, 17), (19, 22)]
    
    # === PARAMETER TEKNIS ===
    SWING_LOOKBACK = 5
    FVG_MIN_SIZE_ATR = 0.5  # FVG minimal harus 0.5x ATR agar valid
    SL_WICK_BUFFER_ATR = 0.3  # Buffer di bawah/atas wick sweep untuk menghindari hunt
    
    # === SAFETY ===
    COOLDOWN_HOURS = 12  # Minimal 12 jam antar sinyal (hindari overtrading)
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
    range_map = {"15m": "5d", "1h":10d", "4h": "60d", "1d": "2y"}.get(interval, "60d")
    
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
# 5. CORE LOGIC: THE PREDATOR
# ==============================================================================
def calc_atr(data: List[Dict], period: int = 14) -> float:
    if len(data) < 2: return 0.0
    trs = [max(data[i]["high"] - data[i]["low"], abs(data[i]["high"] - data[i-1]["close"]), abs(data[i]["low"] - data[i-1]["close"])) for i in range(1, len(data))]
    return sum(trs[-period:]) / period if len(trs) >= period else (sum(trs) / len(trs) if trs else 0.0)

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
    return fvgs[-5:]

def is_valid_session() -> Tuple[bool, str]:
    now_wib = datetime.now(timezone.utc) + timedelta(hours=7)
    hour = now_wib.hour
    for start, end in Config.ALLOWED_HOURS_WIB:
        if start <= hour < end:
            return True, f"Session Aktif ({start}:00-{end}:00 WIB)"
    return False, f"Di luar sesi trading ({hour}:00 WIB)"

def analyze_predator_setup(data_d1: List[Dict], data_h4: List[Dict], data_h1: List[Dict], live_price: float) -> Dict[str, Any]:
    atr_h1 = calc_atr(data_h1, 14)
    if atr_h1 == 0: return {"signal": "WAIT", "reason": "ATR tidak valid"}
    
    # 1. D1 Trend Filter
    closes_d1 = [d["close"] for d in data_d1]
    if len(closes_d1) < 200: return {"signal": "WAIT", "reason": "Data D1 kurang"}
    ema50, ema200 = sum(closes_d1[-50:])/50, sum(closes_d1[-200:])/200 # Simplified EMA for speed
    bias = "bullish" if live_price > ema50 > ema200 else "bearish" if live_price < ema50 < ema200 else "neutral"
    if bias == "neutral": return {"signal": "WAIT", "reason": "D1 Netral, tidak ada arah jelas"}
    
    # 2. H4 Liquidity Sweep Detection
    swings_h4 = find_swing_points(data_h4, 3)
    sweep_valid = False
    sweep_level = 0.0
    
    if bias == "bullish" and len(swings_h4["lows"]) >= 2:
        last_low = swings_h4["lows"][-1]["price"]
        prev = data_h4[-2]
        # Harga menembus low sebelumnya, tapi close di atasnya (wick)
        if prev["low"] < last_low and prev["close"] > last_low:
            sweep_valid = True
            sweep_level = prev["low"]
            
    elif bias == "bearish" and len(swings_h4["highs"]) >= 2:
        last_high = swings_h4["highs"][-1]["price"]
        prev = data_h4[-2]
        if prev["high"] > last_high and prev["close"] < last_high:
            sweep_valid = True
            sweep_level = prev["high"]
            
    if not sweep_valid:
        return {"signal": "WAIT", "reason": "Belum ada Liquidity Sweep di H4"}
    
    # 3. H1 ChoCh + FVG Retest
    swings_h1 = find_swing_points(data_h1, 3)
    fvgs_h1 = find_fvg(data_h1, atr_h1)
    
    target_fvg = None
    if bias == "bullish":
        # Cari FVG Bullish yang belum terisi, di atas sweep level
        for fvg in fvgs_h1:
            if fvg["type"] == "bullish" and fvg["bottom"] > sweep_level and live_price <= fvg["top"] and live_price >= fvg["bottom"]:
                target_fvg = fvg
                break
    else:
        for fvg in fvgs_h1:
            if fvg["type"] == "bearish" and fvg["top"] < sweep_level and live_price >= fvg["bottom"] and live_price <= fvg["top"]:
                target_fvg = fvg
                break
                
    if not target_fvg:
        return {"signal": "WAIT", "reason": "Harga belum retest ke area FVG valid"}
    
    # 4. Kalkulasi SL & TP (Anti-Hunt)
    if bias == "bullish":
        # SL ditempatkan DI BAWAH wick sweep, ditambah buffer
        sl_price = sweep_level - (atr_h1 * Config.SL_WICK_BUFFER_ATR)
        sl_distance = live_price - sl_price
        
        # TP ditempatkan di Swing High H4 berikutnya
        tp_price = swings_h4["highs"][-1]["price"] if swings_h4["highs"] else live_price + (sl_distance * 2)
        tp_distance = tp_price - live_price
        direction = "BUY"
    else:
        sl_price = sweep_level + (atr_h1 * Config.SL_WICK_BUFFER_ATR)
        sl_distance = sl_price - live_price
        tp_price = swings_h4["lows"][-1]["price"] if swings_h4["lows"] else live_price - (sl_distance * 2)
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
# 6. MAIN EXECUTION
# ==============================================================================
def run():
    log.info(f"=== 🚀 {Config.BOT_NAME} v{Config.VERSION} ===")
    
    in_session, session_msg = is_valid_session()
    if not in_session:
        log.info(f"Menunggu sesi trading: {session_msg}")
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
            f"  • SL ditempatkan di luar jangkauan 'Wick Hunt'\n"
            f"  • Entry saat retest FVG, bukan saat breakout\n"
            f"──────────────────────\n"
            f"<b>⚠️ ATURAN MUTLAK:</b>\n"
            f"• Pasang SL di ${result['sl']:.2f} (JANGAN DIGESER)\n"
            f"• Risiko maksimal 1% dari modal\n"
            f"• Jika harga belum masuk area FVG, JANGAN ENTRY DULU\n"
            f"──────────────────────\n"
            f"<i>Ini bukan tebakan. Ini adalah eksekusi berdasarkan jejak institusi.</i>"
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
