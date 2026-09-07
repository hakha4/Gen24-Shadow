# Extern GEN24-optimerare (kolumn A/B/C)

Optimeraren körs i en **extern container** (Proxmox LXC) och pratar med Home
Assistant via REST API. Detta undviker HA:s begränsade `python_script`-miljö
(ingen import av `datetime`, `json`, `os`, `homeassistant.components` osv).

> **v2 (rättad) — läs detta först**
>
> Version 2 rättar fyra fel i A/B/C-bokföringen som gjorde kolumnerna
> ojämförbara (bl.a. att `C` kunde bli dyrare än `A`, vilket är omöjligt för en
> äkta optimum). Se avsnitt **"v2 — vad som rättades"** längst ned. Kör alltid
> `gen24_optimizer.py` (v2). Den gamla logiken finns kvar dokumenterad i
> CHANGELOG i slutet av python-filen.

## Arkitektur

```
[Proxmox LXC]  gen24_optimizer.py  --REST-->  Home Assistant (.123)
   cron var 6:e timme                        |  läser historik (PV, last, grid, pris, beslut, soc)
                                              |  skriver resultat till:
                                              |    input_number.gen24_opt_cost_actual/_replay/_optimal
                                              |    input_text.gen24_opt_report
```

## Fördelar / nackdelar

**Fördelar:** full Python (pandas, numpy, scipy), robust felhantering, separat
livscykel (ingen HA-omstart), kan senare bli en riktig forecast+optimerare.

**Nackdelar:** en extra rörlig del att drifta, kräver token-hantering, REST
har viss overhead (irrelevant för en post-hoc-optimerare som körs var 6:e timme).

## Prismodell (realistisk köp/sälj)

Modellen skiljer på köp- och säljpris, hämtat från `gen24_price_provider`
(paket `40_price_provider.yaml` på .123):
- **Köp (import):** `sensor.gen24_effective_buy_price` (SEK/kWh) — full köpkostnad
  (spot + energiskatt + påslag + nätavgift + moms).
- **Sälj (export):** `sensor.gen24_sell_price` (SEK/kWh) — spot + sälj-adders.

Detta gör ren prishandel (köp billigt / sälj dyrt) mestadels olönsam, medan
batteriets verkliga värde kvarstår: ladda från överskotts-sol och täcka last
vid dyra timmar. Justera `elpris_*`-helpers i `40_price_provider.yaml` efter
din faktura.

## 1. Skapa Long-Lived Access Token i HA

1. Home Assistant UI → Profil (klicka på ditt användarnamn nere till vänster).
2. Scrolla till **Long-Lived Access Tokens** → **Skapa token**.
3. Namnge den t.ex. `gen24_optimizer` och kopiera token (visas bara en gång).
4. **Spara token säkert** — den ger full åtkomst till HA. Lägg den i en
   `.env`-fil eller i containerns miljövariabler, aldrig i git.

## 2. Skapa LXC-container på Proxmox

1. Proxmox → Create CT (LXC), t.ex. Debian 12 template.
2. Ge den en IP (t.ex. `192.168.1.124`) och tillräckligt med resurser
   (1 CPU, 512 MB RAM räcker).
3. Starta containern och logga in.

## 3. Installera Python + skript

```bash
# i LXC-containern
apt update && apt install -y python3 python3-pip python3-venv
mkdir -p /opt/gen24_optimizer
cd /opt/gen24_optimizer
python3 -m venv venv
source venv/bin/activate
pip install requests
```

Kopiera `gen24_optimizer.py` till `/opt/gen24_optimizer/`.

## 4. Konfigurera token (säkert)

Skapa en `.env`-fil (skyddad, inte i git):

```bash
cd /opt/gen24_optimizer
cat > .env <<'EOF'
HA_URL=http://192.168.1.123:80
HA_TOKEN=<DIN_LONG_LIVED_TOKEN>
HOURS=24
REF_PRICE_MODE=mean_buy
EOF
chmod 600 .env
```

Priserna (köp/sälj) hämtas från `sensor.gen24_effective_buy_price` och
`sensor.gen24_sell_price` i HA (paket `40_price_provider.yaml`). Justera
`elpris_*`-helpers där efter din faktura — inga priser i `.env`.

`REF_PRICE_MODE` styr terminalvärderingen av kvarvarande batterienergi:
- `mean_buy` (default): tidsviktat medel-köppris över fönstret.
- `last_buy`: sista köppriset i fönstret.

## 5. Testa manuellt

```bash
cd /opt/gen24_optimizer
source venv/bin/activate
set -a; source .env; set +a
python3 gen24_optimizer.py
```

Du ska se A/B/C-kostnaderna i terminalen (plus shadow-action-fördelningen),
och de ska dyka upp i HA-panelen (kort 8 "GEN24 Optimizer").

## 6. Schemalägg med cron

