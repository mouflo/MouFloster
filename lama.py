"""
Effacement de qualité (modèle LaMa, sur le processeur): remplit la zone effacée en recréant textures et motifs,
comme le « remplissage d'après le contenu » de Photoshop.

Optionnel: installé automatiquement sur le serveur par install-lama.sh (lancé par deploy.sh).
Tant que ce n'est pas installé, l'appli utilise le moteur léger (OpenCV): rien ne casse.
"""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

MODEL_PATH = Path(os.getenv("LAMA_MODEL", str(Path(__file__).parent / "data" / "models" / "big-lama.pt")))
LAMA_TIMEOUT = 300  # secondes

_lock = threading.Lock()
_model = None
_torch = None
_check = {"t": 0.0, "ok": False, "reason": "pas encore vérifié"}


def check(force=False):
    """
    LaMa utilisable ? Retourne (ok, raison). Le « non » n'est gardé que 60 s: dès que l'installation
    automatique est terminée, l'appli le voit sans redémarrage.
    """
    now = time.time()
    if not force and (_check["ok"] or now - _check["t"] < 60) and _check["t"]:
        return _check["ok"], _check["reason"]
    ok, reason = False, ""
    if os.getenv("INPAINT_ENGINE", "").lower() == "opencv":
        reason = "désactivé par INPAINT_ENGINE=opencv"
    elif not MODEL_PATH.is_file():
        reason = f"fichier du modèle absent ({MODEL_PATH})"
    else:
        try:
            import torch  # noqa: F401
            ok, reason = True, f"prêt (torch {torch.__version__}, modèle {MODEL_PATH.stat().st_size // 1048576} Mo)"
        except Exception as e:
            reason = f"torch inutilisable: {type(e).__name__}: {e}"
    _check.update(t=now, ok=ok, reason=reason)
    return ok, reason


def available():
    return check()[0]


def unavailable_reason():
    return check()[1]


def _load():
    global _model, _torch
    if _model is None:
        import torch
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        _model = torch.jit.load(str(MODEL_PATH), map_location="cpu").eval()
        _torch = torch
    return _model


def _run(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """rgb: HxWx3 uint8, mask: HxW uint8 (255 = à remplir) → HxWx3 uint8"""
    model = _load()
    torch = _torch
    h, w = mask.shape
    ph, pw = (-h) % 8, (-w) % 8  # le modèle veut des côtés multiples de 8
    if ph or pw:
        rgb = np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="symmetric")
        mask = np.pad(mask, ((0, ph), (0, pw)), mode="symmetric")
    img_t = torch.from_numpy(rgb).permute(2, 0, 1).float().div(255.0).unsqueeze(0)
    mask_t = torch.from_numpy((mask > 127).astype("float32")).unsqueeze(0).unsqueeze(0)
    with _lock, torch.inference_mode():
        out = model(img_t, mask_t)
    out = out[0].permute(1, 2, 0).cpu().numpy()
    if out.max() <= 1.5:  # selon les versions du modèle, la sortie est en 0-1 ou en 0-255
        out = out * 255.0
    out = np.clip(out, 0, 255).astype("uint8")
    return out[:h, :w]


def _mem_available_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 4096


def max_side():
    """Taille max (pixels) de la zone envoyée au modèle: plus la mémoire libre est faible, plus on la réduit"""
    mem = _mem_available_mb()
    if mem >= 3500:
        return 800
    if mem >= 2500:
        return 640
    return 512


def _prefer_oom_victim():
    """Si la mémoire vient à manquer, c'est CE calcul qui doit être arrêté en premier, pas l'appli web"""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def _run_isolated(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    Lance LaMa dans un processus séparé: s'il manque de mémoire, seul ce processus est arrêté
    (l'appli reste en vie et retombe sur le moteur léger), et la mémoire est rendue juste après.
    """
    with tempfile.TemporaryDirectory(prefix="lama_") as tmp:
        src, dst = os.path.join(tmp, "in.npz"), os.path.join(tmp, "out.npy")
        np.savez(src, rgb=rgb, mask=mask)
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", src, dst],
                capture_output=True, text=True, timeout=LAMA_TIMEOUT, preexec_fn=_prefer_oom_victim,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LaMa trop lent (plus de {LAMA_TIMEOUT} s), arrêté")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            hint = " (arrêt brutal: manque de mémoire probable, mémoire libre " + str(_mem_available_mb()) + " Mo)" if proc.returncode in (-9, 137) else ""
            raise RuntimeError(f"processus LaMa terminé avec le code {proc.returncode}{hint}: {' | '.join(tail)}")
        logger.info(f"LaMa: calcul en {time.time() - t0:.1f} s (mémoire libre avant: {_mem_available_mb()} Mo)")
        return np.load(dst)


def inpaint(img: Image.Image, mask: np.ndarray) -> Image.Image:
    """
    img: image RGB pleine résolution; mask: HxW uint8 (255 = zone à effacer, déjà un peu élargie).
    Seule la zone autour du masque est traitée; le reste de l'image n'est pas touché (même résolution, mêmes pixels).
    """
    arr = np.array(img.convert("RGB"))
    h, w = mask.shape
    ys, xs = np.where(mask > 0)
    bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    margin = max(64, int(0.5 * max(bw, bh)))  # du contexte autour: c'est lui qui sert de modèle au remplissage
    x0, x1 = max(0, xs.min() - margin), min(w, xs.max() + margin + 1)
    y0, y1 = max(0, ys.min() - margin), min(h, ys.max() + margin + 1)

    crop = arr[y0:y1, x0:x1]
    mcrop = mask[y0:y1, x0:x1]
    ch, cw = mcrop.shape

    side = max_side()
    scale = min(1.0, side / max(ch, cw))
    if scale < 1.0:
        small_size = (max(8, round(cw * scale)), max(8, round(ch * scale)))
        small = np.array(Image.fromarray(crop).resize(small_size, Image.Resampling.LANCZOS))
        msmall = np.array(Image.fromarray(mcrop).resize(small_size, Image.Resampling.NEAREST))
        filled_small = _run_isolated(small, msmall)
        filled = np.array(Image.fromarray(filled_small).resize((cw, ch), Image.Resampling.LANCZOS))
    else:
        filled = _run_isolated(crop, mcrop)

    # On ne remplace que la zone effacée (bord très légèrement adouci pour une jonction invisible)
    soft = np.array(Image.fromarray(mcrop).filter(ImageFilter.GaussianBlur(2))).astype("float32") / 255.0
    soft = soft[..., None]
    blended = (crop.astype("float32") * (1 - soft) + filled.astype("float32") * soft).round().astype("uint8")
    arr[y0:y1, x0:x1] = blended
    return Image.fromarray(arr)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    # Processus de calcul isolé (lancé par _run_isolated)
    data = np.load(sys.argv[2])
    if os.getenv("MOUFLOSTER_LAMA_FAKE") == "1":  # mode test: pas de modèle, remplit en vert
        out = data["rgb"].copy()
        out[data["mask"] > 127] = (0, 255, 0)
    else:
        out = _run(data["rgb"], data["mask"])
    np.save(sys.argv[3], out)
