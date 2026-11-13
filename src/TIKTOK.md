# Associer un compte TikTok

Tout se fait dans Split Studio : clique sur **Compte TikTok** (en bas à gauche) et suis les étapes.

1. Sur [TikTok for Developers](https://developers.tiktok.com/apps/), connecte-toi et crée une application.
2. Ajoute **Login Kit** (plateforme Desktop) et **Content Posting API** avec **Direct Post** et la portée `video.publish`.
3. Dans Login Kit, ajoute l’adresse de redirection affichée par Split Studio (en général `http://127.0.0.1:8765/api/tiktok/callback`). Le bouton **Copier** la met dans le presse-papiers.
4. Colle la **Client key** et le **Client secret** dans Split Studio, puis clique sur **Enregistrer et me connecter à TikTok** et autorise l’accès.

Les clés sont enregistrées sur ton Mac seulement, lisibles par ton compte uniquement.

## Publier

Clique sur **Publier sur TikTok** à côté d’un clip. La légende est déjà remplie (dossier · partie, plus les hashtags de ta dernière publication). Choisis la visibilité, coche la confirmation, puis **Publier ce clip**. Le clip affiche ensuite « Publié ✓ ».

TikTok impose ces deux choix à chaque publication (visibilité sans valeur par défaut et confirmation de la déclaration d’utilisation musicale) ; ils ne peuvent pas être supprimés.

Tant que ton application TikTok n’a pas passé l’audit de TikTok, les publications sont limitées à la visibilité « Moi uniquement », sur un compte privé. Pour publier en public, soumets ton application à l’audit dans le portail développeur. [Conditions de Direct Post](https://developers.tiktok.com/docs/en/content-sharing-guidelines)

Le bouton **Dissocier le compte** révoque l’accès auprès de TikTok.
