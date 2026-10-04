#!/bin/bash
# Enregistre une clé secrète sur CE serveur (jamais envoyée sur GitHub) puis redémarre l'appli.
# Usage:  bash set-secret.sh NOM_DE_LA_CLE "valeur"
#   ex.:  bash set-secret.sh EMBY_API_KEY "abcdef123456"
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
FILE="$DIR/data/secrets.env"
NAME="$1"
VALUE="$2"

if [ -z "$NAME" ] || [ -z "$VALUE" ]; then
    echo "Usage: bash set-secret.sh NOM_DE_LA_CLE \"valeur\""
    exit 1
fi
case "$NAME" in
    *[!A-Z0-9_]*) echo "❌ Nom invalide (majuscules, chiffres et _ uniquement)"; exit 1 ;;
esac

mkdir -p "$DIR/data"
touch "$FILE"
chmod 600 "$FILE"
grep -v "^${NAME}=" "$FILE" > "$FILE.tmp" || true
printf '%s="%s"\n' "$NAME" "$VALUE" >> "$FILE.tmp"
mv "$FILE.tmp" "$FILE"
chmod 600 "$FILE"

echo "✅ $NAME enregistré dans $FILE"
systemctl restart moufloster && echo "✅ Appli redémarrée"
