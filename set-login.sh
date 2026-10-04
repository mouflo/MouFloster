#!/bin/bash
# Définit l'identifiant et le mot de passe de Moufloster (demandés ici, donc pas dans l'historique du terminal).
# Usage:  bash /opt/moufloster/set-login.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="python3"

read -r -p "Identifiant : " MF_USER
read -r -s -p "Mot de passe (8 caractères minimum) : " MF_PASS; echo
read -r -s -p "Confirme le mot de passe : " MF_PASS2; echo
if [ "$MF_PASS" != "$MF_PASS2" ]; then
    echo "❌ Les deux mots de passe sont différents, rien n'a été changé."
    exit 1
fi

export MF_USER MF_PASS
"$PY" "$DIR/auth.py" --set-login
unset MF_PASS MF_PASS2
systemctl restart moufloster && echo "✅ Appli redémarrée : tu peux te connecter."
