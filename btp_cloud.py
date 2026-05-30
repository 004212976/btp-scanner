"""
btp_cloud.py — Scanner BTP lunghi (versione cloud 24/7 per GitHub Actions)
===========================================================================
Gira UNA volta a ogni esecuzione (lo scheduler di GitHub lo richiama ogni 5 min).
- Legge i prezzi in diretta (rendimentibtp.it + fallback Borsa Italiana)
- Calcola RSI / momentum / distanza dai minimi -> punteggio 0..10
- Manda l'alert su Telegram quando il punteggio supera la soglia
- Tiene MEMORIA in state.json (serie prezzi, cooldown, segnali aperti)
- Traccia da solo l'ESITO di ogni segnale (target/stop) e ti manda il
  riepilogo giornaliero con il win-rate.

Le credenziali Telegram arrivano dalle GitHub Secrets (TG_TOKEN, TG_CHAT):
NON vanno scritte qui dentro.
"""

import os, json, time, math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
import numpy as np
from bs4 import BeautifulSoup

# ───────────────────────────── CONFIG ─────────────────────────────
ROME = ZoneInfo("Europe/Rome")
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

CFG = {
    "score_alert":      7,     # soglia per mandare l'alert (0..10)
    "cooldown_min":     45,    # minuti minimi tra due alert sullo stesso BTP
    "hist_len":         40,    # quanti prezzi tenere in memoria per ogni BTP
    "rsi_period":       9,
    "tp_pct":           0.30,  # target +0.30%  (per il tracking automatico)
    "sl_pct":           0.25,  # stop  -0.25%
    "signal_timeout_h": 6,     # dopo X ore un segnale aperto si chiude "neutro"
    "summary_hour":     17,    # ora (Rome) del riepilogo giornaliero
    "summary_minute":   45,
}

# BTP lunghi monitorati
BONDS = [
    {"name": "BTP 1.70% 2051", "isin": "IT0005425233"},
    {"name": "BTP 2.15% 2051", "isin": "IT0005383309"},
    {"name": "BTP 3.85% 2049", "isin": "IT0005474330"},
    {"name": "BTP 4.00% 2035", "isin": "IT0005358806"},
    {"name": "BTP 2.80% 2067", "isin": "IT0005217390"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
}

# ─────────────────────────── STATO / MEMORIA ───────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)

def default_bond_state():
    return {"prices": [], "last_alert": None}

# ─────────────────────────── TELEGRAM ───────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] credenziali mancanti — salto invio")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=12,
        )
        if r.status_code != 200:
            print("[TG] errore", r.status_code, r.text[:200])
    except Exception as e:
        print("[TG] eccezione", e)

# ─────────────────────────── FONTI PREZZO ───────────────────────────
def _first_plausible_number(soup, lo=40, hi=160):
    for tag in soup.find_all(["span", "td", "div", "strong", "b", "p"]):
        txt = tag.get_text(strip=True).replace("\u00a0", "").replace(".", "").replace(",", ".").strip()
        # nota: rimuovo i punti delle migliaia poi uso la virgola come decimale
        try:
            val = float(txt)
        except ValueError:
            continue
        if lo < val < hi:
            return val
    return None

def src_rendimentibtp(isin):
    url = f"https://www.rendimentibtp.it/btp/{isin}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return _first_plausible_number(soup)

def src_borsaitaliana(isin):
    url = f"https://www.borsaitaliana.it/borsa/obbligazioni/mot/btp/scheda/{isin}.html?lang=it"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # cerca la riga "Prezzo ufficiale" / "Ultimo prezzo"
    for lab in ("ultimo", "prezzo ufficiale", "prezzo"):
        for th in soup.find_all(["td", "span", "th"]):
            if lab in th.get_text(strip=True).lower():
                sib = th.find_next(["td", "span", "strong"])
                if sib:
                    t = sib.get_text(strip=True).replace(".", "").replace(",", ".")
                    try:
                        v = float(t)
                        if 40 < v < 160:
                            return v
                    except ValueError:
                        pass
    return _first_plausible_number(soup)

SOURCES = [("rendimentibtp", src_rendimentibtp),
           ("borsaitaliana", src_borsaitaliana)]

def get_price(isin):
    for name, fn in SOURCES:
        try:
            p = fn(isin)
            if p:
                return round(p, 3), name
        except Exception as e:
            print(f"  [fonte {name}] errore su {isin}: {e}")
        time.sleep(0.8)
    return None, None

