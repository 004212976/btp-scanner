"""
btp_cloud.py — Scanner BTP lunghi (CLOUD) — "timing + piano operativo"
======================================================================
Gira UNA volta a esecuzione (GitHub Actions lo richiama ogni 5 min).

Cosa fa l'alert ora — un PIANO COMPLETO pronto per Fineco:
  • PREZZO D'INGRESSO (e un 2° livello d'ingresso più basso, se vuoi mediare)
  • STOP unico di protezione
  • 3 LIVELLI DI VENDITA (TP1/TP2/TP3) per la protezione multilivello Fineco
    con suggerimento di scarico a tranche (1/3 + 1/3 + 1/3)
  Tutti i livelli sono ADATTIVI alla volatilità del momento.

Timing d'ingresso: l'alert parte SOLO su conferma di inversione
(rientro Bollinger / incrocio Stocastico / barra di inversione / divergenza),
con filtro "anti-coltello" e voto qualità A/B/C.

Filtro SPREAD BTP-Bund: il bot legge lo spread Italia-Germania 10Y e lo usa
come contesto (favorisce gli acquisti quando lo spread si restringe, mette in
guardia quando si allarga). Se la fonte non risponde, viene semplicemente
ignorato e il bot continua a funzionare.

Profondità/book: NON è disponibile in automatico (dato a pagamento / non
raschiabile). Ogni alert ti ricorda di controllare il book su Fineco a mano
prima di entrare.

Credenziali da GitHub Secrets: TG_TOKEN, TG_CHAT.
"""

import os, json, time, re
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
    "score_alert":   6,
    "cooldown_min":  45,
    "hist_len":      140,
    "rsi_fast":      9, "rsi_slow": 14,
    "stoch_period":  14,
    "boll_period":   20, "boll_k": 2.0,
    "trend_period":  60,
    "vol_window":    12,
    # livelli adattivi (in % e in multipli di volatilità)
    "entry2_min_pct": 0.12, "entry2_vol_mult": 0.5,   # 2° ingresso più basso
    "sl_min_pct":     0.20, "sl_vol_mult":    1.0,     # stop
    "tp1_min_pct":    0.25, "tp1_vol_mult":   1.0,
    "tp2_min_pct":    0.45, "tp2_vol_mult":   1.8,
    "tp3_min_pct":    0.70, "tp3_vol_mult":   2.8,
    "signal_timeout_h": 6,
    "summary_hour":  17, "summary_minute": 45,
    "open_skip_min": 15,
    "spread_hist_len": 30,
    # ---- COSTI / FILTRO ECONOMICO (Fineco) ----
    "pos_size_eur":  25000,   # ⬅️ METTI QUI il TUO controvalore tipico per operazione
    "comm_rate":     0.0019,  # 0,19%
    "comm_min":      2.95,    # commissione minima per eseguito
    "comm_max":      19.0,    # commissione massima (cap) per eseguito
    "min_net_eur":   10.0,    # guadagno netto minimo a TP1 per dare il segnale
}

