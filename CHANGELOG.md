# Journal des versions de MouFloster

La version affichée est « 1.0 » suivie du nombre de mises à jour depuis le passage en 1.0 (et de l'empreinte du commit).
L'historique d'avant ce fichier se lit dans les commits du dépôt.

## 26 octobre 2026
- 📱 **Au téléphone, plus de réglage changé par mégarde en faisant défiler la page** :
  - un appui qui arrive pendant que la page défile (le doigt posé pour l'arrêter) est ignoré : aucune case cochée, aucun bouton ni poster choisi sans le vouloir ; seul le texte d'une case est sensible au doigt, plus toute la ligne ;
  - les curseurs (épaisseur du cadre, gauche/droite, haut/bas, zoom) ne bougent que si on les glisse de côté, d'autant que le doigt a glissé (plus de saut à l'endroit touché) ; boutons − / + de part et d'autre pour régler finement ;
  - sur l'aperçu, un doigt fait défiler la page ; le recadrage au doigt se fait en mode « ✋ Recadrer au doigt » (ou par un appui long sur l'image), et deux doigts écartés ou rapprochés règlent le zoom. À la souris, rien ne change ;
  - un petit aperçu reste collé en bas de l'écran tant que le grand n'est pas visible : un appui y mène, « ↩ Retour aux réglages » ramène à l'endroit quitté.
- ⚡ **Aperçu plus léger et plus rapide** : envoyé en image JPEG (qualité 85, sans différence visible) au lieu de texte : environ deux fois moins de données à chaque retouche ; le dégradé noir est calculé une fois (aperçu en 37 ms au lieu de 63 ms). Plus de bulle « Aperçu généré » à chaque retouche.
- 🎵 **Générique cherché tout seul par MouFlopening** après l'envoi d'un poster (thème validé par la communauté), sans quitter la page ; s'il ne trouve rien, la fenêtre « Ouvrir MouFlopening » en donne la raison.
- 🆕 Les **nouveautés** disent si chaque titre a déjà son générique (« 🎵 générique ✓ »), ou proposent de l'ouvrir dans MouFlopening.
- 🖼️ **Ouverture depuis MouFlopening** (bouton « 🖼️ Poster dans MouFloster ») : titre et saison choisis tout seuls, bandeau et proposition de revenir une fois le poster envoyé.
- 🔗 **Applis voisines** (⚙️ Réglages → Connexions) : MouFloster et MouFlopening se parlent de serveur à serveur avec une clé créée toute seule (rien à régler sur un même serveur) ; « 📥 Reprendre les réglages de » copie Emby, TheMovieDB ou Telegram d'une autre appli MouFl. Les clés ne s'affichent jamais (4 derniers caractères seulement).
- 🗜️ Pages et listes **compressées** (gzip) : beaucoup moins de données au téléphone (fichier `compression.py`, identique dans les quatre applis).
- 🐛 Le menu « Remonter à » des nouveautés ne dépasse plus de l'écran du téléphone.
- 🧪 Tests ajoutés : envoi vers la médiathèque et restauration (dossier factice : sauvegarde de l'ancien poster, noms et dossiers interdits, écriture ratée sans dégât), aperçu en image, applis voisines, compression.
