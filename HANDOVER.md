# Handover-dokument: Gen24-Shadow (.123) — ändringar och planerade förbättringar

**Datum:** 2026-09-01  
**Repo:** https://github.com/hakha4/Gen24-Shadow  
**Lokal kopia (Windows):** `C:\Gen24-Shadow`  
**HA-instans (.123):** `Y:\packages` (nätverksdelad mapp, //192.168.1.123/config/packages)

---

## 1. Genomförda ändringar (commits a958210 + 21e299a)

### Problem som identifierades

Shadow-systemet (.123) hade **gammal prismodell** som inte var synkad med produktionens senaste VAT-fixes (commits f40a98f + d4c234c från .161). Detta gav:

| Inkonsistens | Konsekvens |
|---|---|
| `sensor.gen24_price_hub` saknade 13 nya moms-attribut | Evaluatorn kunde inte analysera köp/sälj-logiken korrekt |
| `ps_13_electricity_pricing.yaml` saknades helt | Dead references i price_hub (`gen24_sell_margin`, `gen24_sell_breakeven` undefined) |
| `gen24_sell_cost_ore` helper saknades | Hardcoded fallback 3.71 öre användes alltid, UI-inställning funkade inte |
| Dubbelarbete på VAT-källa | Risk att produktion och shadow divergerar vid framtida ändringar |

### Åtgärdade filer

**Commit a958210:** "sync price model from production"

1. **`40_price_provider.yaml`** — ersatt helt med produktionsversion
   - `gen24_sell_price`: nu spot **EXKL moms** (elpris_se3 primärt, Nordpool/momsfaktor fallback)
   - `gen24_sell_threshold`: dagsmedel EXKL moms-termer
   - `gen24_price_hub`: **13 nya attribut**:
     ```yaml
     vat_pct, buy_includes_vat, sell_includes_vat,
     spot_incl_vat_sek, spot_excl_vat_sek,
     buy_energy_tax_sek, buy_supplier_markup_sek, buy_grid_fee_sek,
     sell_tax_reduction_sek, sell_markup_sek, sell_broker_cost_sek,
     moms_modell (förklaringstext),
     is_sell_attractive_arbitrage
     ```

2. **`sensors/ps_13_electricity_pricing.yaml`** — ny fil, portad från produktion
   - `gen24_total_buy_price` (öre/kWh, inkl moms)
   - `gen24_sell_breakeven` (öre/kWh, single VAT source-fix)
   - `gen24_sell_margin` (öre/kWh, vinst/förlust)
   - `gen24_export_profit_status` (vinst sol/batteri/förlust)
   - Helper `gen24_sell_cost_ore`: **initial: 0** (noll mäklaravgift, matchar användarkontraktet)

**Commit 21e299a:** "clean: remove .bak file + add *.bak to .gitignore"
   - Tog bort `gen24_shadow_decision_dataset.yaml.pre-atomic-20260829-185919.bak`
   - Lade till `.gitignore` med `*.bak`

### Validering utförd

✅ YAML-syntax (båda filer)  
✅ Duplicate-key-check (custom DupCheckLoader)  
✅ Cross-file unique_id-check (inga konflikter)  
✅ Externa entity-beroenden verifierade (alla sensorer finns i shadow)  
✅ Inga shadow-specifika referenser som bröts

---

## 2. VAD SOM ÅTERSTÅR ATT GÖRA PÅ .123

### A. Aktivera de synkade filerna (manuellt, omedelbart)

**Status:** Ändringar finns i GitHub och `C:\Gen24-Shadow` men **inte kopierade till .123 än**.

**Kommando (PowerShell på Windows):**
```powershell
cd C:\Gen24-Shadow
Copy-Item -Path "40_price_provider.yaml" -Destination "Y:\packages\" -Force
Copy-Item -Path "sensors\ps_13_electricity_pricing.yaml" -Destination "Y:\packages\sensors\" -Force
Copy-Item -Path ".gitignore" -Destination "Y:\packages\" -Force
```

Om `Y:\packages\sensors\` inte finns:
```powershell
New-Item -ItemType Directory -Path "Y:\packages\sensors\" -Force
```

**Sedan:** Starta om HA på .123 (UI → Inställningar → System → Starta om)

---

### B. Kritiska kodförbättringar (ännu EJ implementerade)

Baserat på analys av `SHADOW_SYSTEM_OVERVIEW.md`:

#### 🔴 **1. Prio-65-kollision (KRITISK — fixas innan dispatcher aktiveras)**

**Fil:** `20_state_machine.yaml`  
**Problem:** `SOLAR_CHARGE` och `SELF_CONS_NIGHT` har båda prio 65 → vilken som vinner beror på YAML-ordning (odefinierat beteende).  
**Fix:** Ge dem olika prio, t.ex:
```yaml
SELF_CONS_NIGHT: 66  # Självkonsumtion på natten viktigare
SOLAR_CHARGE: 64     # Solöverskott sekundärt
```
**Motivering:** När flera guards kan vara sanna samtidigt måste ordningen vara deterministisk.

---

#### ⚠️ **2. Guards "not implemented" — scoreboard inte representativ**

**Fil:** `20_state_machine.yaml`  
**Problem:** Många states (PEAK_SHAVE, NIGHT_BATTERY, CHARGE_CHEAP, DAYTIME_SMART etc.) har `reason: "..._guard_not_implemented"` → evaluatorns quality score mäter bara en bråkdel av systemet.  
**Åtgärd:** 
1. Lägg till attribut i `gen24_state_decision`:
   ```yaml
   guards_implemented: "4/10"  # räkna färdiga guards
   ```
2. Implementera guards stegvis innan dispatcher aktiveras.

---

#### 🔧 **3. Snapshot-ålder 10 000 ms — troligen för tight**

**Fil:** `20_state_machine.yaml` (guard i `gen24_state_decision`)  
**Problem:** Hard-coded 10s-tröskel kan ge falska DATA_QUALITY_REJECT vid nätverksjitter.  
**Fix:**
1. Mät faktisk P99-latens på `snapshot_age_ms` i 24h
2. Sätt tröskel till 3× median (inte godtyckligt 10s)
3. Lägg till attribut: `snapshot_age_p99_ms`, `snapshot_age_median_ms`

---

#### 🔧 **4. Optimerare: cron → event-trigger**

**Fil:** `45_optimizer.yaml` (extern LXC 192.168.1.122)  
**Problem:** Körs var 6:e timme (06:00/12:00/18:00/00:00) → missar/duplicerar Nordpool-prisuppdatering (~13:15).  
**Fix:** Trigga på `sensor.nordpool_kwh_se3_sek_3_10_025` state change (när nästa dags priser kommer) + en backup-körning 06:00.

---

#### 🔧 **5. DP-optimerare: end-SoC constraint för konservativ**

**Fil:** Extern optimerare (LXC, Python DP-kod)  
**Problem:** Kravet `end_soc >= start_soc` är fel — om man startar på 85% SoC och priset är högt kan 60% slut-SoC vara optimalt (vinst på urladdning).  
**Fix:** Ändra constraint till:
```python
end_soc >= gen24_battery_min_soc_pct  # t.ex. 10%, inte start-SoC
```

---

#### 🔧 **6. Evaluator: kaskadat unavailable → förlorad data**

**Fil:** `25_state_evaluator.yaml`  
**Problem:** Under MQTT-avbrott blir hela kedjan `unavailable` → evaluatorhistorik försvinner, kan inte skilja "data rejected" från "data lost".  
**Fix:** Lägg till state `NO_DATA` i evaluatorn (istället för unavailable) med tidsstämpel, så avbrott kan analyseras i efterhand.

---

#### 📋 **7. Aktiveringskriterium för dispatcher — saknas kvantitativt mål**

**Fil:** Policy-beslut (ingen kod)  
**Problem:** Dokumentet säger "när scoren är stabil och bra" utan siffror.  
**Förslag:** Definiera explicit kriterium:
```
quality_score >= 85% OCH reject_ratio < 5% i minst 72 timmar
```

---

#### ❓ **8. Kolumn B (replay vs parallel run) — klarifiering behövs**

**Fil:** `35_economic_evaluator.yaml`  
**Fråga:** Är kolumn B ett *replay* (feeda .161:s historik in i .123:s state_decision) eller en *parallel run* (state_decision kör på .123:s live data)?  
**Åtgärd:** Klargör i kod/dokumentation vilken det är — de är fundamentalt olika saker.

---

## 3. Arkitektonisk kontext (för den nya AI:n)

### Två HA-instanser
| Instans | IP | Repo | Roll |
|---|---|---|---|
| **PRODUKTION** | 192.168.1.161 | Gen24-Bridge-refactored | Live system — RÖR INTE |
| **SHADOW** | 192.168.1.123 | Gen24-Shadow | Utveckling/test — allt arbete här |

### Shadow-system syfte
Ersätta ~40 röriga automationer med en **tillståndsmaskin**. Fas 1 (nuvarande): read-only, jämför beslut mot verklighet. Fas 2: aktivera dispatcher → faktisk styrning.

### Kärnprincip
**En beslutsfattare** (`sensor.gen24_state_decision`) → deterministisk prioritetstabell → separata guards för batteri/EV → reject ≠ AUTO (felsäker hållning).

### Viktigaste filer i shadow
```
20_state_machine.yaml         ← state_input, state_decision, control_hub
25_state_evaluator.yaml        ← agreement/reject scorecard
30_state_dispatcher.yaml       ← inaktiv (initial_state: false)
40_price_provider.yaml         ← NYSYNKAD med produktion
sensors/ps_13_electricity_pricing.yaml  ← NY fil, portad från produktion
45_optimizer.yaml              ← extern LXC-optimerare
```

---

## 4. Nästa steg — prioritetsordning

**OMEDELBART (innan dispatcher aktiveras):**
1. ✅ Kopiera synkade filer till .123 (se sektion 2A ovan)
2. ✅ Starta om HA → verifiera att inga fel i loggen
3. 🔴 Fixa prio-65-kollisionen (`20_state_machine.yaml`)
4. ⚠️ Implementera resterande guards eller markera explicit vilka som saknas

**FÖRE AKTIVERING:**
5. 🔧 Mät snapshot-latens, justera tröskel
6. 📋 Definiera aktiveringskriterium (quality score + reject_ratio + tid)
7. 🔧 Evaluator NO_DATA-state
8. ❓ Klargör kolumn B (replay vs parallel)

**EFTER AKTIVERING (optimeringar):**
9. 🔧 Event-trigger för optimerare
10. 🔧 DP end-SoC constraint

---

## 5. Gotchas att komma ihåg

1. **DUPLICATE-KEY**: HA errors på dubletter, men `yaml.safe_load` i Python döljer dem → alltid grep-check efter attribut-tillägg
2. **HELPER `initial`**: Endast använd när helper först skapas, UI-ändringar permanent i `.storage/core.restore_state`
3. **Reject ≠ AUTO**: Vid osäker data → BLOCK, inte fallback till AUTO
4. **Kaskadat unavailable**: `valid: false` från firmware → hela kedjan blir unavailable

---

## 6. Produktionskontext (Gen24-Bridge-refactored)

### Senaste commits i produktion (.161)
- **e31d30d:** Stale lock release fix
- **62bc2e4:** sell_price nu EXKL moms
- **801dd49:** Docs clarification
- **f40a98f:** Price consistency: single VAT source, buy=incl/sell=excl, add price_hub moms model attributes
- **d4c234c:** Set gen24_sell_cost_ore initial to 0 (zero broker fee default)

### Workflow för produktionsändringar
```bash
# På Abacus VM
cd /home/ubuntu/project/clean_build_ready
# ... gör ändringar ...
git add .
git commit -m "..."
git push origin main
git push origin master  # push till BÅDA branches

# Användaren på HA .161:
bash /config/gen24-pull.sh
# Starta om HA
```

### Viktiga produktionsfiler
```
packages/gen24_price_provider.yaml
packages/gen24_automations_ver12.yaml
packages/sensors/ps_13_electricity_pricing.yaml
packages/sensors/ps_02_battery_hub.yaml
```

---

**Slutsats:** Prismodellen är nu synkad och identisk mellan produktion och shadow. De 8 förbättringspunkterna ovan är nästa fas — börja med prio-65-kollisionen (kritisk). Lycka till! 🚀
