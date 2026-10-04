#!/bin/bash
# Script d'installation de Moufloster sur Proxmox
# À lancer une fois après le clone du repo

echo "🎬 Installation de Moufloster"
echo "=============================="

REPO_DIR="/opt/moufloster"
SERVICE_FILE="$REPO_DIR/moufloster.service"
DEPLOY_SCRIPT="$REPO_DIR/deploy.sh"
CRON_SCRIPT="$REPO_DIR/setup-cronjob.sh"

# 1. Vérifier que le répertoire existe
if [ ! -d "$REPO_DIR" ]; then
    echo "❌ Erreur : $REPO_DIR introuvable"
    exit 1
fi

# 2. Rendre les scripts exécutables
echo "📝 Permissions des scripts..."
chmod +x "$DEPLOY_SCRIPT" "$CRON_SCRIPT"

# 3. Copier le service systemd
echo "📋 Installation du service systemd..."
cp "$SERVICE_FILE" /etc/systemd/system/moufloster.service

# 4. Recharger systemd
echo "🔄 Rechargement de systemd..."
systemctl daemon-reload

# 5. Activer le service
echo "⚙️  Activation du service..."
systemctl enable moufloster

# 6. Démarrer le service
echo "🚀 Démarrage du service..."
systemctl start moufloster

# 7. Vérifier le statut
echo ""
echo "📊 Statut du service :"
systemctl status moufloster

# 8. Installer la cronjob de mise à jour automatique (idempotent)
echo ""
echo "⏱️  Configuration de la mise à jour automatique (cronjob)..."
bash "$CRON_SCRIPT"

echo ""
echo "✅ Installation terminée !"
echo ""
echo "🌐 Accès : http://localhost:8000"
echo "📋 Logs app : journalctl -u moufloster -f"
echo "📋 Logs déploiement : tail -f /var/log/moufloster-cron.log"
echo ""
echo "Les mises à jour sont automatiques : chaque minute, le serveur vérifie"
echo "GitHub et redéploie seul s'il y a du nouveau. Rien d'autre à faire."