# ─────────────────────────── INDICATORI ───────────────────────────
def rsi(prices, period):
    if len(prices) < period + 1:
        return None
    arr = np.array(prices[-(period + 1):], dtype=float)
    d = np.diff(arr)
    up = d[d > 0].sum() / period
    dn = -d[d < 0].sum() / period
    if dn == 0:
        return 100.0
    rs = up / dn
    return round(100 - 100 / (1 + rs), 1)

def score_bond(prices):
    """Punteggio 0..10 trasparente, basato sui dati che riusciamo a leggere."""
    pts, reasons = 0, []
    p = prices[-1]
    r = rsi(prices, CFG["rsi_period"])

    if r is not None:
        if r < 35:
            pts += 3; reasons.append(f"RSI ipervenduto ({r})")
        elif r < 42:
            pts += 1; reasons.append(f"RSI basso ({r})")
        # RSI che gira verso l'alto da zona bassa
        r_prev = rsi(prices[:-1], CFG["rsi_period"])
        if r_prev is not None and r_prev < 38 and r > r_prev:
            pts += 1; reasons.append("RSI in ripresa")

    if len(prices) >= 20:
        window = prices[-20:]
        low20 = min(window)
        if p <= low20 * 1.001:
            pts += 2; reasons.append("Su minimi 20 periodi")
        mean = sum(window) / len(window)
        std = (sum((x - mean) ** 2 for x in window) / len(window)) ** 0.5
        if std > 0 and p < mean - 2 * std:
            pts += 2; reasons.append("Sotto banda Bollinger inf.")

    # micro-momentum positivo (rimbalzo che inizia)
    if len(prices) >= 3 and prices[-1] > prices[-2] <= prices[-3]:
        pts += 1; reasons.append("Inizio rimbalzo")

    return min(pts, 10), (r if r is not None else "n/d"), reasons

# ─────────────────────────── MESSAGGI ───────────────────────────
def fineco_hint(isin):
    return f"Su FinecoX cerca ISIN <code>{isin}</code> nel mercato MOT."

def alert_msg(bond, price, score, rsi_val, reasons, tp, sl):
    bars = "🟩" * score + "⬜" * (10 - score)
    return (
        f"📈 <b>SEGNALE BTP — {bond['name']}</b>\n"
        f"🕐 {datetime.now(ROME).strftime('%H:%M  %d/%m/%Y')}\n\n"
        f"💰 Prezzo: <b>{price}</b>\n"
        f"⚡ RSI({CFG['rsi_period']}): <b>{rsi_val}</b>\n"
        f"🎯 Score: <b>{score}/10</b>\n{bars}\n\n"
        f"🎯 Target: <b>{tp}</b>\n"
        f"🛑 Stop:   <b>{sl}</b>\n\n"
        f"<b>Motivi:</b>\n" + "\n".join(f"  • {x}" for x in reasons) + "\n\n"
        f"{fineco_hint(bond['isin'])}\n"
        f"⚠️ Dato indicativo: conferma sul book Fineco. Non è una consulenza."
    )

# ─────────────────────────── TRACKING ESITO ───────────────────────────
def track_open_signals(state, prices_now):
    """Controlla i segnali aperti e li chiude quando toccano target/stop o scadono."""
    open_sig = state.setdefault("open_signals", [])
    closed   = state.setdefault("closed_signals", [])
    still_open = []
    now = datetime.now(timezone.utc)

    for sig in open_sig:
        cur = prices_now.get(sig["isin"])
        opened = datetime.fromisoformat(sig["time"])
        exit_reason, result = None, None
        if cur is not None:
            if cur >= sig["tp"]:
                exit_reason, result = "target", "win"
            elif cur <= sig["sl"]:
                exit_reason, result = "stop", "loss"
        if exit_reason is None and (now - opened) > timedelta(hours=CFG["signal_timeout_h"]):
            cur = cur if cur is not None else sig["entry"]
            exit_reason = "timeout"
            result = "win" if cur >= sig["entry"] else "loss"

        if exit_reason:
            sig.update({"exit": round(cur, 3), "result": result,
                        "reason": exit_reason, "closed": now.isoformat()})
            closed.append(sig)
        else:
            still_open.append(sig)

    state["open_signals"] = still_open

