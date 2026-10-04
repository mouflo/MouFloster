# 🚀 Installation & Déploiement Moufloster

Guide complet pour installer Moufloster sur un LXC Proxmox avec démarrage auto
et mises à jour automatiques. **Ce fichier est la référence : si le serveur
disparaît un jour, tout ce qu'il faut pour tout reconstruire à l'identique est
ici, dans ce dépôt.**

Convention utilisée pour **toutes** les applications de ce compte GitHub
(MouFlanimexer, Moufloster, et les suivantes) : un dossier sous `/opt/<nom>`,
un service systemd du même nom, et une mise à jour automatique par **cronjob**
(pas de webhook — voir "Pourquoi pas un webhook ?" plus bas).

## 📋 Prérequis

- LXC Debian/Ubuntu sur Proxmox
- Python 3.8+
- Git configuré (le dépôt doit pouvoir faire `git pull` sans mot de passe —
  clé SSH ou token déjà en place)
- Accès root (pas besoin de sudo, on est déjà root)

## ⚡ Installation complète (sur une machine neuve)

```bash
# 1. Clone le repo au bon endroit
cd /opt
git clone https://github.com/mouflo/MouFloster.git moufloster
cd moufloster

# 2. Crée l'environnement virtuel et installe les dépendances
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate

# 3. Configure les clés API (jamais sur GitHub, stockées localement)
bash set-secret.sh TMDB_API_KEY "ta-clé-tmdb"
bash set-secret.sh EMBY_API_KEY "ta-clé-emby"   # optionnel

# 4. Installe le service + la mise à jour automatique
chmod +x install.sh
bash install.sh
```

**C'est tout.** Le service démarre, se relance seul au boot, et se met à jour
seul dès qu'un nouveau commit arrive sur `main`/`master` — sans aucune autre
manipulation.

## 📊 Vérifier le statut

```bash
systemctl status moufloster          # l'appli tourne ?
journalctl -u moufloster -f          # logs en direct de l'appli
tail -f /var/log/moufloster-deploy.log  # logs des déploiements
tail -f /var/log/moufloster-cron.log    # logs bruts de la cronjob (1x/minute)
```

## 🌐 Accès

- **App** : `http://<ip-du-serveur>:8000`

## 🤖 Mises à jour automatiques (cronjob)

C'est `setup-cronjob.sh` (déjà lancé par `install.sh`) qui met ça en place.
Il ajoute cette ligne à la crontab root — **en l'ajoutant aux lignes déjà
présentes, jamais en les remplaçant** :

```
*/1 * * * * bash /opt/moufloster/deploy.sh >> /var/log/moufloster-cron.log 2>&1
```

Chaque minute, `deploy.sh` :
1. Compare le commit local au commit distant sur GitHub (`git fetch`)
2. S'il n'y a rien de neuf → ne fait rien
3. S'il y a du neuf → `git reset --hard` sur le dernier commit, réinstalle les
   dépendances si `requirements.txt` a changé, puis redémarre le service

Pour réinstaller cette cronjob à la main (normalement jamais nécessaire,
`install.sh` le fait déjà) :
```bash
bash /opt/moufloster/setup-cronjob.sh
```

### Pourquoi pas un webhook GitHub ?

Moufloster utilisait auparavant un webhook (GitHub appelle directement le
serveur à chaque push). Ça a été abandonné pour rester cohérent avec les
autres applications : un webhook demande d'exposer le serveur sur internet
(ouverture de port, nom de domaine) et un secret à configurer côté GitHub —
une pièce de plus qui peut tomber en panne sans prévenir. Le cronjob, lui,
tourne entièrement en local, ne nécessite rien d'ouvert vers l'extérieur, et
c'est la même méthode, au mot pour mot, pour chaque application de ce
compte. Le seul coût : jusqu'à 1 minute de délai avant qu'un correctif
arrive, ce qui n'a aucune importance en pratique.

## 🔧 Configuration avancée

### Clés API / secrets

Jamais dans le code ni sur GitHub — toujours via :
```bash
bash /opt/moufloster/set-secret.sh NOM_DE_LA_CLE "valeur"
```
Stockées dans `data/secrets.env` (ignoré par git), rechargées au redémarrage
du service que `set-secret.sh` déclenche automatiquement.

### Variables d'environnement non-secrètes

Dans `/etc/systemd/system/moufloster.service` (copié depuis
`moufloster.service` du dépôt — toute modification doit être répercutée dans
les deux, sinon elle sera perdue au prochain `install.sh` sur une nouvelle
machine) :
```ini
[Service]
Environment="OUTPUT_DIR=/mnt/mouflosyno/MouFloster"
Environment="MEDIATHEQUE_DIR=/mnt/media"
```
Puis : `systemctl daemon-reload && systemctl restart moufloster`

### Changer le port

Modifie `app.py` (`app.run(..., port=8080)`), puis redémarre le service.

## 🐛 Dépannage

**L'app ne démarre pas ?**
```bash
journalctl -u moufloster -e
cd /opt/moufloster && source venv/bin/activate && python app.py   # voir l'erreur en direct
```

**Port déjà utilisé ?**
```bash
lsof -i :8000
kill -9 <PID>
systemctl restart moufloster
```

**Les mises à jour ne se font pas ?**
```bash
bash /opt/moufloster/deploy.sh      # test manuel, affiche l'erreur
crontab -l                           # la ligne moufloster est bien là ?
cat /var/log/moufloster-deploy.log   # historique des déploiements
```

## 📝 Fichiers importants (tous dans ce dépôt)

```
/opt/moufloster/
├── app.py                 # Application principale
├── requirements.txt       # Dépendances
├── moufloster.service     # Service systemd — SOURCE DE VÉRITÉ, à copier dans /etc/systemd/system/
├── deploy.sh              # Script de déploiement (lancé par cron chaque minute)
├── setup-cronjob.sh       # Installe la ligne cron ci-dessus (idempotent)
├── install.sh             # Installation complète en une commande sur machine neuve
├── set-secret.sh          # Enregistre une clé API localement + redémarre l'appli
└── venv/                  # Environnement virtuel (recréé par l'installation, pas versionné)
```

## 🔄 Logs

```bash
journalctl -u moufloster -f            # app
tail -f /var/log/moufloster.log        # app (fichier)
tail -f /var/log/moufloster-deploy.log # déploiements
tail -f /var/log/moufloster-cron.log   # cronjob brute
```

## 🎯 Intégration avec MouFlanimexer

Les deux apps tournent sur le même LXC, même convention de déploiement :
- **MouFlanimexer** : `/opt/mouflanimexer`, service `mouflanimexer`, port 5000
- **Moufloster** : `/opt/moufloster`, service `moufloster`, port 8000

Voir `INSTALL.md` du dépôt MouFlanimexer pour le détail de cette app.

---

**Questions ?** Lance l'une des commandes de diagnostic ci-dessus et partage
les logs avec Claude.