```bash
crontab -e
# kör var 6:e timme
0 */6 * * * cd /opt/gen24_optimizer && set -a && source .env && set +a && venv/bin/python3 gen24_optimizer.py >> /var/log/gen24_optimizer.log 2>&1
```

## 7. Tolkning av resultat

| Mått | Betydelse |
|---|---|
| **A** | Faktiskt utfall (SEK, netto) — vad den gamla styrningen kostade |
| **B** | Replay av vår state machine (SEK, netto) — vad vår styrning skulle ha kostat |
| **B2** | Replay med **framförhållnings-policy** (lookahead, kommande N h) — ladda i fönstrets billigaste timmar, ladda ur i de dyraste |
| **C** | Teoretiskt optimal (SEK, netto) — med facit i hand |
| **B−C gap** | Hur mycket vår styrning tappar mot optimal |
| **B−B2** | **Lookahead-vinsten** — positivt = framförhållning hade sparat pengar mot dagens regler |
| **A−C gap** | Hur mycket den gamla styrningen tappar |

**Ger framförhållning mer? (B2)** Kolumn B2 speglar HA-sensorn
`sensor.gen24_price_lookahead` (paket `sensors/ps_21_price_lookahead.yaml`):
för varje timme tittar den kommande N timmar framåt (default 6, styrs av
`LOOKAHEAD_HOURS` i `.env` eller `input_number.gen24_lookahead_hours` i HA) och
laddar i billiga timmar / laddar ur i dyra. **B − B2 mäter över tid** om en
lookahead-regel hade slagit dagens tröskel-baserade state machine. Kör var 6:e
timme och följ B−B2 över dagar innan du bygger in lookahead i state machine
(Fas 2). B2 mäts med samma start-SoC och terminalvärdering som B/C → direkt
jämförbar (C ≤ B2 garanterat).

Alla tre mäts på **samma villkor**: samma verkliga start-SoC och samma
terminalvärdering av slut-SoC. Därför gäller alltid **A−C ≥ 0** och **B−C ≥ 0**
(C är en äkta undre gräns). Om `B−C < A−C` förbättrar vår state machine ekonomin.

**Shadow-action-fördelning:** rapporten visar nu andelen IDLE/CHARGE/DISCHARGE
i replayn. I **Fas 1 (read-only)** står state machine oftast i AUTO → `IDLE`,
och då mäter B "utan batterioptimering" — vilket förklarar att B kan ligga
över A. Titta på fördelningen innan du tolkar B−C.

## 8. Felsökning

- **401 Unauthorized** → token fel eller utgången. Skapa ny.
- **Ingen data** → systemet har inte gått tillräckligt länge, eller entiteterna
  har inte historik. Kontrollera att `sensor.gen24_state_input`,
  `sensor.gen24_effective_buy_price` och `sensor.gen24_sell_price` har värden.
- **Connection refused** → fel HA_URL eller port. Kontrollera att HA är nåbar
  från containern (`curl http://192.168.1.123:80`).
- **B >> A** → kolla shadow-action-fördelningen i rapporten. Om mest `IDLE`
  (Fas 1/AUTO) är det väntat, inte ett fel.

## 9. Nästa steg

- **Förbättra kolumn C** — DP är implementerad; kan bytas mot linjärprogrammering
  (scipy) för kontinuerlig effekt och fler begränsningar.
- **Lägg till effekttariff** — peak-term när tariffen återinförs.
- **Koppla till dispatchern** — när state machine är validerad kan optimeraren
  även föreslå nästa dags plan (forecast + optimering).

## v2 — vad som rättades

| # | Fel i v1 | Effekt | Rättning i v2 |
|---|---|---|---|
| 1 | DP tvingade slut-SoC ≥ start och startade alltid på 50 %, medan A fritt fick tömma batteriet | **C kunde bli dyrare än A** (A−C < 0) — omöjligt för en äkta optimum | Alla tre startar på samma **verkliga** start-SoC; netto = nätkostnad − (soc_slut − soc_start)·ref_pris. C blir en äkta undre gräns |
| 2 | I B laddade `grid_kw` även när `clamp` hindrade SoC att öka (fullt batteri) | B **betalade för el som aldrig lagrades** → uppblåst kostnad | Grid reconcilias mot faktiskt lagrad/uttagen energi efter clamp |
| 3 | Timpriset i C tog *sista* sampel per timme | Prisfel i C | Tidsviktat medel av köp/säljpris per timme |
| 4 | Ingen insyn i vilken strategi B replayade | Svårt att förstå B >> A | Loggar **shadow-action-fördelning** (IDLE/CHARGE/DISCHARGE) |

Verifierat med syntetiska tester: C är aldrig dyrare än vare sig en passiv
IDLE-policy eller en greedy ladda/ladda-ur-policy (A−C ≥ 0, B−C ≥ 0 håller).
