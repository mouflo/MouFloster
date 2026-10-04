"""
Connexion à Moufloster: vraie page de connexion (formulaire HTML), pas de fenêtre popup du navigateur.
Les gestionnaires de mots de passe (Bitwarden, etc.) la reconnaissent et la remplissent normalement.

Identifiant et mot de passe se définissent sur le serveur avec:   bash /opt/moufloster/set-login.sh
(stockés dans data/secrets.env: seul un hash du mot de passe est conservé, jamais le mot de passe lui-même)
"""

import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from flask import Response, jsonify, redirect, render_template_string, request, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).parent
SECRETS_FILE = BASE_DIR / "data" / "secrets.env"
KEY_FILE = BASE_DIR / "data" / "session_key"

REMEMBER_DAYS = 30
MAX_FAILS_IP = 5          # essais ratés par adresse avant blocage
MAX_FAILS_GLOBAL = 30     # essais ratés au total avant blocage
WINDOW = 600              # fenêtre de comptage: 10 minutes

_fails = {}               # ip -> [horodatages]
_fails_all = []
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

def _user():
    return os.getenv("APP_USER", "").strip()


def _hash():
    return os.getenv("APP_PASSWORD_HASH", "").strip()


def configured():
    return bool(_user() and _hash())


def _fingerprint():
    """Change quand le mot de passe change: toutes les anciennes sessions deviennent invalides"""
    return hashlib.sha256(_hash().encode()).hexdigest()[:16]


def _secret_key():
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env
    try:
        if KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
            if key:
                return key
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        KEY_FILE.write_text(key)
        os.chmod(KEY_FILE, 0o600)
        return key
    except OSError:
        return secrets.token_hex(32)  # sessions perdues au redémarrage, mais jamais de clé prévisible


# ---------------------------------------------------------------------------
# Limitation des essais (anti force brute)
# ---------------------------------------------------------------------------

def _recent(stamps, now):
    return [t for t in stamps if now - t < WINDOW]


def _blocked(ip):
    now = time.time()
    with _lock:
        _fails[ip] = _recent(_fails.get(ip, []), now)
        _fails_all[:] = _recent(_fails_all, now)
        return len(_fails[ip]) >= MAX_FAILS_IP or len(_fails_all) >= MAX_FAILS_GLOBAL


def _register_fail(ip):
    now = time.time()
    with _lock:
        _fails.setdefault(ip, []).append(now)
        _fails_all.append(now)


def _clear_fails(ip):
    with _lock:
        _fails.pop(ip, None)