BONDS = [
    {"name": "BTP 1.70% 2051", "isin": "IT0005425233"},
    {"name": "BTP 2.15% 2051", "isin": "IT0005383309"},
    {"name": "BTP 3.85% 2049", "isin": "IT0005474330"},
    {"name": "BTP 4.00% 2035", "isin": "IT0005358806"},
    {"name": "BTP 2.80% 2067", "isin": "IT0005217390"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "it-IT,it;q=0.9,en;q=0.6"}

# ─────────────────────────── STATO ───────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
def default_bond_state():
    return {"prices": [], "last_alert": None, "prev_k": None, "prev_d": None}

# ─────────────────────────── TELEGRAM ───────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("[TG] credenziali mancanti — salto invio"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=12)
        if r.status_code != 200: print("[TG] errore", r.status_code, r.text[:200])
    except Exception as e: print("[TG] eccezione", e)

# ─────────────────────────── FONTI PREZZO ───────────────────────────
def _first_plausible_number(soup, lo=40, hi=160):
    for tag in soup.find_all(["span","td","div","strong","b","p"]):
        txt = tag.get_text(strip=True).replace("\u00a0","").replace(".","").replace(",",".").strip()
        try: v = float(txt)
        except ValueError: continue
        if lo < v < hi: return v
    return None
def src_rendimentibtp(isin):
    r = requests.get(f"https://www.rendimentibtp.it/btp/{isin}", headers=HEADERS, timeout=15)
    r.raise_for_status(); return _first_plausible_number(BeautifulSoup(r.text,"html.parser"))
def src_borsaitaliana(isin):
    r = requests.get(f"https://www.borsaitaliana.it/borsa/obbligazioni/mot/btp/scheda/{isin}.html?lang=it",
                     headers=HEADERS, timeout=15); r.raise_for_status()
    soup = BeautifulSoup(r.text,"html.parser")
    for lab in ("ultimo","prezzo ufficiale","prezzo"):
        for th in soup.find_all(["td","span","th"]):
            if lab in th.get_text(strip=True).lower():
                sib = th.find_next(["td","span","strong"])
                if sib:
                    t = sib.get_text(strip=True).replace(".","").replace(",",".")
                    try:
                        v = float(t)
                        if 40 < v < 160: return v
                    except ValueError: pass
    return _first_plausible_number(soup)
SOURCES = [("rendimentibtp", src_rendimentibtp), ("borsaitaliana", src_borsaitaliana)]
def get_price(isin):
    for name, fn in SOURCES:
        try:
            p = fn(isin)
            if p: return round(p,3), name
        except Exception as e: print(f"  [fonte {name}] errore {isin}: {e}")
        time.sleep(0.8)
    return None, None

# ─────────────────────────── SPREAD BTP-BUND ───────────────────────────
def get_spread_bp():
    """Spread Italia-Germania 10Y in punti base. Best-effort, con fallback None."""
    try:
        url = "https://www.worldgovernmentbonds.com/spread/italy-10-years-vs-germany-10-years/"
        r = requests.get(url, headers=HEADERS, timeout=15); r.raise_for_status()
        txt = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        # cerca un valore plausibile di spread espresso in bp
        for m in re.finditer(r"(\d{2,3}(?:\.\d+)?)\s*bp", txt):
            v = float(m.group(1))
            if 20 <= v <= 400: return round(v, 1)
    except Exception as e:
        print("  [spread] errore:", e)
    return None

def spread_context(state, spread_bp):
    """Aggiorna lo storico e restituisce (testo, modificatore_score)."""
    if spread_bp is None:
        return "n/d", 0
    hist = state.setdefault("spread_hist", [])
    hist.append(spread_bp); state["spread_hist"] = hist[-CFG["spread_hist_len"]:]
    if len(hist) < 4:
        return f"{spread_bp} bp", 0
    ref = float(np.mean(hist[-4:-1])); delta = spread_bp - ref
    if delta <= -1:   return f"{spread_bp} bp (in restringimento ✓)", +1
    if delta >= 2:    return f"{spread_bp} bp (in allargamento ⚠️)", -2
    return f"{spread_bp} bp (stabile)", 0

# ─────────────────────────── INDICATORI ───────────────────────────
def rsi(prices, period):
    if len(prices) < period+1: return None
    arr = np.array(prices[-(period+1):], float); d = np.diff(arr)
    up = d[d>0].sum()/period; dn = -d[d<0].sum()/period
    if dn == 0: return 100.0
    return round(100 - 100/(1+up/dn), 1)
def stochastic(prices, period):
    if len(prices) < period+3: return None, None
    def k_at(end):
        w = prices[end-period:end]; hi, lo = max(w), min(w)
        return 50.0 if hi==lo else (prices[end-1]-lo)/(hi-lo)*100
    k_now = k_at(len(prices)); k3 = [k_at(len(prices)-i) for i in (2,1,0)]
    return round(k_now,1), round(sum(k3)/3,1)
def bollinger(prices, period, k):
    if len(prices) < period: return None
    w = np.array(prices[-period:], float); m=w.mean(); s=w.std()
    return m, m-k*s, m+k*s
def sma(prices, period):
    return None if len(prices)<period else float(np.mean(prices[-period:]))
def vol_pct(prices, window, price):
    if len(prices) < window or not price: return None
    return float(np.std(prices[-window:]))/price*100

# ─────────────────────────── TIMING ───────────────────────────
def evaluate(prices, st):
    if len(prices) < 22: return None
    p, prev = prices[-1], prices[-2]
    r9 = rsi(prices, CFG["rsi_fast"]); r14 = rsi(prices, CFG["rsi_slow"])
    k, d = stochastic(prices, CFG["stoch_period"])
    boll = bollinger(prices, CFG["boll_period"], CFG["boll_k"])
    sma_t = sma(prices, min(CFG["trend_period"], len(prices)))
    low20 = min(prices[-20:]); new_low = p <= low20*1.0005
    pts, triggers, confirms = 0, [], 0

    if boll:
        _, lower, _ = boll
        if prev < lower and p >= lower:
            pts += 3; confirms += 1; triggers.append("Rientro da banda Bollinger")
    if k is not None and d is not None and st.get("prev_k") is not None:
        pk, pd = st["prev_k"], st["prev_d"]
        if pk is not None and pd is not None and pk < pd and k > d and min(pk,k) < 25:
            pts += 2; confirms += 1; triggers.append("Stocastico incrocia in su")
    if len(prices) >= 3 and prev <= prices[-3] and p > prev and prev <= low20*1.001:
        pts += 2; confirms += 1; triggers.append("Inversione dal minimo")
    if len(prices) >= 14 and r9 is not None:
        win = prices[-12:]; i_min = int(np.argmin(win))
        if i_min < 6:
            rsi_old = rsi(prices[:-12+i_min+1], CFG["rsi_fast"]) if len(prices) > 12 else None
            if p < win[i_min]*1.001 and rsi_old is not None and r9 > rsi_old + 3:
                pts += 3; confirms += 1; triggers.append("Divergenza rialzista RSI")

    if r9 is not None:
        if r9 < 28: pts += 2
        elif r9 < 35: pts += 1
    if p <= low20*1.0015: pts += 1
    if r14 is not None and r9 is not None and r9 > r14: pts += 1

    falling_knife = False
    if sma_t and p < sma_t*0.995 and new_low and confirms == 0:
        pts -= 3; falling_knife = True

    score = max(0, min(pts, 10))
    if confirms >= 2 and (r9 is not None and r9 < 35): grade = "A"
    elif confirms >= 1 and (r9 is not None and r9 < 38): grade = "B"
    else: grade = "C"
    info = {"price": p, "rsi9": r9, "k": k, "confirms": confirms,
            "falling_knife": falling_knife, "vol": vol_pct(prices, CFG["vol_window"], p)}
    st["prev_k"], st["prev_d"] = k, d
    return score, grade, triggers, info

# ─────────────────────────── PIANO OPERATIVO (livelli) ───────────────────────────
def trade_plan(price, info):
    v = info.get("vol") or 0
    def up(minp, mult): return round(price*(1+max(minp, v*mult)/100), 3)
    def dn(minp, mult): return round(price*(1-max(minp, v*mult)/100), 3)
    return {
        "entry":  price,
        "entry2": dn(CFG["entry2_min_pct"], CFG["entry2_vol_mult"]),
        "sl":     dn(CFG["sl_min_pct"],  CFG["sl_vol_mult"]),
        "tp1":    up(CFG["tp1_min_pct"], CFG["tp1_vol_mult"]),
        "tp2":    up(CFG["tp2_min_pct"], CFG["tp2_vol_mult"]),
        "tp3":    up(CFG["tp3_min_pct"], CFG["tp3_vol_mult"]),
    }

# ─────────────────────────── COSTI / ECONOMIA DEL TRADE ───────────────────────────
def commission(controvalore):
    """Commissione Fineco per eseguito: 0,19% con minimo 2,95 e cap 19 €."""
    return min(CFG["comm_max"], max(CFG["comm_min"], controvalore * CFG["comm_rate"]))

def economics(plan):
    """Conti del trade al controvalore di riferimento (andata+ritorno)."""
    size = CFG["pos_size_eur"]
    tp1_pct = (plan["tp1"]/plan["entry"] - 1) * 100          # distanza % fino a TP1
    cost_rt = commission(size) * 2                            # commissioni A/R
    gross   = size * tp1_pct/100                              # guadagno lordo a TP1
    net     = gross - cost_rt
    # size minima per pareggiare i costi (assumendo cap 19 -> 38 A/R)
    breakeven = round((CFG["comm_max"]*2) / (tp1_pct/100)) if tp1_pct > 0 else None
    return {"size": size, "tp1_pct": round(tp1_pct, 2), "cost_rt": round(cost_rt, 2),
            "gross": round(gross, 2), "net": round(net, 2), "breakeven": breakeven}

# ─────────────────────────── MESSAGGIO ───────────────────────────
def alert_msg(bond, score, grade, triggers, info, plan, spread_txt, eco):
    bars = "🟩"*score + "⬜"*(10-score)
    star = {"A":"⭐⭐⭐ ottimo","B":"⭐⭐ buono","C":"⭐ debole"}[grade]
    return (
        f"📈 <b>{bond['name']}</b> — ingresso {grade} {star}\n"
        f"🕐 {datetime.now(ROME).strftime('%H:%M  %d/%m/%Y')}\n"
        f"⚡ RSI {info['rsi9']} · Stoc {info['k']} · Score {score}/10\n{bars}\n"
        f"📊 Spread BTP-Bund: {spread_txt}\n\n"
        f"━━━ <b>PIANO OPERATIVO</b> ━━━\n"
        f"🟢 <b>INGRESSO:</b> {plan['entry']}\n"
        f"     (2° livello per mediare: {plan['entry2']})\n"
        f"🛑 <b>STOP:</b> {plan['sl']}\n\n"
        f"🎯 <b>VENDITE</b> (protezione multilivello Fineco):\n"
        f"   TP1 {plan['tp1']}  → vendi 1/3\n"
        f"   TP2 {plan['tp2']}  → vendi 1/3\n"
        f"   TP3 {plan['tp3']}  → vendi ultimo 1/3\n\n"
        f"💶 <b>Conti</b> (su €{eco['size']:,}):\n"
        f"   lordo a TP1 ≈ €{eco['gross']}  −  costi A/R €{eco['cost_rt']}  =  <b>netto ≈ €{eco['net']}</b>\n"
        f"   (per pareggiare i costi: almeno €{eco['breakeven']:,} di nominale)\n\n"
        f"<b>Conferme:</b> " + ", ".join(triggers) + "\n\n"
        f"👉 Prima di entrare, controlla il <b>book su Fineco</b> "
        f"(ISIN <code>{bond['isin']}</code>, MOT): verifica un bid che regge / "
        f"l'ask in assorbimento.\n"
        f"⚠️ Dati indicativi. Non è consulenza."
    )

# ─────────────────────────── TRACKING ESITO ───────────────────────────
def track_open_signals(state, prices_now):
    open_sig = state.setdefault("open_signals", []); closed = state.setdefault("closed_signals", [])
    still, now = [], datetime.now(timezone.utc)
    for sig in open_sig:
        cur = prices_now.get(sig["isin"]); opened = datetime.fromisoformat(sig["time"])
        reason = result = None
        if cur is not None:
            if cur >= sig["tp1"]: reason, result = "TP1", "win"
            elif cur <= sig["sl"]: reason, result = "stop", "loss"
        if reason is None and (now-opened) > timedelta(hours=CFG["signal_timeout_h"]):
            cur = cur if cur is not None else sig["entry"]
            reason = "timeout"; result = "win" if cur >= sig["entry"] else "loss"
        if reason:
            sig.update({"exit": round(cur,3), "result": result, "reason": reason, "closed": now.isoformat()})
            closed.append(sig)
        else: still.append(sig)
    state["open_signals"] = still

def maybe_daily_summary(state):
    now = datetime.now(ROME)
    if not (now.hour == CFG["summary_hour"] and now.minute >= CFG["summary_minute"]): return
    today = now.strftime("%Y-%m-%d")
    if state.get("last_summary") == today: return
    closed = state.get("closed_signals", [])
    if not closed: state["last_summary"] = today; return
    wins = sum(1 for c in closed if c["result"]=="win"); n = len(closed)
    rows = "\n".join(f"  {'✅' if c['result']=='win' else '❌'} {c['name']} "
                     f"{c['entry']}→{c['exit']} ({c['reason']})" for c in list(reversed(closed))[:6])
    tg(f"📊 <b>RIEPILOGO</b> {now.strftime('%d/%m/%Y')}\n\n"
       f"Segnali: <b>{n}</b> · Win-rate: <b>{round(wins/n*100)}%</b> ({wins}/{n})\n\n"
       f"<b>Ultimi:</b>\n{rows}\n\n<i>Esito automatico (target TP1 / stop).</i>")
    state["last_summary"] = today

# ─────────────────────────── ORARIO ───────────────────────────
def market_open():
    now = datetime.now(ROME)
    if now.weekday() >= 5: return False
    t = now.hour*60 + now.minute
    return (8*60+CFG["open_skip_min"]) <= t <= (17*60+30-CFG["open_skip_min"])

# ─────────────────────────── MAIN ───────────────────────────
def main():
    state = load_state()
    if not state.get("started"):
        state["started"] = datetime.now(timezone.utc).isoformat()
        tg("🟢 <b>BTP Scanner — piano operativo attivo.</b>\nOgni alert avrà "
           "ingresso, stop e 3 livelli di vendita, più lo spread BTP-Bund.")
        save_state(state)

    if not market_open():
        print("Fuori orario — esco."); maybe_daily_summary(state); save_state(state); return

    spread_bp = get_spread_bp()
    spread_txt, spread_mod = spread_context(state, spread_bp)
    print(f"  Spread: {spread_txt} (mod {spread_mod})")

    bs = state.setdefault("bonds", {}); prices_now, fails = {}, 0
    for bond in BONDS:
        isin = bond["isin"]; st = bs.setdefault(isin, default_bond_state())
        price, source = get_price(isin)
        if price is None: print(f"  ⚠ n/d: {bond['name']}"); fails += 1; continue
        prices_now[isin] = price
        st["prices"].append(price); st["prices"] = st["prices"][-CFG["hist_len"]:]

        res = evaluate(st["prices"], st)
        if res is None: print(f"  {bond['name']}: {price} (riscaldamento)"); continue
        score, grade, triggers, info = res
        score = max(0, min(10, score + spread_mod))   # contributo spread
        print(f"  {bond['name']}: {price} score {score}/10 [{grade}] conf={info['confirms']}")

        ready = True
        if st.get("last_alert"):
            if datetime.now(timezone.utc)-datetime.fromisoformat(st["last_alert"]) < timedelta(minutes=CFG["cooldown_min"]):
                ready = False

        if (score >= CFG["score_alert"] and info["confirms"] >= 1
                and not info["falling_knife"] and ready):
            plan = trade_plan(price, info)
            eco = economics(plan)
            # FILTRO COSTI: salta se a TP1 non resta abbastanza dopo le commissioni
            if eco["net"] < CFG["min_net_eur"]:
                print(f"     ⏭ scartato per costi: netto a TP1 ≈ €{eco['net']} "
                      f"(servono ≥ €{eco['breakeven']:,} di nominale)")
                continue
            tg(alert_msg(bond, score, grade, triggers, info, plan, spread_txt, eco))
            st["last_alert"] = datetime.now(timezone.utc).isoformat()
            state.setdefault("open_signals", []).append({
                "isin": isin, "name": bond["name"], "entry": price,
                "tp1": plan["tp1"], "sl": plan["sl"], "score": score, "grade": grade,
                "time": datetime.now(timezone.utc).isoformat()})
            print(f"  🔔 ALERT {bond['name']} [{grade}]")

    if fails == len(BONDS):
        today = datetime.now(ROME).strftime("%Y-%m-%d")
        if state.get("last_heartbeat") != today:
            tg("⚠️ <b>BTP Scanner</b>: nessun prezzo letto. Probabile cambio sito, parser da aggiornare.")
            state["last_heartbeat"] = today

    track_open_signals(state, prices_now)
    maybe_daily_summary(state)
    save_state(state)
    print("Fatto.")

if __name__ == "__main__":
    main()
