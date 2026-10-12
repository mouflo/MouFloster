#!/bin/bash
# Mise à jour automatique depuis GitHub : AJOUTE une ligne à la crontab (les autres lignes ne sont jamais touchées).
# Usage : bash setup-cronjob.sh [nuit|heure|minute|jamais]   (nuit = chaque nuit à 5 h, par défaut)
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_SCRIPT="$REPO_DIR/deploy.sh"
CRON_LOG="/var/log/moufloster-cron.log"
case "${1:-nuit}" in
    minute) QUAND="* * * * *" ;;
    heure)  QUAND="0 * * * *" ;;
    nuit)   QUAND="0 5 * * *" ;;
    jamais) echo "⏸️  Pas de mise à jour automatique : lance « bash $DEPLOY_SCRIPT » quand tu veux."; exit 0 ;;
    *) echo "Usage : bash setup-cronjob.sh [nuit|heure|minute|jamais]"; exit 1 ;;
esac
chmod +x "$DEPLOY_SCRIPT"
touch "$CRON_LOG"
if crontab -l 2>/dev/null | grep -q "$DEPLOY_SCRIPT"; then
    echo "✅ Une mise à jour automatique existe déjà (crontab -e pour changer sa fréquence) :"
    crontab -l | grep "$DEPLOY_SCRIPT"
else
    (crontab -l 2>/dev/null; echo "$QUAND bash $DEPLOY_SCRIPT >> $CRON_LOG 2>&1") | crontab -
    echo "✅ Mise à jour automatique ajoutée (${1:-nuit}) : $QUAND"
fi
