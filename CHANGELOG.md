# Journal des versions de MouFloster

La version affichée est « 1.0 » suivie du nombre de mises à jour depuis le passage en 1.0 (et de l'empreinte du commit).
L'historique d'avant ce fichier se lit dans les commits du dépôt.

## 5 novembre 2026
- 🎯 **Garder une série cohérente : les posters existants de la série et les propositions classées par ressemblance.** Quand tu choisis le poster d'une série, une bande « 📚 Déjà dans ta médiathèque pour cette série » montre les posters déjà présents dans son dossier (série et saisons), qu'ils aient été faits avec MouFloster ou bien avant. Les posters proposés par TheMovieDB sont ensuite classés du plus proche au moins proche de ces modèles (couleurs, ambiance sombre ou claire, composition), avec un pourcentage et la raison (« mêmes tons bleus, composition proche ») ; toucher un poster l'explique. Le poster de la saison que tu es en train de refaire ne sert pas de modèle, et le classement se refait quand tu changes de saison ou de langue.
- 🧽 **Le texte des posters est signalé** : « ✅ sans texte » (TheMovieDB le dit), ou « texte facile / moyen / difficile à effacer » (estimation d'après le fond autour du titre : lisse = facile, chargé = difficile). Un poster sans texte passe devant un poster chargé de même style ; le bouton « 🎯 Trié par ressemblance » revient à l'ordre de TheMovieDB. Rien n'est jamais modifié sans ton choix.
- Pour l'instant : séries seulement (pas les films ni les sagas), comparaison avec la série elle-même (pas avec les autres séries de la médiathèque).

## 30 octobre 2026
- 📚 **Les posters déjà faits dans MouFlanga se retrouvent à la recherche, même sans fiche TheMovieDB** (manga sans anime) : en tapant un nom, les séries de MouFlanga qui portent ce nom et ont un poster choisi apparaissent en tête (« 📚 + Again · ton poster dans MouFlanga ») ; « again », « plus again » et « + Again » donnent le même résultat. Un clic reprend le poster pour le modifier ou le renvoyer. Réservé au compte principal.

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
