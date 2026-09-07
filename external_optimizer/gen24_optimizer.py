#!/usr/bin/env python3
"""GEN24 post-hoc optimerare (kolumn A/B/C) - extern container.  v2 (rattad).

Laser historik fran Home Assistant via REST API och beraknar:
  A = faktiskt utfall (SEK, netto)
  B = replay av var state machine (SEK, netto)
  C = teoretiskt optimal (SEK, netto)

Alla tre kolumner mats nu pa SAMMA villkor:
  * Samma verkliga start-SoC (last fran sensor, ej hardkodat 50 %).
  * Netto-kostnad = natkostnad - (soc_slut - soc_start) * ref_pris.
    Dvs varje strategi debiteras for att tomma batteriet och krediteras
    for att fylla det, vardera till samma referenspris. Dette gor att
    A - C och B - C ALDRIG kan bli negativa (C ar en akta undre grans).

Skriver resultat tillbaka till HA via REST (input_number/input_text).

Rattningar mot v1 (se CHANGELOG langst ner):
  1. Gemensam SoC-baslinje + terminalvardering av slut-SoC (fix: C > A).
  2. Clamp-reconciliation i B (betala bara for energi som faktiskt lagras/tas ut).
  3. Tidsviktat kop-/saljpris per timme i C (ej sista sampel).
  4. Loggar shadow-action-fordelning (IDLE/CHARGE/DISCHARGE) sa man ser om
     B replayar en passiv (AUTO/IDLE) eller aktiv strategi.

Konfiguration via miljovariabler:
  HA_URL   (default: http://192.168.1.123:80)
  HA_TOKEN (obligatorisk)
  HOURS    (default: 24)
  REF_PRICE_MODE (default: mean_buy; alt: last_buy)
"""
import os
import sys
import datetime
import requests

# --- Konfiguration ---------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "http://192.168.1.123:80")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HOURS = int(os.environ.get("HOURS", "24"))
REF_PRICE_MODE = os.environ.get("REF_PRICE_MODE", "mean_buy")  # mean_buy | last_buy

CAPACITY_KWH = 17.0
EFF = 0.90
MIN_SOC = 5.0
MAX_SOC = 90.0
USABLE = CAPACITY_KWH * (MAX_SOC - MIN_SOC) / 100.0
P_MAX_KW = 5.0  # max ladd/urladdningseffekt (kW)

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def soc_pct_to_kwh(pct):
    """Konvertera SoC-% till anvandbar energi (kWh) i intervallet [0, USABLE]."""
    return clamp(CAPACITY_KWH * (pct - MIN_SOC) / 100.0, 0.0, USABLE)


def grid_cost(grid_kw, buy_price, sell_price, dt):
    """Kostnad for nateffekt over dt timmar.
    grid>0 = import (koppris), grid<0 = export (saljpris)."""
    if grid_kw >= 0:
        return grid_kw * buy_price * dt
    else:
        return grid_kw * sell_price * dt


