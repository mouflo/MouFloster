#!/bin/bash
# Script de déploiement automatique pour Moufloster
# Utilisable via webhook GitHub ou cronjob

# Déterminer le répertoire du repo
if [ -d "/opt/moufloster" ]; then
    REPO_DIR="/opt/moufloster"
elif [ -d "/root/moufloster" ]; then
    REPO_DIR="/root/moufloster"
else
    echo "❌ Répertoire moufloster non trouvé!"
    exit 1
fi

LOG_FILE="/var/log/moufloster-deploy.log"
SERVICE_NAME="moufloster"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Créer le fichier de log s'il n'existe pas
touch "$LOG_FILE"


# Installation automatique de LaMa (effacement de qualité): une seule fois, en arrière-plan, sans rien demander
maybe_install_lama() {
    [ -f "$REPO_DIR/install-lama.sh" ] || return 0
    [ -f "$REPO_DIR/data/.lama-ok" ] && return 0
    if [ -f "$REPO_DIR/data/.lama-disabled" ]; then
        [ "$REPO_DIR/install-lama.sh" -nt "$REPO_DIR/data/.lama-disabled" ] || return 0
    fi
    # après un échec: nouvel essai tout de suite si le script d'installation a été corrigé depuis, sinon au bout de 6 h
    if [ -f "$REPO_DIR/data/.lama-failed" ]; then
        if [ "$REPO_DIR/install-lama.sh" -nt "$REPO_DIR/data/.lama-failed" ]; then
            rm -f "$REPO_DIR/data/.lama-failed" "$REPO_DIR/data/.lama-tries" "$REPO_DIR/data/.lama-disabled"
        else
            AGE=$(( $(date +%s) - $(stat -c %Y "$REPO_DIR/data/.lama-failed") ))
            [ "$AGE" -ge 21600 ] || return 0
        fi
    fi
    mkdir -p "$REPO_DIR/data"
    log "🧠 Installation de LaMa lancée en arrière-plan (journal: data/lama-install.log)"
    nohup flock -n /tmp/moufloster-lama.lock bash "$REPO_DIR/install-lama.sh" >> "$REPO_DIR/data/lama-install.log" 2>&1 &
}

# Dossier des posters (créations + anciens posters): /mnt/mouflosyno/MouFloster. Une seule fois: corrige le service
# installé (qui avait /mnt/data/posters), crée le dossier et COPIE (sans rien supprimer) ce qui existait déjà.
migrate_output_dir() {
    local NEW="/mnt/mouflosyno/MouFloster" OLD="/mnt/data/posters" UNIT="/etc/systemd/system/moufloster.service"
    [ -f "$REPO_DIR/data/.output-migrated" ] && return 0
    [ -f "$UNIT" ] || return 0
    grep -q "OUTPUT_DIR=$OLD" "$UNIT" || { mkdir -p "$REPO_DIR/data"; touch "$REPO_DIR/data/.output-migrated"; return 0; }
    if ! mkdir -p "$NEW" 2>/dev/null || [ ! -w "$NEW" ]; then
        log "⚠️ $NEW inaccessible (partage monté ?): dossier des posters inchangé, nouvel essai à la prochaine vérification"
        return 0
    fi
    if [ -d "$OLD" ]; then
        log "📦 Copie des posters existants de $OLD vers $NEW (sans rien supprimer)..."
        cp -an "$OLD"/. "$NEW"/ 2>&1 | tee -a "$LOG_FILE"
    fi
    sed -i "s#OUTPUT_DIR=$OLD#OUTPUT_DIR=$NEW#" "$UNIT"
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME"
    mkdir -p "$REPO_DIR/data"; touch "$REPO_DIR/data/.output-migrated"
    log "✅ Les posters sont maintenant enregistrés dans $NEW"
}

log "🔍 Vérification des mises à jour..."
migrate_output_dir

# Aller dans le répertoire du repo
cd "$REPO_DIR"

# Sauvegarder le commit actuel
OLD_COMMIT=$(git rev-parse HEAD)

# Récupérer les dernières modifications
git fetch origin -q

# Vérifier s'il y a des changements
NEW_COMMIT=$(git rev-parse origin/main 2>/dev/null || git rev-parse origin/master)

if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
    log "⏭️  Aucune mise à jour disponible"
    maybe_install_lama
    exit 0
fi

log "=== 🚀 Déploiement Moufloster détecté ==="
log "Ancien commit: $OLD_COMMIT"
log "Nouveau commit: $NEW_COMMIT"

# Réinitialiser au dernier commit distant
log "📥 Téléchargement des changements..."
git reset --hard origin/main || git reset --hard origin/master

# Installer les dépendances (au cas où requirements.txt a changé)
if git diff $OLD_COMMIT HEAD -- requirements.txt | grep -q .; then
    log "📦 Installation des dépendances (requirements.txt modifié)..."
    if [ -d "./venv" ]; then
        ./venv/bin/python -m pip install -q -r requirements.txt 2>&1 | tee -a "$LOG_FILE"
    else
        log "⚠️  Dossier venv non trouvé - passer l'installation des dépendances"
    fi
fi

# Redémarrer le service
log "🔄 Redémarrage du service..."
if systemctl is-active --quiet $SERVICE_NAME; then
    systemctl restart $SERVICE_NAME
    sleep 2
    if systemctl is-active --quiet $SERVICE_NAME; then
        log "✅ Déploiement réussi ! Service redémarré."
    else
        log "❌ Erreur : Le service n'a pas pu redémarrer."
        exit 1
    fi
else
    log "⚠️  Service n'était pas actif, tentative de démarrage..."
    systemctl start $SERVICE_NAME
    sleep 2
fi

maybe_install_lama
log "=== Fin du déploiement Moufloster ==="
