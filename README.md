# Bot GlobalExam

Le launcher (`BotGlobalExam.exe`) vérifie à chaque lancement `version.txt` ici.
Si la version est plus récente que celle de l'exe, il télécharge `bot.py` dans
le dossier `maj` à côté de l'exe et l'utilise : pas besoin de reconstruire l'exe.

- `bot.py` : le moteur (mis à jour automatiquement)
- `launcher.py` : l'interface (une modification demande `Construction.bat`)
- `version.txt` : version publiée du bot

Pour construire l'exe : ajouter `cle_fournie.py` (privé, non publié), puis lancer `Construction.bat`.