def api_get(path):
    r = requests.get(f"{HA_URL}{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path, data):
    r = requests.post(f"{HA_URL}{path}", headers=HEADERS, json=data, timeout=30)
    print(f"  POST {path} -> {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()


def get_state(entity):
    try:
        s = api_get(f"/api/states/{entity}")
        return s.get("state")
    except Exception:
        return None


def get_history(entity, start, end):
    """Hamta historik for en entitet via /api/history/period."""
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    path = f"/api/history/period/{start_iso}?end_time={end_iso}&filter_entity_id={entity}&minimal_response"
    try:
        data = api_get(path)
        if not data:
            return []
        out = []
        for series in data:
            for s in series:
                try:
                    out.append((s["last_changed"], float(s["state"])))
                except (KeyError, TypeError, ValueError):
                    continue
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        print(f"  varning: kunde inte hamta historik for {entity}: {e}")
        return []


def get_history_attr(entity, attr, start, end):
    """Hamta historik for ett attribut (t.ex. pv_w, load_w)."""
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    path = f"/api/history/period/{start_iso}?end_time={end_iso}&filter_entity_id={entity}"
    try:
        data = api_get(path)
        if not data:
            return []
        out = []
        for series in data:
            for s in series:
                try:
                    v = s.get("attributes", {}).get(attr)
                    if v is None:
                        continue
                    out.append((s["last_changed"], float(v)))
                except (KeyError, TypeError, ValueError):
                    continue
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        print(f"  varning: kunde inte hamta attribut {attr} for {entity}: {e}")
        return []


def get_history_attr_str(entity, attr, start, end):
    """Hamta historik for ett str-attribut (t.ex. action)."""
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    path = f"/api/history/period/{start_iso}?end_time={end_iso}&filter_entity_id={entity}"
    try:
        data = api_get(path)
        if not data:
            return []
        out = []
        for series in data:
            for s in series:
                v = s.get("attributes", {}).get(attr)
                if v is None:
                    continue
                out.append((s["last_changed"], str(v)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        print(f"  varning: kunde inte hamta str-attribut {attr} for {entity}: {e}")
        return []


def merge(*series_list):
    """Slar ihop tidsserier till tidsordnad lista med senaste varden."""
    events = []
    for key, s in enumerate(series_list):
        for ts, v in s:
            events.append((ts, key, v))
    events.sort(key=lambda x: x[0])
    merged = []
    last = [None] * len(series_list)
    for ts, key, v in events:
        last[key] = v
        merged.append((ts, list(last)))
    return merged


def dp_optimal(hours, usable, eff, p_max, soc_init_kwh, ref_price, k=200):
    """DP over timvis netto-last och pris for att minimera NETTO natkostnad.

    hours: dict {datetime: {'net': kW, 'buy': SEK/kWh, 'sell': SEK/kWh}}
    Netto-mal: sum(natkostnad) - (soc_slut - soc_start) * ref_price.
      -> terminalbelogning +soc_slut*ref_price (soc_start ar konstant).
    Startar pa verkligt soc_init_kwh (ej hardkodat 50 %).
    Ingen "slut >= start"-tvang; terminalvardering garanterar rattvis jamforelse.
    Returnerar (netto_kostnad, slut_soc_kwh).
    """
    times = sorted(hours)
    n = len(times)
    if n == 0:
        return 0.0, soc_init_kwh
    dt = 1.0
    soc_levels = [usable * i / (k - 1) for i in range(k)]
    start_idx = min(range(k), key=lambda i: abs(soc_levels[i] - soc_init_kwh))
    INF = float("inf")

    def trans_cost(s_prev, s, L, buy, sell):
        if s >= s_prev:
            p = (s - s_prev) / (dt * eff)
        else:
            p = (s - s_prev) * eff / dt
        if abs(p) > p_max:
            return None
        return grid_cost(L + p, buy, sell, dt)

    # Forsta timmen fran start-soc
    L0 = hours[times[0]]["net"]
    buy0 = hours[times[0]]["buy"]
    sell0 = hours[times[0]]["sell"]
    dp = [INF] * k
    for i in range(k):
        c = trans_cost(soc_levels[start_idx], soc_levels[i], L0, buy0, sell0)
        if c is not None:
            dp[i] = c

    for t in range(1, n):
        L = hours[times[t]]["net"]
        buy = hours[times[t]]["buy"]
        sell = hours[times[t]]["sell"]
        new_dp = [INF] * k
        for i in range(k):
            s = soc_levels[i]
            best = INF
            for j in range(k):
                if dp[j] == INF:
                    continue
                c = trans_cost(soc_levels[j], s, L, buy, sell)
                if c is not None:
                    tot = dp[j] + c
                    if tot < best:
                        best = tot
            new_dp[i] = best
        dp = new_dp

    # Terminalvardering: minimera natkostnad - soc_slut * ref_price.
    best_net = INF
    best_idx = start_idx
    for i in range(k):
        if dp[i] == INF:
            continue
        net = dp[i] - soc_levels[i] * ref_price
        if net < best_net:
            best_net = net
            best_idx = i
    # Netto inkl. start-SoC-kredit (samma definition som A och B).
    net_cost = best_net + soc_init_kwh * ref_price
    return net_cost, soc_levels[best_idx]


def build_hours(load_w, pv_w, buy_price, sell_price):
    """Aggregera till timmar med TIDSVIKTAT kop/saljpris och medel-nettolast."""
    acc = {}
    prev_ts = None
    for ts, vals in merge(load_w, pv_w, buy_price, sell_price):
        l = (vals[0] or 0.0) / 1000.0
        pv_kw = (vals[1] or 0.0) / 1000.0
        buy = vals[2] or 0.0
        sell = vals[3] or 0.0
        cur = datetime.datetime.fromisoformat(ts)
        if prev_ts is None:
            prev_ts = cur
            continue
        dt = (cur - prev_ts).total_seconds() / 3600.0
        prev_ts = cur
        if dt <= 0:
            continue
        h = cur.replace(minute=0, second=0, microsecond=0)
        if h not in acc:
            acc[h] = {"net_wsum": 0.0, "buy_wsum": 0.0, "sell_wsum": 0.0, "dt": 0.0}
        acc[h]["net_wsum"] += (l - pv_kw) * dt
        acc[h]["buy_wsum"] += buy * dt
        acc[h]["sell_wsum"] += sell * dt
        acc[h]["dt"] += dt
    hours = {}
    for h, a in acc.items():
        if a["dt"] <= 0:
            continue
        hours[h] = {
            "net": a["net_wsum"] / a["dt"],
            "buy": a["buy_wsum"] / a["dt"],
            "sell": a["sell_wsum"] / a["dt"],
        }
    return hours


def replay_lookahead(hours, usable, eff, p_max, soc_init_kwh, ref_price, win_h=6):
    """Replay med en enkel FRAMFORHALLNINGS-policy (kolumn B2).

    Speglar sensor.gen24_price_lookahead: for varje timme, titta pa kommande
    win_h timmar och avgor nuvarande timmes prisplacering (percentil):
      * percentil <= 33 (billig)  -> ladda (om plats i batteriet)
      * percentil >= 67 (dyr)     -> ladda ur (om energi finns)
      * annars                    -> idle
    Effekt begransas av p_max och SoC-grans. Netto-kostnad med samma
    terminalvardering som A/B/C sa B2 ar direkt jamforbar.

    Syfte: kvantifiera om en lookahead-regel slar den nuvarande
    tröskel-baserade state machine (kolumn B) over samma historik.
    Returnerar (netto_kostnad, action_fordelning_str).
    """
    times = sorted(hours)
    n = len(times)
    if n == 0:
        return 0.0, ""
    dt = 1.0
    soc = soc_init_kwh
    grid_cost_b2 = 0.0
    counts = {"CHARGE": 0, "DISCHARGE": 0, "IDLE": 0}
    for i, t in enumerate(times):
        buy = hours[t]["buy"]
        sell = hours[t]["sell"]
        net_load = hours[t]["net"]
        # Fonster = denna + kommande (win_h-1) timmar (buy-pris som signal).
        window = [hours[times[j]]["buy"] for j in range(i, min(i + win_h, n))]
        lo = min(window)
        hi = max(window)
        cur = buy
        pct = 50.0 if (hi - lo) < 1e-4 else (cur - lo) / (hi - lo) * 100.0

        if pct <= 33 and soc < usable:
            # Ladda upp till p_max, begransat av kvarvarande plats.
            room_p = (usable - soc) / (dt * eff)
            p = min(p_max, room_p)
            soc_before = soc
            soc = clamp(soc + p * dt * eff, 0.0, usable)
            stored = soc - soc_before
            grid_kw = net_load + (stored / (dt * eff) if dt > 0 else 0.0)
            counts["CHARGE"] += 1
        elif pct >= 67 and soc > 0:
            # Ladda ur for att tacka last, begransat av tillganglig energi.
            avail_p = soc * eff / dt
            p = min(p_max, avail_p)
            soc_before = soc
            soc = clamp(soc - p * dt / eff, 0.0, usable)
            withdrawn = soc_before - soc
            grid_kw = net_load - (withdrawn * eff / dt if dt > 0 else 0.0)
            counts["DISCHARGE"] += 1
        else:
            grid_kw = net_load
            counts["IDLE"] += 1
        grid_cost_b2 += grid_cost(grid_kw, buy, sell, dt)

    net_cost = grid_cost_b2 - (soc - soc_init_kwh) * ref_price
    tot = sum(counts.values()) or 1
    dist = " ".join(f"{k}={counts[k]}({counts[k]*100//tot}%)"
                    for k in ("CHARGE", "DISCHARGE", "IDLE") if counts[k] > 0)
    return net_cost, dist


def main():
    if not HA_TOKEN:
        print("FEL: HA_TOKEN saknas. Satt miljovariabeln.")
        sys.exit(1)

    print(f"GEN24 optimerare v2 - {HOURS}h historik")
    print(f"HA: {HA_URL}  ref_price_mode={REF_PRICE_MODE}")

    end = datetime.datetime.now()
    start = end - datetime.timedelta(hours=HOURS)

    # --- Hamta data --------------------------------------------------------
    print("Hamtar historik...")
    buy_price = get_history("sensor.gen24_effective_buy_price", start, end)
    sell_price = get_history("sensor.gen24_sell_price", start, end)
    grid_w = get_history_attr("sensor.gen24_state_input", "grid_w", start, end)
    load_w = get_history_attr("sensor.gen24_state_input", "load_w", start, end)
    pv_w = get_history_attr("sensor.gen24_state_input", "pv_w", start, end)
    soc_pct = get_history_attr("sensor.gen24_state_input", "soc_pct", start, end)
    dec_action = get_history_attr_str("sensor.gen24_state_decision", "action", start, end)
    dec_power = get_history_attr("sensor.gen24_state_decision", "power_w", start, end)

    print(f"  datapunkter: grid_w={len(grid_w)}, buy={len(buy_price)}, "
          f"sell={len(sell_price)}, load={len(load_w)}, pv={len(pv_w)}, "
          f"soc={len(soc_pct)}, dec={len(dec_action)}")
    if not grid_w or not buy_price or not sell_price:
        print("VARNING: ingen data anu. Lat systemet ga nagra timmar.")
        write_report_only(0, "ingen data anu")
        return

    # --- Gemensam start-SoC (verklig) -------------------------------------
    if soc_pct:
        soc_start_kwh = soc_pct_to_kwh(soc_pct[0][1])
        soc_end_pct_actual = soc_pct[-1][1]
        soc_end_kwh_actual = soc_pct_to_kwh(soc_end_pct_actual)
        print(f"  SoC start={soc_pct[0][1]:.1f}% ({soc_start_kwh:.2f} kWh), "
              f"slut={soc_end_pct_actual:.1f}% ({soc_end_kwh_actual:.2f} kWh)")
    else:
        soc_start_kwh = USABLE * 0.5
        soc_end_kwh_actual = soc_start_kwh
        print("  varning: ingen soc-historik, faller tillbaka pa 50 %")

    # --- Referenspris for terminalvardering -------------------------------
    #   mean_buy = tidsviktat medel-koppris; last_buy = sista koppris.
    if REF_PRICE_MODE == "last_buy" and buy_price:
        ref_price = buy_price[-1][1]
    else:
        num = 0.0
        den = 0.0
        prev_ts = None
        for ts, v in buy_price:
            cur = datetime.datetime.fromisoformat(ts)
            if prev_ts is None:
                prev_ts = cur
                continue
            dt = (cur - prev_ts).total_seconds() / 3600.0
            prev_ts = cur
            if dt <= 0:
                continue
            num += v * dt
            den += dt
        ref_price = (num / den) if den > 0 else (buy_price[-1][1] if buy_price else 0.0)
    print(f"  ref_price = {ref_price:.4f} SEK/kWh")

    # --- Kolumn A: faktiskt utfall (netto) --------------------------------
    grid_cost_a = 0.0
    prev_ts = None
    for ts, vals in merge(grid_w, buy_price, sell_price):
        g = vals[0] or 0.0
        buy = vals[1] or 0.0
        sell = vals[2] or 0.0
        cur = datetime.datetime.fromisoformat(ts)
        if prev_ts is None:
            prev_ts = cur
            continue
        dt = (cur - prev_ts).total_seconds() / 3600.0
        prev_ts = cur
        if dt <= 0:
            continue
        grid_cost_a += grid_cost(g / 1000.0, buy, sell, dt)
    cost_a = grid_cost_a - (soc_end_kwh_actual - soc_start_kwh) * ref_price

    # --- Kolumn B: replay av var state machine (netto) --------------------
    dec_map = {}
    for ts, a in dec_action:
        dec_map[ts] = (a, 0.0)
    for ts, pw in dec_power:
        if ts in dec_map:
            dec_map[ts] = (dec_map[ts][0], pw)
        else:
            dec_map[ts] = ("IDLE", pw)
    dec_sorted = sorted(dec_map.items())

    action_counts = {"CHARGE": 0, "DISCHARGE": 0, "IDLE": 0, "BLOCK": 0, "OTHER": 0}
    soc_b = soc_start_kwh
    grid_cost_b = 0.0
    prev_ts = None
    for ts, vals in merge(load_w, pv_w, buy_price, sell_price):
        l = (vals[0] or 0.0) / 1000.0
        pv_kw = (vals[1] or 0.0) / 1000.0
        buy = vals[2] or 0.0
        sell = vals[3] or 0.0
        cur = datetime.datetime.fromisoformat(ts)
        if prev_ts is None:
            prev_ts = cur
            continue
        dt = (cur - prev_ts).total_seconds() / 3600.0
        prev_ts = cur
        if dt <= 0:
            continue
        net_load = l - pv_kw
        action = "IDLE"
        power_kw = 0.0
        for dts, (a, pw) in dec_sorted:
            if dts <= ts:
                action, power_kw = a, (pw or 0.0) / 1000.0
            else:
                break
        # Rakna action-fordelning (viktat pa dt).
        key = action if action in action_counts else "OTHER"
        action_counts[key] = action_counts.get(key, 0) + 1

        if action == "CHARGE":
            soc_before = soc_b
            soc_b = clamp(soc_b + power_kw * dt * EFF, 0.0, USABLE)
            stored = soc_b - soc_before            # kWh faktiskt lagrat
            grid_from_batt = stored / (dt * EFF) if dt > 0 else 0.0
            grid_kw = net_load + grid_from_batt    # betala bara for verklig laddning
        elif action == "DISCHARGE":
            soc_before = soc_b
            soc_b = clamp(soc_b - power_kw * dt / EFF, 0.0, USABLE)
            withdrawn = soc_before - soc_b         # kWh faktiskt uttaget
            grid_to_load = withdrawn * EFF / dt if dt > 0 else 0.0
            grid_kw = net_load - grid_to_load      # kreditera bara verklig urladdning
        else:
            grid_kw = net_load
        grid_cost_b += grid_cost(grid_kw, buy, sell, dt)
    cost_b = grid_cost_b - (soc_b - soc_start_kwh) * ref_price

    # --- Kolumn C: teoretiskt optimal (DP, netto) -------------------------
    hours = build_hours(load_w, pv_w, buy_price, sell_price)
    cost_c, soc_end_c = dp_optimal(hours, USABLE, EFF, P_MAX_KW, soc_start_kwh, ref_price)

    # --- Kolumn B2: replay med framforhallnings-policy (lookahead) ---------
    win_h = int(os.environ.get("LOOKAHEAD_HOURS", "6"))
    cost_b2, dist_b2 = replay_lookahead(hours, USABLE, EFF, P_MAX_KW,
                                        soc_start_kwh, ref_price, win_h)
    print(f"  lookahead-replay ({win_h}h) action-fordelning: {dist_b2}")

    # --- Action-fordelning (for tolkning av B) ----------------------------
    tot_actions = sum(action_counts.values()) or 1
    dist = " ".join(
        f"{k}={action_counts[k]}({action_counts[k]*100//tot_actions}%)"
        for k in ("CHARGE", "DISCHARGE", "IDLE", "BLOCK", "OTHER")
        if action_counts[k] > 0
    )
    print(f"  shadow-action-fordelning: {dist}")

    # --- Skriv resultat ---------------------------------------------------
    write_results(cost_a, cost_b, cost_c, len(hours), "ok", dist,
                  cost_b2=cost_b2, win_h=win_h)


def write_report_only(n_hours, note):
    print(f"timmar: {n_hours} ({note})")
    report = (
        f"GEN24 optimering {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"A/B/C oforandrat (ingen ny data)\n"
        f"timmar: {n_hours} ({note})"
    )
    try:
        api_post("/api/services/input_text/set_value", {
            "entity_id": "input_text.gen24_opt_report",
            "value": report[:255],
        })
        print(f"  OK: input_text.gen24_opt_report ({len(report[:255])} tecken)")
    except Exception as e:
        print(f"  FEL vid skrivning till input_text.gen24_opt_report: {e}")


def write_results(cost_a, cost_b, cost_c, n_hours, note, dist="",
                  cost_b2=None, win_h=6):
    print(f"A faktiskt:      {cost_a:.2f} SEK")
    print(f"B replay:        {cost_b:.2f} SEK")
    if cost_b2 is not None:
        print(f"B2 lookahead {win_h}h: {cost_b2:.2f} SEK")
    print(f"C optimal:       {cost_c:.2f} SEK")
    print(f"B-C gap:         {cost_b - cost_c:.2f} SEK")
    if cost_b2 is not None:
        print(f"B2-C gap:        {cost_b2 - cost_c:.2f} SEK")
        print(f"B-B2 (lookahead-vinst): {cost_b - cost_b2:.2f} SEK "
              f"({'+' if cost_b - cost_b2 >= 0 else ''}{cost_b - cost_b2:.2f} = "
              f"{'lookahead battre' if cost_b - cost_b2 > 0.01 else 'ingen vinst'})")
    print(f"A-C gap:         {cost_a - cost_c:.2f} SEK")
    print(f"timmar: {n_hours} ({note})")

    def _set_input_number(entity_id, value):
        try:
            api_post("/api/services/input_number/set_value", {
                "entity_id": entity_id,
                "value": round(value, 2),
            })
            print(f"  OK: {entity_id} = {value:.2f}")
        except Exception as e:
            print(f"  FEL vid skrivning till {entity_id}: {e}")

    def _set_input_text(entity_id, value):
        try:
            api_post("/api/services/input_text/set_value", {
                "entity_id": entity_id,
                "value": value,
            })
            print(f"  OK: {entity_id} ({len(value)} tecken)")
        except Exception as e:
            print(f"  FEL vid skrivning till {entity_id}: {e}")

    _set_input_number("input_number.gen24_opt_cost_actual", cost_a)
    _set_input_number("input_number.gen24_opt_cost_replay", cost_b)
    _set_input_number("input_number.gen24_opt_cost_optimal", cost_c)

    b2_line = ""
    if cost_b2 is not None:
        b2_line = (f"B2 lookahead {win_h}h: {cost_b2:.2f} SEK "
                   f"(vinst {cost_b - cost_b2:+.2f})\n")
    report = (
        f"GEN24 optimering {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"A faktiskt: {cost_a:.2f} SEK\n"
        f"B replay:   {cost_b:.2f} SEK\n"
        f"{b2_line}"
        f"C optimal:  {cost_c:.2f} SEK\n"
        f"B-C gap:    {cost_b - cost_c:.2f} SEK\n"
        f"A-C gap:    {cost_a - cost_c:.2f} SEK\n"
        f"{dist}\n"
        f"timmar: {n_hours} ({note})"
    )
    _set_input_text("input_text.gen24_opt_report", report[:255])


if __name__ == "__main__":
    main()

# =============================================================================
# CHANGELOG v1 -> v2
# =============================================================================
# BUG 1 (fix): C kunde bli dyrare an A (A-C < 0) -> ingen giltig optimum.
#   Orsak: DP tvingade slut-SoC >= start och startade alltid pa 50 %, medan A
#   fritt fick tomma batteriet. Nu: alla tre startar pa samma verkliga start-SoC
#   och netto-kostnad = natkostnad - (soc_slut - soc_start) * ref_price. C blir
#   en akta undre grans (A-C >= 0, B-C >= 0).
# BUG 2 (fix): B betalade for laddning som inte skedde nar batteriet var fullt
#   (clamp klamde SoC men grid_kw laddade anda). Nu reconcilias grid mot faktiskt
#   lagrad/uttagen energi efter clamp.
# BUG 3 (fix): timpris i C tog sista sampel per timme. Nu tidsviktat medel.
# BUG 4 (nytt): loggar shadow-action-fordelning sa man ser om B replayar en
#   passiv (AUTO/IDLE) strategi -> forklarar B >> A i Fas 1 (read-only).
# =============================================================================