def maybe_daily_summary(state):
    now = datetime.now(ROME)
    if not (now.hour == CFG["summary_hour"] and now.minute >= CFG["summary_minute"]):
        return
    today = now.strftime("%Y-%m-%d")
    if state.get("last_summary") == today:
        return
    closed = state.get("closed_signals", [])
    if not closed:
        state["last_summary"] = today
        return
    wins = sum(1 for c in closed if c["result"] == "win")
    n = len(closed)
    wr = round(wins / n * 100)
    last5 = closed[-5:]
    rows = "\n".join(
        f"  {'✅' if c['result']=='win' else '❌'} {c['name']}  "
        f"{c['entry']}→{c['exit']} ({c['reason']})" for c in reversed(last5)
    )
    tg(f"📊 <b>RIEPILOGO GIORNALIERO</b>\n{now.strftime('%d/%m/%Y')}\n\n"
       f"Segnali tracciati: <b>{n}</b>\n"
       f"Win-rate: <b>{wr}%</b>  ({wins}/{n})\n\n"
       f"<b>Ultimi segnali:</b>\n{rows}\n\n"
       f"<i>Esito automatico sui segnali del bot (target {CFG['tp_pct']}% / "
       f"stop {CFG['sl_pct']}%), non sulle tue operazioni reali.</i>")
    state["last_summary"] = today

# ─────────────────────────── ORARIO MERCATO ───────────────────────────
def market_open():
    now = datetime.now(ROME)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 8 * 60 <= t <= 17 * 60 + 30

# ─────────────────────────── MAIN ───────────────────────────
def main():
    state = load_state()

    # primo avvio assoluto -> messaggio di benvenuto
    if not state.get("started"):
        state["started"] = datetime.now(timezone.utc).isoformat()
        tg("🟢 <b>BTP Scanner cloud attivato.</b>\nRicevi alert sui BTP lunghi "
           "e un riepilogo giornaliero con il win-rate.")
        save_state(state)

    if not market_open():
        print("Mercato chiuso — esco.")
        # anche a mercato chiuso provo il riepilogo serale
        maybe_daily_summary(state)
        save_state(state)
        return

    bs = state.setdefault("bonds", {})
    prices_now, fails = {}, 0

    for bond in BONDS:
        isin = bond["isin"]
        st = bs.setdefault(isin, default_bond_state())
        price, source = get_price(isin)
        if price is None:
            print(f"  ⚠ prezzo non disponibile: {bond['name']}")
            fails += 1
            continue

        prices_now[isin] = price
        st["prices"].append(price)
        st["prices"] = st["prices"][-CFG["hist_len"]:]
        print(f"  {bond['name']}: {price}  (fonte {source})")

        score, rsi_val, reasons = score_bond(st["prices"])
        print(f"     score {score}/10  rsi {rsi_val}")

        # cooldown
        ready = True
        if st.get("last_alert"):
            last = datetime.fromisoformat(st["last_alert"])
            if datetime.now(timezone.utc) - last < timedelta(minutes=CFG["cooldown_min"]):
                ready = False

        if score >= CFG["score_alert"] and ready and reasons:
            tp = round(price * (1 + CFG["tp_pct"] / 100), 3)
            sl = round(price * (1 - CFG["sl_pct"] / 100), 3)
            tg(alert_msg(bond, price, score, rsi_val, reasons, tp, sl))
            st["last_alert"] = datetime.now(timezone.utc).isoformat()
            state.setdefault("open_signals", []).append({
                "isin": isin, "name": bond["name"], "entry": price,
                "tp": tp, "sl": sl, "score": score,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            print(f"  🔔 ALERT inviato per {bond['name']}")

    # heartbeat: se NIENTE prezzi in tutto il giro, avvisa (max 1/giorno)
    if fails == len(BONDS):
        today = datetime.now(ROME).strftime("%Y-%m-%d")
        if state.get("last_heartbeat") != today:
            tg("⚠️ <b>BTP Scanner</b>: nessun prezzo letto da nessuna fonte. "
               "Probabile cambio struttura del sito — va aggiornato il parser.")
            state["last_heartbeat"] = today

    track_open_signals(state, prices_now)
    maybe_daily_summary(state)
    save_state(state)
    print("Fatto.")

if __name__ == "__main__":
    main()
