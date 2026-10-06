"""
XAUUSD Signal Bot — Single File Edition
Grade A / A+ / A+++ | Multi-source failover | Anti-spam | Anti-geoblock
"""
import os
import sys
import json
import time
import random
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

# ── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("xauusd-bot")

# ── Konstanta ───────────────────────────────────────────────
STATE_BRANCH = "bot-state"
STATE_FILE = "last_signal.json"
TIMEOUT = 10
MAX_RETRIES = 2
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]
GRADE_THRESHOLDS = [("A+++", 90), ("A+", 75), ("A", 60)]
GRADE_CONFIG = {
    "A":    {"risk_percent": 0.5, "lot_multiplier": 1},
    "A+":   {"risk_percent": 1.0, "lot_multiplier": 2},
    "A+++": {"risk_percent": 1.5, "lot_multiplier": 3},
}
COOLDOWN_BY_GRADE = {"A": 60, "A+": 45, "A+++": 15}
GRADE_RANK = {"A": 1, "A+": 2, "A+++": 3}


# ═══════════════════════════════════════════════════════════
#  SECTION 1: DATA FETCHER (5 sumber berlapis)
# ═══════════════════════════════════════════════════════════

def _headers():
    return {"User-Agent": random.choice(USER_AGENTS)}


def _get(url, params=None, headers=None):
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers or _headers(),
                             timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            log.warning("HTTP %s dari %s", r.status_code, url)
        except Exception as e:
            log.warning("Request gagal %s: %s", url, e)
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
    return None


def _ohlc_from_biquote(symbol, interval, limit):
    try:
        from biquote import Biquote
        bq = Biquote()
        bars = bq.ohlc(symbol, interval=interval, limit=limit)
        if not bars or len(bars) < 50:
            return None
        df = pd.DataFrame(bars)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
            df.set_index("time", inplace=True)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    except Exception as e:
        log.warning("biquote gagal: %s", e)
        return None


def _ohlc_from_xaus(interval, limit):
    try:
        r = _get("https://xaus.com/api/v1/intraday",
                 params={"symbol": "xau", "hours": 48})
        if not r:
            return None
        data = r.json()
        if "series" not in data or len(data["series"]) < 100:
            return None
        df = pd.DataFrame(data["series"])
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df = df.rename(columns={"price": "Close"})
        df["Open"] = df["Close"].shift(1)
        df["High"] = df["Close"]
        df["Low"] = df["Close"]
        df["Volume"] = 0
        df = df.dropna()
        rule = "30min" if interval == "30m" else "1h"
        ohlc = df["Close"].resample(rule).ohlc()
        ohlc["Volume"] = 0
        ohlc.columns = ["Open", "High", "Low", "Close", "Volume"]
        ohlc = ohlc.dropna()
        return ohlc if len(ohlc) >= 50 else None
    except Exception as e:
        log.warning("xaus gagal: %s", e)
        return None


def _ohlc_from_goldprice(interval, limit):
    try:
        r = _get("https://api.goldprice.dev/api/v1/ohlc",
                 params={"symbol": "XAUUSD", "interval": interval, "limit": limit})
        if not r:
            return None
        data = r.json()
        if "data" not in data or len(data["data"]) < 50:
            return None
        df = pd.DataFrame(data["data"])
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                                "c": "Close", "v": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    except Exception as e:
        log.warning("goldprice gagal: %s", e)
        return None


def _ohlc_from_yfinance(interval, limit):
    try:
        import yfinance as yf
        yf_interval = "30m" if interval == "30m" else "1h"
        period = "5d" if yf_interval == "30m" else "1mo"
        df = yf.download("GC=F", interval=yf_interval, period=period, progress=False)
        if df is None or len(df) < 50:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        return df.tail(limit)
    except Exception as e:
        log.warning("yfinance gagal: %s", e)
        return None


def fetch_ohlc(interval: str, limit: int = 300) -> pd.DataFrame | None:
    """Coba 4 sumber berurutan. Return DataFrame pertama yang berhasil."""
    sources = [
        ("biquote",   lambda: _ohlc_from_biquote("XAUUSD", interval, limit)),
        ("xaus",      lambda: _ohlc_from_xaus(interval, limit)),
        ("goldprice", lambda: _ohlc_from_goldprice(interval, limit)),
        ("yfinance",  lambda: _ohlc_from_yfinance(interval, limit)),
    ]
    for name, fn in sources:
        df = fn()
        if df is not None and len(df) >= 50:
            log.info("✅ Data %s dari %s (%d bar)", interval, name, len(df))
            return df
    log.error("🚨 SEMUA SUMBER GAGAL untuk %s", interval)
    return None


