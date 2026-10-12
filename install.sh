#!/bin/bash
# Installation de MouFloster sur un serveur Debian/Ubuntu (conteneur LXC Proxmox ou autre), à lancer en root
# une fois après le clone du dépôt dans /opt/moufloster :   bash /opt/moufloster/install.sh
set -e

echo "🎬 Installation de MouFloster"
echo "=============================="

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

# 1. Paquets du système (python3-venv manque souvent sur une Debian neuve)
echo "📦 Paquets du système…"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl cron >/dev/null
echo "   ✅ python3-venv, pip, git, curl, cron"

# 2. Environnement Python de l'appli
if [ ! -x venv/bin/python ]; then
    echo "🐍 Création de l'environnement Python…"
    python3 -m venv venv
fi
echo "📥 Dépendances Python (quelques minutes la première fois)…"
venv/bin/python -m pip install -q --upgrade pip
venv/bin/python -m pip install -q -r requirements.txt
mkdir -p data && chmod 700 data

# 3. Identifiant (facultatif ici : il peut aussi se créer à la première ouverture de la page, depuis le réseau local)
if ! grep -q "^APP_PASSWORD_HASH=" data/secrets.env 2>/dev/null; then
    read -r -p "🔐 Créer l'identifiant de connexion maintenant ? (o/N) " rep || rep=""
    if [ "$rep" = "o" ] || [ "$rep" = "O" ]; then
        bash set-login.sh
    else
        echo "   → Tu le créeras à la première ouverture de la page (depuis ton réseau local)."
    fi
fi

# 4. Service systemd (démarrage automatique)
echo "📋 Service systemd…"
sed "s#/opt/moufloster#$REPO_DIR#g" moufloster.service > /etc/systemd/system/moufloster.service
systemctl daemon-reload
systemctl enable -q moufloster
systemctl restart moufloster

# 5. Mises à jour automatiques depuis GitHub (une ligne AJOUTÉE à la crontab)
echo ""
echo "⏱️  Mises à jour automatiques depuis GitHub :"
echo "   1) chaque nuit à 5 h (conseillé)   2) chaque heure   3) chaque minute   4) jamais (à la main)"
read -r -p "   Ton choix [1] : " freq || freq=""
case "$freq" in
    2) f=heure ;;
    3) f=minute ;;
    4) f=jamais ;;
    *) f=nuit ;;
esac
bash setup-cronjob.sh "$f"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "✅ Installation terminée !"
echo "🌐 Ouvre http://${IP:-localhost}:8000 depuis ton réseau local."
echo "   Puis ⚙️ Réglages : clé TheMovieDB, dossier de ta médiathèque (Dossiers), adresse et clé d'Emby (Connexions)."
echo "📋 Journal de l'appli : journalctl -u moufloster -f"
