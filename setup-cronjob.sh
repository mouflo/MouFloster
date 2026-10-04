#!/bin/bash
# Script d'installation de la cronjob de déploiement
# À exécuter une seule fois sur le serveur

set -e

# Déterminer le répertoire du repo (peut être /opt/moufloster ou /root/moufloster)
if [ -d "/opt/moufloster" ]; then
    REPO_DIR="/opt/moufloster"
elif [ -d "/root/moufloster" ]; then
    REPO_DIR="/root/moufloster"
else
    echo "❌ Répertoire moufloster non trouvé!"
    exit 1
fi

DEPLOY_SCRIPT="$REPO_DIR/deploy.sh"
CRON_LOG="/var/log/moufloster-cron.log"

echo "🔧 Configuration de la cronjob de déploiement..."

# Créer le fichier de log
touch "$CRON_LOG"
chmod 666 "$CRON_LOG"

# Créer une entrée cronjob
# Format: minute heure jour mois jour_semaine commande
# */1 = toutes les minutes
CRON_ENTRY="*/1 * * * * bash $DEPLOY_SCRIPT >> $CRON_LOG 2>&1"

# Vérifier si la cronjob existe déjà
if crontab -l 2>/dev/null | grep -q "$DEPLOY_SCRIPT"; then
    echo "✅ La cronjob est déjà configurée"
    crontab -l | grep moufloster
else
    # Ajouter la cronjob
    (crontab -l 2>/dev/null || echo ""; echo "$CRON_ENTRY") | crontab -
    echo "✅ Cronjob ajoutée avec succès!"
    echo "   Exécution: Toutes les 1 minute(s)"
    echo "   Logs: $CRON_LOG"
    crontab -l | grep moufloster
fi

echo ""
echo "📋 Pour voir les logs en temps réel:"
echo "   tail -f $CRON_LOG"
echo ""
echo "❌ Pour supprimer la cronjob:"
echo "   crontab -e  (puis supprimer la ligne moufloster)"
