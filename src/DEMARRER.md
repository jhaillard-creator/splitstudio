# Split Studio

## Installation

1. Décompresse SplitStudio.zip, puis glisse **Split Studio** dans ton dossier **Applications**.
2. Premier lancement : fais un **clic droit** sur Split Studio, choisis **Ouvrir**, puis **Ouvrir** à nouveau. macOS le demande une seule fois, car l’application ne vient pas de l’App Store. Si macOS refuse, va dans Réglages Système › Confidentialité et sécurité, puis clique sur **Ouvrir quand même**.
3. Au tout premier lancement, Split Studio installe son moteur vidéo et IA (quelques minutes, connexion Internet nécessaire). Une notification te prévient, puis la fenêtre s’ouvre toute seule. Les lancements suivants sont immédiats.

Rien d’autre à installer : pas de Terminal, pas de Python à télécharger.

## Utilisation

Colle l’URL d’une vidéo, choisis 10 secondes, 3 minutes ou 10 minutes, puis génère tes clips. Le mode **Moments forts IA** choisit automatiquement les passages les plus intéressants ; **Vidéo entière** découpe tout, du début à la fin.

Chaque clip affiche « Partie N » dans la bande noire du haut et ses sous-titres dans la bande noire du bas. Pour afficher deux vidéos, choisis le split screen et ajoute l’URL de la vidéo du bas.

Chaque vidéo a son dossier : les clips sont rangés dans **Films › Split Studio › titre de la vidéo**. Dans Mes clips, coche des clips pour les ranger dans un autre dossier, les enregistrer ou renommer un dossier. Le bouton Télécharger ouvre la fenêtre « Enregistrer sous » de macOS.

## En cas de souci

Le journal se trouve dans ~/Library/Logs/Split Studio.log. Les données internes sont dans ~/Library/Application Support/Split Studio ; supprimer ce dossier réinstalle le moteur au prochain lancement.

Pour associer TikTok, suis « Configurer TikTok.txt ». Le rendu et l’enregistrement des clips fonctionnent sans.
