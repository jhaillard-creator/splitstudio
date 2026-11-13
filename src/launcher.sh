#!/bin/bash
# Split Studio launcher: installs its own Python runtime on first launch, then opens the native window.
# Runs without Terminal. Everything it does is logged to ~/Library/Logs/Split Studio.log.
set -u

RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SUPPORT="$HOME/Library/Application Support/Split Studio"
LOG="$HOME/Library/Logs/Split Studio.log"
UV="$SUPPORT/bin/uv"
VENV="$SUPPORT/runtime"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
export SPLITSTUDIO_DATA="$SUPPORT/.studio"
export UV_PYTHON_INSTALL_DIR="$SUPPORT/python"
export UV_CACHE_DIR="$SUPPORT/cache"
export UV_NO_PROGRESS=1

mkdir -p "$SUPPORT/bin" "$(dirname "$LOG")"
# Keep the log from growing forever.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5000000 ]; then mv -f "$LOG" "$LOG.old"; fi
exec >>"$LOG" 2>&1
echo "===== $(date) · Split Studio ====="

notify() {
  /usr/bin/osascript - "$1" <<'APPLESCRIPT' >/dev/null 2>&1 || true
on run argv
  display notification (item 1 of argv) with title "Split Studio"
end run
APPLESCRIPT
}

fail() {
  echo "ERREUR : $1"
  /usr/bin/osascript - "$1" "$LOG" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set reply to display dialog (item 1 of argv) & return & return & "Le journal détaillé est disponible." with title "Split Studio" buttons {"Afficher le journal", "Fermer"} default button "Fermer" with icon stop
  if button returned of reply is "Afficher le journal" then do shell script "open -R " & quoted form of (item 2 of argv)
end run
APPLESCRIPT
  exit 1
}

# 0. Automatic updates: compare our build number with the latest GitHub release, swap the app if newer.
REPO="jhaillard-creator/splitstudio"
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

update_app() {
  [ -n "${SPLITSTUDIO_UPDATED:-}" ] && return 0
  case "$APP_DIR" in *.app) ;; *) return 0 ;; esac
  local base current remote tmp expected actual new backup
  base="${SPLITSTUDIO_UPDATE_URL:-https://raw.githubusercontent.com/$REPO/main/release}"
  current="$(tr -dc '0-9' < "$RESOURCES/BUILD" 2>/dev/null)"
  remote="$(curl -fsSL --max-time 6 "$base/build.txt" 2>/dev/null | tr -dc '0-9')"
  [ -n "$remote" ] || return 0
  [ $(( remote > ${current:-0} )) -eq 1 ] || return 0
  if [ ! -w "$APP_DIR" ] || [ ! -w "$(dirname "$APP_DIR")" ]; then
    echo "Mise à jour $remote disponible, mais l’app n’est pas modifiable à cet emplacement ($APP_DIR)."
    return 0
  fi
  echo "Mise à jour : build ${current:-0} -> $remote"
  notify "Mise à jour de Split Studio en cours…"
  tmp="$(mktemp -d)"
  if ! curl -fsSL --retry 2 --max-time 300 "$base/SplitStudio.zip" -o "$tmp/update.zip" \
     || ! curl -fsSL --max-time 30 "$base/SplitStudio.zip.sha256" -o "$tmp/update.sha256"; then
    echo "Téléchargement de la mise à jour impossible, lancement de la version actuelle."
    rm -rf "$tmp"; return 0
  fi
  expected="$(tr -dc '0-9a-f' < "$tmp/update.sha256" | cut -c1-64)"
  actual="$(shasum -a 256 "$tmp/update.zip" | cut -d' ' -f1)"
  if [ "${#expected}" -ne 64 ] || [ "$expected" != "$actual" ]; then
    echo "Mise à jour rejetée : l’empreinte SHA-256 ne correspond pas."
    rm -rf "$tmp"; return 0
  fi
  mkdir -p "$tmp/x"
  if [ -x /usr/bin/ditto ]; then /usr/bin/ditto -x -k "$tmp/update.zip" "$tmp/x"; else unzip -q "$tmp/update.zip" -d "$tmp/x"; fi
  new="$tmp/x/Split Studio.app"
  if [ ! -x "$new/Contents/MacOS/SplitStudio" ] || [ "$(tr -dc '0-9' < "$new/Contents/Resources/BUILD" 2>/dev/null)" != "$remote" ]; then
    echo "Mise à jour rejetée : archive incomplète."
    rm -rf "$tmp"; return 0
  fi
  backup="$SUPPORT/previous-version.app"
  rm -rf "$backup"
  if ! mv "$APP_DIR" "$backup"; then rm -rf "$tmp"; return 0; fi
  if ! mv "$new" "$APP_DIR"; then
    mv "$backup" "$APP_DIR"
    echo "Remplacement impossible, version actuelle conservée."
    rm -rf "$tmp"; return 0
  fi
  xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
  rm -rf "$tmp"
  echo "Mise à jour installée : build $remote"
  notify "Split Studio est à jour."
  SPLITSTUDIO_UPDATED=1 exec "$APP_DIR/Contents/MacOS/SplitStudio"
}
update_app