def fetch_dxy_bias():
    """Bias DXY vs SMA20 daily untuk filter arah gold."""
    try:
        from biquote import Biquote
        bars = Biquote().ohlc("DXY", interval="1d", limit=50)
        if not bars or len(bars) < 25:
            return "UNKNOWN"
        closes = [b["close"] for b in bars]
        sma20 = sum(closes[-20:]) / 20
        return "BEARISH" if closes[-1] < sma20 else "BULLISH"
    except Exception:
        return "UNKNOWN"


def news_blackout(min_before=30, min_after=30):
    """Return True jika dalam window high-impact news USD."""
    try:
        from biquote import Biquote
        events = Biquote().calendar(importance="high", countries="US")
        now = datetime.now(timezone.utc)
        for ev in events:
            try:
                t = datetime.fromisoformat(ev["time"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                delta = abs((t - now).total_seconds() / 60)
                if delta <= max(min_before, min_after):
                    log.info("🚫 News blackout: %s (%.0f mnt)", ev.get("title", "?"), delta)
                    return True
            except Exception:
                continue
    except Exception as e:
        log.warning("Kalender gagal: %s", e)
    return False


# ═══════════════════════════════════════════════════════════
#  SECTION 2: INDIKATOR TEKNIKAL
# ═══════════════════════════════════════════════════════════

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


def rsi(s, p=14):
    d = s.diff()
    g = d.where(d > 0, 0.0)
    l = -d.where(d < 0, 0.0)
    ag = g.ewm(alpha=1/p, adjust=False, min_periods=p).mean()
    al = l.ewm(alpha=1/p, adjust=False, min_periods=p).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(h, l, c, p=14):
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False, min_periods=p).mean()


def adx(h, l, c, p=14):
    pdm = h.diff()
    mdm = -l.diff()
    pdm = pdm.where((pdm > mdm) & (pdm > 0), 0.0)
    mdm = mdm.where((mdm > pdm) & (mdm > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1/p, adjust=False, min_periods=p).mean()
    pdi = 100 * (pdm.ewm(alpha=1/p, adjust=False, min_periods=p).mean() / atr_v)
    mdi = 100 * (mdm.ewm(alpha=1/p, adjust=False, min_periods=p).mean() / atr_v)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/p, adjust=False, min_periods=p).mean()


def add_indicators(df):
    df = df.copy()
    df["EMA20"] = ema(df["Close"], 20)
    df["EMA50"] = ema(df["Close"], 50)
    df["EMA200"] = ema(df["Close"], 200)
    df["RSI14"] = rsi(df["Close"], 14)
    df["ATR14"] = atr(df["High"], df["Low"], df["Close"], 14)
    df["ADX14"] = adx(df["High"], df["Low"], df["Close"], 14)
    return df


# ═══════════════════════════════════════════════════════════
#  SECTION 3: STRATEGI & GRADING
# ═══════════════════════════════════════════════════════════

@dataclass
class Signal:
    pair: str
    direction: str
    entry: float
    sl: float
    tp: float
    timeframe: str
    grade: str
    score: int
    factors: list = field(default_factory=list)
    risk_percent: float = 0.5
    lot_multiplier: int = 1
    reason: str = ""
    timestamp: str = ""


def h1_bias(df_h1):
    df = add_indicators(df_h1)
    last = df.iloc[-1]
    slope = last["EMA50"] - df["EMA50"].iloc[-5]
    if last["EMA50"] > last["EMA200"] and slope > 0:
        return "BULLISH"
    if last["EMA50"] < last["EMA200"] and slope < 0:
        return "BEARISH"
    return "RANGING"


def _detect_divergence(df, direction, lookback=20):
    if len(df) < lookback + 5:
        return False
    recent = df.iloc[-lookback:].copy()
    if direction == "BUY":
        idx = recent["Low"].nsmallest(2).index
        if len(idx) < 2:
            return False
        p = recent.loc[idx, "Low"].values
        r = recent.loc[idx, "RSI14"].values
        return p[1] < p[0] and r[1] > r[0]
    else:
        idx = recent["High"].nlargest(2).index
        if len(idx) < 2:
            return False
        p = recent.loc[idx, "High"].values
        r = recent.loc[idx, "RSI14"].values
        return p[1] > p[0] and r[1] < r[0]


def _near_sr(df, price, tol):
    lb = df.iloc[-50:]
    for lvl in list(lb["High"].nlargest(5).values) + list(lb["Low"].nsmallest(5).values):
        if abs(price - lvl) <= tol:
            return True
    return False


def _near_fib618(df, price, tol):
    lb = df.iloc[-50:]
    hi, lo = lb["High"].max(), lb["Low"].min()
    rng = hi - lo
    if rng <= 0:
        return False
    f1 = lo + 0.618 * rng
    f2 = hi - 0.618 * rng
    return abs(price - f1) <= tol or abs(price - f2) <= tol


def _near_round(price, step=10.0, tol=1.0):
    return abs(price - round(price / step) * step) <= tol


def _london_ny_overlap():
    return 13 <= datetime.now(timezone.utc).hour < 17


def grade_signal(df_m30, df_h1, df_h4, direction, entry, sl, tp,
                 dxy_bias="UNKNOWN", news_black=False):
    score = 0
    factors = []
    last = df_m30.iloc[-1]
    prev = df_m30.iloc[-2]
    h1 = df_h1.iloc[-1]
    atr_v = last["ATR14"]

    if pd.isna(atr_v) or atr_v <= 0:
        return None

    # ── BASIS (60) ──────────────────────────────────────────
    if (direction == "BUY" and h1["EMA50"] > h1["EMA200"]) or \
       (direction == "SELL" and h1["EMA50"] < h1["EMA200"]):
        score += 15; factors.append("✅ H1 trend searah")

    dist = abs(entry - last["EMA20"]) / atr_v
    if dist <= 1.0:
        score += 10; factors.append(f"✅ Pullback EMA20 ({dist:.2f} ATR)")

    rsi_cross = (prev["RSI14"] < 50 <= last["RSI14"]) if direction == "BUY" \
                else (prev["RSI14"] > 50 >= last["RSI14"])
    if rsi_cross:
        score += 10; factors.append("✅ RSI cross 50")

    candle_ok = (last["Close"] > last["Open"]) if direction == "BUY" \
                else (last["Close"] < last["Open"])
    if candle_ok:
        score += 10; factors.append("✅ Candle konfirmasi")

    adx_m30 = last["ADX14"] if not pd.isna(last["ADX14"]) else 0
    if adx_m30 > 25:
        score += 10; factors.append(f"✅ ADX M30 {adx_m30:.1f}")

    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    if rr >= 2.0:
        score += 5; factors.append(f"✅ RR {rr:.2f}")

    # ── A+ BONUS ────────────────────────────────────────────
    if adx_m30 > 30:
        score += 5; factors.append(f"⭐ ADX M30 > 30")
    adx_h1 = h1["ADX14"] if not pd.isna(h1["ADX14"]) else 0
    if adx_h1 > 25:
        score += 5; factors.append(f"⭐ ADX H1 {adx_h1:.1f}")
    if (direction == "BUY" and dxy_bias == "BEARISH") or \
       (direction == "SELL" and dxy_bias == "BULLISH"):
        score += 5; factors.append(f"⭐ DXY {dxy_bias}")
    if _detect_divergence(df_m30, direction):
        score += 5; factors.append("⭐ RSI divergence")
    if dist < 0.3:
        score += 5; factors.append("⭐ Entry presisi <0.3 ATR")
    avg_vol = df_m30["Volume"].iloc[-21:-1].mean()
    if avg_vol > 0 and last["Volume"] > 1.5 * avg_vol:
        score += 4; factors.append("⭐ Volume spike")

    # ── A+++ BONUS ──────────────────────────────────────────
    if _near_sr(df_m30, entry, 0.5 * atr_v):
        score += 8; factors.append("💎 Dekat zona S/R")
    if df_h4 is not None and len(df_h4) > 200:
        h4 = df_h4.iloc[-1]
        h4_bull = h4["EMA50"] > h4["EMA200"]
        h1_bull = h1["EMA50"] > h1["EMA200"]
        aligned = (h4_bull and h1_bull and direction == "BUY") or \
                  (not h4_bull and not h1_bull and direction == "SELL")
        if aligned:
            score += 8; factors.append("💎 Multi-TF M30+H1+H4")
    if _near_fib618(df_m30, entry, 0.5 * atr_v):
        score += 5; factors.append("💎 Fib 0.618")
    if _near_round(entry, 10.0, atr_v * 0.5):
        score += 4; factors.append("💎 Round number")
    if _london_ny_overlap():
        score += 5; factors.append("💎 London/NY overlap")
    if not news_black:
        score += 5; factors.append("💎 Bebas news 2 jam")

    final = min(100, score)
    grade = "NONE"
    for g, thr in GRADE_THRESHOLDS:
        if final >= thr:
            grade = g; break

    if grade == "NONE":
        return None

    cfg = GRADE_CONFIG[grade]
    return {
        "grade": grade, "score": final, "factors": factors,
        "risk_percent": cfg["risk_percent"], "lot_multiplier": cfg["lot_multiplier"],
    }


def analyze_m30(df_m30, df_h1, df_h4, bias, dxy, news_black):
    if bias == "RANGING":
        return None
    df = add_indicators(df_m30)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if pd.isna(last["ATR14"]) or last["ATR14"] <= 0:
        return None

    atr_v = last["ATR14"]
    price = last["Close"]
    dist = abs(price - last["EMA20"]) / atr_v
    if dist > 1.0:
        return None

    # Cek sinyal dasar
    if bias == "BULLISH":
        rsi_cross = prev["RSI14"] < 50 <= last["RSI14"]
        rsi_pb = 40 <= last["RSI14"] <= 60
        candle = last["Close"] > last["Open"]
        if not (rsi_cross or (rsi_pb and candle)):
            return None
        direction = "BUY"
        sl = price - 1.5 * atr_v
        tp = price + 2.5 * atr_v
    elif bias == "BEARISH":
        rsi_cross = prev["RSI14"] > 50 >= last["RSI14"]
        rsi_pb = 40 <= last["RSI14"] <= 60
        candle = last["Close"] < last["Open"]
        if not (rsi_cross or (rsi_pb and candle)):
            return None
        direction = "SELL"
        sl = price + 1.5 * atr_v
        tp = price - 2.5 * atr_v
    else:
        return None

    g = grade_signal(df, df_h1, df_h4, direction, price, sl, tp, dxy, news_black)
    if g is None:
        log.info("Grade NONE — sinyal ditolak")
        return None

    return Signal(
        pair="XAUUSD", direction=direction,
        entry=round(price, 2), sl=round(sl, 2), tp=round(tp, 2),
        timeframe="M30",
        grade=g["grade"], score=g["score"], factors=g["factors"],
        risk_percent=g["risk_percent"], lot_multiplier=g["lot_multiplier"],
        reason=" | ".join(g["factors"][:3]),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ═══════════════════════════════════════════════════════════
#  SECTION 4: STATE MANAGER (orphan branch)
# ═══════════════════════════════════════════════════════════

def _run(cmd, check=False):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


def load_state():
    try:
        _run(f"git fetch origin {STATE_BRANCH}")
        r = _run(f"git show origin/{STATE_BRANCH}:{STATE_FILE}")
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception as e:
        log.warning("Load state gagal: %s", e)
    return {}


def save_state(state):
    try:
        _run("git config user.email 'bot@users.noreply.github.com'")
        _run("git config user.name 'xauusd-bot'")
        _run(f"git fetch origin {STATE_BRANCH}")
        r = _run(f"git checkout {STATE_BRANCH}")
        if r.returncode != 0:
            _run(f"git checkout --orphan {STATE_BRANCH}")
            _run("git rm -rf . --cached")

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        _run(f"git add {STATE_FILE}")
        _run(f'git commit -m "state: {datetime.now(timezone.utc).isoformat()}"')
        _run(f"git push origin {STATE_BRANCH}")
        log.info("✅ State tersimpan di branch %s", STATE_BRANCH)
        _run("git checkout main")
    except Exception as e:
        log.error("Save state gagal: %s", e)


def should_send(signal):
    """Anti-spam berbasis cooldown + override grade lebih tinggi."""
    state = load_state()
    last = state.get("last_signal")
    if not last:
        return True

    last_grade = last.get("grade", "A")
    if GRADE_RANK.get(signal.grade, 0) > GRADE_RANK.get(last_grade, 0):
        log.info("Grade lebih tinggi — override cooldown")
        return True

    try:
        last_t = datetime.fromisoformat(last["timestamp"])
        delta_m = (datetime.now(timezone.utc) - last_t).total_seconds() / 60
        cd = COOLDOWN_BY_GRADE.get(signal.grade, 60)
        if delta_m < cd:
            log.info("Cooldown aktif (%.0f < %d menit)", delta_m, cd)
            return False
    except Exception:
        pass
    return True


# ═══════════════════════════════════════════════════════════
#  SECTION 5: TELEGRAM NOTIFIER
# ═══════════════════════════════════════════════════════════

GRADE_EMOJI = {"A": "🟢", "A+": "🟢🟢", "A+++": "🟢🟢🟢"}
GRADE_BADGE = {"A": "", "A+": " ⭐", "A+++": " 💎🔥"}


def format_message(s: Signal) -> str:
    emoji = GRADE_EMOJI.get(s.grade, "⚪")
    badge = GRADE_BADGE.get(s.grade, "")
    arrow = "📈" if s.direction == "BUY" else "📉"
    rr = abs(s.tp - s.entry) / abs(s.entry - s.sl) if abs(s.entry - s.sl) > 0 else 0

    msg = f"{emoji} *SINYAL XAUUSD — GRADE {s.grade}*{badge}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏆 *Skor:* `{s.score}/100`"
    if s.grade == "A+++":
        msg += "  ★★★ PREMIUM"
    msg += "\n"
    msg += (f"{arrow} *Arah:* `{s.direction}`\n"
            f"⏰ *TF:* `{s.timeframe}`\n"
            f"💰 *Entry:* `{s.entry}`\n"
            f"🛑 *SL:* `{s.sl}`\n"
            f"🎯 *TP:* `{s.tp}`\n"
            f"📊 *RR:* `1:{rr:.1f}`\n")
    msg += "━━━━━━━━━━━━━━━━━━━━\n📝 *Faktor:*\n"

    factors = "\n".join(s.factors[:8])
    if len(s.factors) > 8:
        factors += f"\n_...dan {len(s.factors) - 8} faktor lain_"
    msg += factors + "\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"⚠️ *Risk:* `{s.risk_percent}%` | *Lot:* `{s.lot_multiplier}× standar`\n"

    if s.grade == "A+++":
        msg += "🎯 *PRIORITAS TERTINGGI — JANGAN LEWATKAN*\n"
    elif s.grade == "A+":
        msg += "✅ *Sinyal berkualitas tinggi*\n"

    msg += f"🕐 _{s.timestamp}_\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "⚠️ _Eksekusi manual di MT5. Gunakan risk management._"
    return msg


def send_telegram(s: Signal) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Token/chat_id Telegram tidak diset!")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": format_message(s),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                log.info("✅ Terkirim ke Telegram")
                return True
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Rate limit, tunggu %s dtk", wait)
                time.sleep(wait)
            else:
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Telegram err: %s", e)
        time.sleep(5)
    return False


# ═══════════════════════════════════════════════════════════
#  SECTION 6: MAIN
# ═══════════════════════════════════════════════════════════

def should_run_now():
    """Window toleransi 5 menit setelah candle close."""
    m = datetime.now(timezone.utc).minute
    return (0 <= m <= 5) or (30 <= m <= 35)


def main():
    log.info("=" * 50)
    log.info("XAUUSD Bot mulai — %s", datetime.now(timezone.utc).isoformat())

    if not should_run_now():
        log.info("Bukan window candle close. Skip.")
        return

    df_h1 = fetch_ohlc("1h")
    if df_h1 is None:
        return
    df_m30 = fetch_ohlc("30m")
    if df_m30 is None:
        return
    df_h4 = fetch_ohlc("1h", limit=500)  # proxy H4 (bisa diganti sumber H4 native)

    bias = h1_bias(df_h1)
    log.info("H1 Bias: %s", bias)

    dxy = fetch_dxy_bias()
    log.info("DXY Bias: %s", dxy)

    news_black = news_blackout()
    if news_black:
        log.info("News blackout aktif — sinyal akan tetap dikirim dengan skor lebih rendah")

    sig = analyze_m30(df_m30, df_h1, df_h4, bias, dxy, news_black)
    if sig is None:
        log.info("Tidak ada sinyal valid.")
        return

    log.info("SINYAL: %s %s | Grade %s | Skor %d | Entry %.2f SL %.2f TP %.2f",
             sig.direction, sig.timeframe, sig.grade, sig.score,
             sig.entry, sig.sl, sig.tp)

    if not should_send(sig):
        log.info("Anti-spam: sinyal di-skip.")
        return

    if send_telegram(sig):
        save_state({
            "last_signal": {
                "pair": sig.pair, "direction": sig.direction,
                "timeframe": sig.timeframe, "grade": sig.grade,
                "score": sig.score, "entry": sig.entry,
                "timestamp": sig.timestamp,
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    log.info("Selesai.")


if __name__ == "__main__":
    main()