# ---------------------------------------------------------------------------
# Page de connexion
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Connexion · MouFloster</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    :root {
        --bg: #101010; --card: #1c1c1c; --field: #2a2a2a; --line: #3a3a3a;
        --text: #ffffff; --muted: #a0a0a0; --accent: #52b54b; --accent-hover: #63c75c; --err: #e5534b;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body {
        background: var(--bg); color: var(--text);
        font-family: "Noto Sans", "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif;
        display: flex; align-items: center; justify-content: center; padding: 16px; min-height: 100vh;
    }
    .box { width: 100%; max-width: 380px; background: var(--card); border-radius: 8px; padding: 32px 28px; box-shadow: 0 8px 30px rgba(0,0,0,.5); }
    h1 { margin: 0 0 4px; font-size: 1.6rem; font-weight: 600; text-align: center; }
    h1 span { color: var(--accent); }
    .sub { text-align: center; color: var(--muted); font-size: .9rem; margin-bottom: 24px; }
    label { display: block; font-size: .85rem; color: var(--muted); margin: 14px 0 6px; }
    input[type=text], input[type=password] {
        width: 100%; padding: 12px; font: inherit; color: var(--text); background: var(--field);
        border: 1px solid var(--line); border-radius: 4px; outline: none;
    }
    input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
    .remember { display: flex; align-items: center; gap: 8px; margin: 16px 0 4px; font-size: .9rem; color: var(--muted); }
    .remember input { width: 18px; height: 18px; accent-color: var(--accent); }
    button {
        width: 100%; margin-top: 20px; padding: 12px; font: inherit; font-weight: 600; color: #fff;
        background: var(--accent); border: 0; border-radius: 4px; cursor: pointer;
    }
    button:hover { background: var(--accent-hover); }
    .error { background: rgba(229,83,75,.15); border: 1px solid var(--err); color: #ffb4ae; padding: 10px 12px; border-radius: 4px; font-size: .9rem; margin-bottom: 8px; }
    .setup { font-size: .9rem; line-height: 1.5; }
    .setup code { display: block; background: var(--field); padding: 10px; border-radius: 4px; margin: 10px 0; user-select: all; word-break: break-all; color: #cfe9cd; }
    .ver { text-align: center; color: #666; font-size: .75rem; margin-top: 22px; }
</style>
</head>
<body>
<main class="box">
    <h1>Mou<span>Floster</span></h1>
    <div class="sub">Générateur de posters</div>
    {% if not configured %}
        <div class="error">Aucun identifiant n'est encore défini sur ce serveur.</div>
        <div class="setup">
            Pour verrouiller l'accès, tape cette commande dans le terminal du serveur, puis reviens ici :
            <code>bash /opt/moufloster/set-login.sh</code>
            Elle te demandera un identifiant et un mot de passe.
        </div>
    {% else %}
        {% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}
        <form method="post" action="/login" autocomplete="on">
            <input type="hidden" name="next" value="{{ next }}">
            <label for="username">Identifiant</label>
            <input type="text" id="username" name="username" autocomplete="username" autocapitalize="off" autocorrect="off" spellcheck="false" required autofocus value="{{ username }}">
            <label for="password">Mot de passe</label>
            <input type="password" id="password" name="password" autocomplete="current-password" required>
            <div class="remember">
                <input type="checkbox" id="remember" name="remember" value="1" checked>
                <label for="remember" style="margin:0">Rester connecté {{ days }} jours</label>
            </div>
            <button type="submit">Se connecter</button>
        </form>
    {% endif %}
    <div class="ver">{{ version }}</div>
</main>
</body>
</html>
"""


def _safe_next(value):
    """On ne redirige que vers une page de CE site"""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value and not value.startswith("/login"):
        return value
    return "/"


def _page(error="", username="", status=200):
    html = render_template_string(
        LOGIN_HTML,
        configured=configured(),
        error=error,
        username=username,
        next=_safe_next(request.values.get("next", "")),
        days=REMEMBER_DAYS,
        version=os.getenv("_APP_VERSION", ""),
    )
    resp = Response(html, status=status, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Branchement sur l'appli Flask
# ---------------------------------------------------------------------------

class _Cookies(SecureCookieSessionInterface):
    """Cookie 'Secure' seulement si la connexion est en HTTPS (derrière le proxy) → marche aussi en http local"""

    def get_cookie_secure(self, app):
        return request.is_secure


def init_app(app, version=""):
    os.environ["_APP_VERSION"] = version
    app.secret_key = _secret_key()
    app.session_interface = _Cookies()
    app.config.update(
        SESSION_COOKIE_NAME="moufloster_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=REMEMBER_DAYS * 86400,
    )
    # Derrière Nginx Proxy Manager: vraie adresse du visiteur et HTTPS
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def is_logged_in():
        return bool(configured() and session.get("u") == _user() and hmac.compare_digest(session.get("f", ""), _fingerprint()))

    @app.before_request
    def require_login():
        if request.path in ("/login", "/healthz"):
            return None
        if is_logged_in():
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Session expirée : reconnecte-toi."}), 401
        target = request.full_path.rstrip("?") if request.method == "GET" else "/"
        return redirect("/login?next=" + _quote(target))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if is_logged_in():
                return redirect(_safe_next(request.args.get("next", "")))
            return _page()

        if not configured():
            return _page(status=503)
        ip = request.remote_addr or "?"
        if _blocked(ip):
            return _page("Trop d'essais ratés. Réessaie dans quelques minutes.", status=429)

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        # Les deux vérifications sont toujours faites (temps constant, on ne révèle pas lequel est faux)
        user_ok = hmac.compare_digest(username.encode(), _user().encode())
        pass_ok = check_password_hash(_hash(), password)
        if user_ok and pass_ok:
            _clear_fails(ip)
            session.clear()
            session["u"] = _user()
            session["f"] = _fingerprint()
            session.permanent = request.form.get("remember") == "1"
            return redirect(_safe_next(request.form.get("next", "")))

        _register_fail(ip)
        time.sleep(0.6)
        return _page("Identifiant ou mot de passe incorrect.", username=username, status=401)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/healthz")
    def healthz():
        return "ok"

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


def _quote(value):
    from urllib.parse import quote
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Outil en ligne de commande (utilisé par set-login.sh)
# ---------------------------------------------------------------------------

def _write_login(user, password):
    if not re.fullmatch(r"[A-Za-z0-9._@+-]{2,64}", user):
        raise SystemExit("❌ Identifiant: 2 à 64 caractères (lettres, chiffres, . _ @ + -)")
    if len(password) < 8:
        raise SystemExit("❌ Mot de passe trop court (8 caractères minimum)")
    hashed = generate_password_hash(password)

    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.touch()
    lines = [l for l in SECRETS_FILE.read_text().splitlines() if not l.startswith(("APP_USER=", "APP_PASSWORD_HASH="))]
    lines.append(f"APP_USER='{user}'")
    lines.append(f"APP_PASSWORD_HASH='{hashed}'")  # guillemets simples: le $ du hash reste tel quel
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_FILE)
    print(f"✅ Identifiant « {user} » enregistré (mot de passe haché)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--set-login":
        _write_login(os.environ.get("MF_USER", "").strip(), os.environ.get("MF_PASS", ""))
    else:
        print("Usage: bash set-login.sh")