# 1. uv: a small, fast installer that also provides a private Python, so nothing is needed beforehand.
if [ ! -x "$UV" ]; then
  notify "Premier lancement : installation du moteur (quelques minutes). L’application s’ouvrira toute seule."
  case "$(uname -m)" in
    arm64) TARGET="aarch64-apple-darwin" ;;
    *) TARGET="x86_64-apple-darwin" ;;
  esac
  TMP="$(mktemp -d)"
  if ! curl -fsSL --retry 3 --connect-timeout 20 \
      "https://github.com/astral-sh/uv/releases/latest/download/uv-$TARGET.tar.gz" -o "$TMP/uv.tar.gz"; then
    rm -rf "$TMP"
    fail "Impossible de télécharger le moteur de Split Studio. Vérifie ta connexion Internet puis rouvre l’application."
  fi
  tar -xzf "$TMP/uv.tar.gz" -C "$TMP" || fail "L’archive du moteur est illisible. Rouvre l’application pour réessayer."
  FOUND="$(find "$TMP" -type f -name uv -perm -u+x | head -n 1)"
  [ -n "$FOUND" ] || fail "Le moteur téléchargé est incomplet. Rouvre l’application pour réessayer."
  mv -f "$FOUND" "$UV"
  chmod +x "$UV"
  rm -rf "$TMP"
fi

# 2. Private Python 3.12 + dependencies, reinstalled only when the requirements change.
WANTED="$(cat "$RESOURCES"/requirements*.txt | shasum | cut -d' ' -f1)-py312"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$VENV/.ready" 2>/dev/null)" != "$WANTED" ]; then
  notify "Installation des composants vidéo et IA… Cela ne se produit qu’une fois."
  if [ ! -x "$VENV/bin/python" ]; then
    "$UV" venv --python 3.12 --python-preference only-managed "$VENV" \
      || fail "Impossible d’installer Python pour Split Studio. Vérifie ta connexion Internet puis rouvre l’application."
  fi
  (cd "$RESOURCES" && "$UV" pip install --python "$VENV/bin/python" -r requirements.txt) \
    || fail "L’installation des composants a échoué. Vérifie ta connexion Internet puis rouvre l’application."
  echo "$WANTED" > "$VENV/.ready"
  date +%s > "$VENV/.ytdlp"
fi

# 3. Keep yt-dlp current (sites change often): at most once a day, never blocking the launch on failure.
LAST="$(cat "$VENV/.ytdlp" 2>/dev/null || echo 0)"
if [ $(( $(date +%s) - LAST )) -gt 86400 ]; then
  "$UV" pip install --python "$VENV/bin/python" --upgrade yt-dlp && date +%s > "$VENV/.ytdlp"
fi

# 4. Optional TikTok credentials (see TIKTOK.md).
if [ -f "$SUPPORT/.env" ]; then
  set -a
  . "$SUPPORT/.env"
  set +a
fi

cd "$RESOURCES" || fail "Les fichiers de l’application sont introuvables. Retélécharge Split Studio."
# exec keeps the process macOS launched, so the Dock shows a single Split Studio icon.
exec "$VENV/bin/python" desktop.py
