#!/bin/bash
# =====================================================================
#  Installation / mise à jour de Bot GlobalExam sur Mac — UNE commande :
#
#    curl -fsSL https://raw.githubusercontent.com/tomascdj0903/bot-globalexam/main/install_mac.sh | bash
#
#  • Télécharge l'app pour ton Mac (Apple Silicon ou Intel)
#  • L'installe dans « Applications » (ton dossier perso, pas besoin
#    de mot de passe) et met un raccourci sur le Bureau
#  • Pas de blocage « développeur non identifié » (téléchargée via curl)
#  • Relancer la même commande = mettre l'app à jour.
#  Le bot lui-même se met à jour TOUT SEUL à chaque lancement.
# =====================================================================
set -e
REPO="tomascdj0903/bot-globalexam"
case "$(uname -m)" in
  arm64) ARCH="arm64" ;;
  *)     ARCH="x86_64" ;;
esac
URL="https://github.com/$REPO/releases/download/mac-latest/BotGlobalExam-mac-$ARCH.zip"
DEST="$HOME/Applications"
TMP="$(mktemp -d)"

echo "🤖 Bot GlobalExam — installation ($ARCH)…"
if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "⚠️  Google Chrome n'est pas installé : installe-le d'abord sur"
  echo "    https://www.google.com/chrome/ puis relance cette commande."
  exit 1
fi

echo "⬇️  Téléchargement…"
curl -fL --progress-bar "$URL" -o "$TMP/app.zip"
mkdir -p "$DEST"
rm -rf "$DEST/BotGlobalExam.app"
ditto -x -k "$TMP/app.zip" "$DEST"
xattr -cr "$DEST/BotGlobalExam.app" 2>/dev/null || true
rm -rf "$TMP"

ln -sfn "$DEST/BotGlobalExam.app" "$HOME/Desktop/Bot GlobalExam" 2>/dev/null || true

echo "✅ Installé : $DEST/BotGlobalExam.app (raccourci sur le Bureau)"
echo "🚀 Lancement…"
open "$DEST/BotGlobalExam.app"
