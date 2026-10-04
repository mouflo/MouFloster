#!/bin/bash
# Installe LaMa (effacement de qualité, sur le processeur). Lancé AUTOMATIQUEMENT par deploy.sh, une seule fois.
# Rien à faire à la main. Journal: data/lama-install.log
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/venv/bin/python"
MODEL_DIR="$DIR/data/models"
MODEL="$MODEL_DIR/big-lama.pt"
MODEL_URL="https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
TRIES="$DIR/data/.lama-tries"

mkdir -p "$MODEL_DIR"
echo "[$(date +'%F %T')] === Installation de LaMa ==="

fail() {
    echo "[$(date +'%F %T')] ❌ $1"
    touch "$DIR/data/.lama-failed"
    n=$(( $(cat "$TRIES" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$TRIES"
    if [ "$n" -ge 3 ]; then
        echo "[$(date +'%F %T')] Abandon après $n essais (l'appli garde le moteur léger OpenCV)."
        touch "$DIR/data/.lama-disabled"
    fi
    exit 1
}

[ -x "$PY" ] || fail "venv introuvable ($PY)"

# Contexte (utile pour comprendre un échec)
echo "Python: $("$PY" --version 2>&1) · archi: $(uname -m) · pip: $("$PY" -m pip --version 2>&1 | cut -d' ' -f1-2)"
echo "Mémoire: $(free -m | awk '/Mem:/ {print $7" Mo disponibles sur "$2" Mo"}') · Disque libre: $(df -h "$DIR" | awk 'NR==2 {print $4}')"

# Place disque: torch CPU (~0,7 Go installé) + modèle (~0,2 Go). Sans cache pip, 2 Go libres suffisent.
FREE_KB=$(df -k "$DIR" | awk 'NR==2 {print $4}')
[ "${FREE_KB:-0}" -gt 2000000 ] || fail "pas assez de place disque (il faut ~2 Go libres)"

# 1) torch version processeur uniquement (pas de pilotes graphiques: bien plus léger).
#    Les dépendances viennent du PyPI normal, et torch seul de l'index PyTorch (évite les erreurs « flit_core »).
if ! "$PY" -c "import torch" 2>/dev/null; then
    echo "📦 Installation des dépendances de torch..."
    "$PY" -m pip install --no-cache-dir --progress-bar off numpy filelock typing-extensions sympy networkx jinja2 fsspec 2>&1 | tail -n 8
    echo "📦 Installation de torch (processeur, ~200 Mo)..."
    "$PY" -m pip install --no-cache-dir --progress-bar off --no-deps torch --index-url https://download.pytorch.org/whl/cpu 2>&1 | tail -n 12
    if ! "$PY" -c "import torch" 2>/dev/null; then
        echo "↪️  2e méthode (index supplémentaire)..."
        "$PY" -m pip install --no-cache-dir --progress-bar off torch --extra-index-url https://download.pytorch.org/whl/cpu 2>&1 | tail -n 12
    fi
    "$PY" -c "import torch; print('torch', torch.__version__, 'OK')" 2>&1 || fail "installation de torch impossible (voir les lignes ci-dessus)"
fi

# 2) le modèle (téléchargé sous un nom temporaire puis renommé: jamais de fichier à moitié téléchargé)
if [ ! -s "$MODEL" ]; then
    echo "⬇️  Téléchargement du modèle LaMa (~200 Mo)..."
    # Téléchargement avec Python (curl n'est pas installé partout), 3 essais
    DL_OK=0
    for i in 1 2 3; do
        if "$PY" - "$MODEL_URL" "$MODEL.part" <<'PYEOF'
import shutil, sys, urllib.request
url, dst = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={"User-Agent": "moufloster"})
with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
    shutil.copyfileobj(r, f, 1024 * 1024)
PYEOF
        then DL_OK=1; break; fi
        echo "↪️  essai $i échoué, nouvelle tentative..."; sleep 5
    done
    [ "$DL_OK" = 1 ] || { rm -f "$MODEL.part"; fail "téléchargement du modèle impossible (voir l'erreur ci-dessus)"; }
    SIZE=$(stat -c %s "$MODEL.part")
    [ "$SIZE" -gt 150000000 ] || { rm -f "$MODEL.part"; fail "modèle incomplet ($SIZE octets)"; }
    mv "$MODEL.part" "$MODEL"
fi

# 3) vérification: le modèle se charge et fait un vrai calcul
"$PY" - <<PYEOF || fail "le modèle ne fonctionne pas"
import torch
m = torch.jit.load("$MODEL", map_location="cpu").eval()
with torch.inference_mode():
    out = m(torch.rand(1, 3, 64, 64), torch.zeros(1, 1, 64, 64))
assert out.shape[-2:] == (64, 64)
print("modèle OK", tuple(out.shape))
PYEOF

touch "$DIR/data/.lama-ok"
rm -f "$DIR/data/.lama-failed" "$TRIES"
echo "[$(date +'%F %T')] ✅ LaMa installé. Redémarrage de l'appli."
systemctl restart moufloster
