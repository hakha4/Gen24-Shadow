#!/bin/bash
# =============================================================
# GEN24 SHADOW PULL (.123)
# Hämtar senaste shadow-konfiguration från GitHub Gen24-Shadow
# och kopierar in i /config/packages.
# =============================================================
set -euo pipefail

REPO_DIR="/config/.gen24shadow_git"
CONFIG_DIR="/config"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPO_URL="https://github.com/hakha4/Gen24-Shadow.git"

echo "=========================================="
echo " GEN24 SHADOW PULL  ($STAMP)"
echo "=========================================="

# 1. Initialisera repo om det inte finns
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "[INIT] Första körningen - klonar Gen24-Shadow-repo..."
  git clone "$REPO_URL" "$REPO_DIR"
  echo "      OK - repo klonat till $REPO_DIR"
else
  echo "[1/4] Repo finns - hämtar uppdateringar från GitHub..."
  cd "$REPO_DIR"
  git fetch origin
  git reset --hard origin/main
  echo "      OK - repo uppdaterat till senaste origin/main"
fi

# 2. Backup av nuvarande packages/ i /config
echo "[2/4] Säkerhetskopierar nuvarande packages/..."
BK="/config/.gen24shadow_backup_$STAMP"
mkdir -p "$BK"
if [ -d "$CONFIG_DIR/packages" ]; then
  cp -a "$CONFIG_DIR/packages" "$BK/"
  echo "      OK - backup i $BK"
else
  echo "      Ingen packages/-mapp att säkerhetskopiera (första gången?)"
fi

# 3. Kopiera IN packages/ från repo
echo "[3/4] Kopierar in shadow-packages från GitHub..."
rm -rf "$CONFIG_DIR/packages"
mkdir -p "$CONFIG_DIR/packages"

# Kopiera YAML-filer från repo root till /config/packages/
# OBS: lovelace/*.yaml kopieras INTE hit - de är Lovelace-kort, inte HA packages
for f in "$REPO_DIR"/*.yaml; do
  [ -f "$f" ] && cp -a "$f" "$CONFIG_DIR/packages/"
done

# Kopiera sensors/-mappen om den finns
if [ -d "$REPO_DIR/sensors" ]; then
  mkdir -p "$CONFIG_DIR/packages/sensors"
  cp -a "$REPO_DIR/sensors"/*.yaml "$CONFIG_DIR/packages/sensors/" 2>/dev/null || true
fi

# Kopiera gen24-pull.sh själv (så framtida uppdateringar hämtas automatiskt)
if [ -f "$REPO_DIR/gen24-pull.sh" ]; then
  cp -a "$REPO_DIR/gen24-pull.sh" "$CONFIG_DIR/"
  chmod +x "$CONFIG_DIR/gen24-pull.sh"
fi

echo "      OK - Shadow-packages på plats."

# 4. Rensa gamla backuper - behåll bara de 3 senaste
echo "      Rensar gamla backuper (behåller 3 senaste)..."
OLD_BACKUPS=$(ls -1dt /config/.gen24shadow_backup_* 2>/dev/null | tail -n +4)
if [ -n "$OLD_BACKUPS" ]; then
  echo "$OLD_BACKUPS" | while read dir; do
    rm -rf "$dir" && echo "        Raderad: $(basename $dir)"
  done
else
  echo "        Inga gamla backuper att radera."
fi

# 5. Påminnelse
echo "[4/4] KLART."
echo "------------------------------------------"
echo "NÄSTA STEG (manuellt i HA):"
echo "  1) Developer Tools → YAML → Check Configuration"
echo "  2) Om giltig → Developer Tools → YAML → Reload All"
echo "     (eller starta om HA om nya helpers/platforms tillkom)"
echo ""
echo "Backup av gamla packages finns i: $BK"
echo "Senaste commit från GitHub: $(cd $REPO_DIR && git log -1 --oneline)"
echo "------------------------------------------"
