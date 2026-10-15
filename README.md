# 🏠 Application de Gestion Locative (Gestion de Cité)

Application Flask de gestion locative, commentée en français, pour qu'un
bailleur camerounais gère ses cités, ses logements, ses locataires et
ses encaissements de loyer.

## Contenu du projet

```
gestion_locative/
├── app.py                  → Toute la logique (Flask + SQLite), un seul fichier
├── templates/
│   ├── base.html            → Structure commune (menu, styles Tailwind)
│   ├── accueil.html         → Page publique d'accueil
│   ├── inscription.html     → Formulaire public de candidature locataire
│   ├── merci.html           → Confirmation après candidature
│   ├── login.html           → Connexion administrateur
│   └── dashboard.html       → Tableau de bord admin (3 onglets)
└── cite_manager.db          → Créé automatiquement au premier lancement
```

## Installation (une seule fois)

Il vous faut Python 3 installé sur votre ordinateur. Ouvrez un terminal
dans le dossier `gestion_locative` et tapez :

```bash
pip install -r requirements.txt
```

## Déploiement Render et persistance

Le fichier `render.yaml` décrit le service Gunicorn lié à `0.0.0.0:$PORT`
et son disque persistant monté sur `/var/data`. Render fournit un sous-domaine
public HTTPS après le premier déploiement. Le chemin par défaut sur Render est
`/var/data/cite_manager.db`; `DATABASE_PATH` permet de le surcharger.
SQLite est configuré en WAL avec un
timeout de 10 secondes. Le disque persistant est indispensable : le système
de fichiers éphémère d’un service Render est perdu lors d’un redéploiement.
La route admin **Télécharger une sauvegarde** fournit un instantané cohérent
créé avec l’API `sqlite3.backup` (et non une copie à chaud du fichier WAL).

### Restaurer une sauvegarde

1. Télécharger la sauvegarde depuis le tableau de bord et la conserver en lieu sûr.
2. Arrêter le service Render ou le mettre en maintenance pour empêcher les écritures.
3. Copier la sauvegarde vers `/var/data/cite_manager.db` (par exemple via un
  shell Render ou un transfert sécurisé), en remplaçant l’ancienne base.
4. Supprimer les fichiers `/var/data/cite_manager.db-wal` et
  `/var/data/cite_manager.db-shm` s’ils existent, puis redémarrer le service.
  Ne jamais restaurer seulement le fichier principal pendant que l’application
  est active.

Variables de déploiement : `DATABASE_PATH` (chemin du fichier SQLite),
`SECRET_KEY`, `MOT_DE_PASSE_ADMIN` et `APP_BASE_URL`. Définir ces valeurs dans les
secrets Render; ne pas utiliser les valeurs de développement. Dans le
dashboard Render, renseigner un mot de passe fort pour `MOT_DE_PASSE_ADMIN`;
`APP_BASE_URL` doit être l’adresse HTTPS publique exacte du service (par exemple
`https://gestion-locative.onrender.com`), afin que les liens de reçus transmis
par WhatsApp fonctionnent depuis l’extérieur. `SECRET_KEY` est générée par le
blueprint.

### Publication initiale

1. Créer un dépôt Git privé et y pousser le code, sans `cite_manager.db`, `.venv`
  ni caches Python. Le fichier `.gitignore` exclut ces éléments.
2. Dans Render, créer un service depuis ce dépôt et choisir le blueprint
  `render.yaml`. Renseigner `MOT_DE_PASSE_ADMIN` dans les secrets demandés.
3. Après le premier déploiement, copier l’URL HTTPS Render affichée dans le
  dashboard dans la variable `APP_BASE_URL`, puis redéployer.
4. Ouvrir le site public, se connecter à `/admin/login`, puis ajouter ses vraies
  cités, logements et locataires.

Une base neuve ne reçoit pas de données fictives. Pour remplir une base locale
de démonstration uniquement, définir `LOAD_DEMO_DATA=1` avant le premier lancement.
Ne jamais activer cette option sur le service de location réel.

## Lancement de l'application

Toujours dans le dossier `gestion_locative` :

```bash
export MOT_DE_PASSE_ADMIN='choisir-un-mot-de-passe-long-et-unique'
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

Ne partagez pas ces valeurs et ne les écrivez pas dans le code. L’application
refuse de démarrer si `MOT_DE_PASSE_ADMIN` n’est pas défini. Le secret local
est à garder dans les variables d’environnement ou un fichier `.env` non
versionné.

Le serveur écoute sur toutes les interfaces réseau sur le port `5000` par
défaut (ou sur la valeur de `PORT`). Pour l’utiliser depuis un autre appareil
connecté au même Wi-Fi ou réseau local :

1. Connecter l’ordinateur qui exécute l’application et les appareils clients
  au même réseau. L’ordinateur hôte doit rester allumé et l’application lancée.
2. Trouver l’adresse IPv4 locale de l’ordinateur hôte (`ip addr` sous Linux,
  `ipconfig` sous Windows, ou les réglages réseau sur macOS). Elle ressemble
  à `192.168.1.25` ou `10.0.0.12`.
3. Si nécessaire, autoriser les connexions entrantes TCP sur le port `5000`
  dans le pare-feu de l’ordinateur hôte. Ne pas exposer ce port sur Internet.
4. Sur les autres appareils, ouvrir `http://ADRESSE_IP:5000/`.

Par exemple, si l’IP de l’ordinateur hôte est `192.168.1.25`, l’adresse à
ouvrir est `http://192.168.1.25:5000/`; l’administration est à
`http://192.168.1.25:5000/admin/login`. Il faut réutiliser l’IP actuelle de
l’ordinateur hôte, qui peut changer après reconnexion au réseau.

Le serveur utilise `debug=False`. Pour un usage intranet durable, réserver
l’IP de l’ordinateur hôte dans le routeur et protéger le réseau Wi-Fi par un
mot de passe fort. Le mot de passe administrateur doit être défini dans
`MOT_DE_PASSE_ADMIN`.

L’accès local sur l’ordinateur hôte reste possible via :

- **Côté public** (visiteurs / futurs locataires) :
  http://127.0.0.1:5000/

- **Côté administrateur** (le bailleur) :
  http://127.0.0.1:5000/admin/login
  Le mot de passe doit être fourni par la configuration sécurisée de
  déploiement (`MOT_DE_PASSE_ADMIN`).

La base de données est créée automatiquement au premier lancement et attend
que vous y ajoutiez vos données réelles. Les enregistrements de démonstration
sont désactivés par défaut.

## Comment fonctionne le côté "public"

N'importe qui peut ouvrir la page d'accueil, consulter la liste des
cités, et remplir un formulaire de candidature (nom, téléphone, CNI,
cité souhaitée). Cette candidature arrive dans l'onglet **2. Locataires**
du tableau de bord admin, sous "Candidatures en attente". L'administrateur
choisit alors le logement à attribuer et valide : le locataire est
automatiquement installé et le logement passe en statut "Occupé".
Le formulaire applique un honeypot, une limite de cinq essais par heure et
par adresse IP, vérifie le numéro de téléphone camerounais et demande le
consentement à l’usage des données. Les adresses IP sont stockées sous forme
d’empreinte pour appliquer la limite; les journaux d’administration ne
contiennent ni CNI ni téléphone.
Les CNI et téléphones sont masqués dans les listes; leur affichage complet
est limité à la fiche locataire réservée à l’administrateur.

## Les 3 onglets du tableau de bord admin

1. **⚙️ Configuration de la cité** — Ajouter des cités et des logements,
   voir le taux d'occupation et les logements libres/occupés.
2. **👥 Locataires** — Installer un locataire dans un logement libre,
   traiter les candidatures publiques, libérer un logement.
3. **💰 Caisse & Encaissements** — Enregistrer un paiement de loyer,
   consulter l'historique, voir le total encaissé ce mois-ci et le
   montant restant à percevoir.

Après l’enregistrement d’un paiement, l’application confirme au bailleur que
le reçu est prêt et propose un lien privé aléatoire à transmettre au locataire.
Le bouton WhatsApp ouvre un message prérempli au numéro enregistré; le bailleur
doit confirmer l’envoi dans WhatsApp. Le locataire peut ensuite ouvrir le lien
sans compte sur le site et imprimer/enregistrer le reçu en PDF depuis son
téléphone ou ordinateur. Le reçu affiche une référence unique, le locataire,
le logement, la période, le montant, la date et le mode de paiement.

Cette notification n’est pas un SMS/WhatsApp automatique : cela nécessiterait
un compte fournisseur de messagerie et des secrets/configurations dédiés, qui
ne sont pas encore présents dans le projet. Le reçu reprend un style de
confirmation orange, mais n’est pas émis par Orange Money ni par un autre
opérateur. Les anciennes bases reçoivent automatiquement références et liens
privés lors de la migration. Toute personne possédant le lien privé peut voir
le reçu; il faut le transmettre uniquement au locataire concerné.

Dans l’onglet caisse, les loyers attendus sont recalculés à partir des locataires
actuellement installés. « Argent encaissé ce mois » suit la date réelle du
versement; « Affecté à [mois] » et « Reste à percevoir » suivent le mois de loyer
indiqué sur chaque paiement. Ainsi, installer un locataire augmente l’attendu
et le reste dû, mais ne gonfle pas artificiellement l’argent déjà reçu.

## Modifier facilement l'application

Tout le code est dans un seul fichier `app.py`, avec des commentaires en
français à chaque étape (connexion à la base, création des tables,
chaque route/action). Les pages HTML sont dans `templates/`, écrites en
Jinja2 simple avec des classes Tailwind CSS directement dans le code
(pas de fichier de configuration à gérer).

Quelques points faciles à modifier :
- Mot de passe admin → variable d’environnement `MOT_DE_PASSE_ADMIN`
- Couleurs → classes Tailwind (`bg-emerald-600`, `text-red-600`, etc.)
- Types de logement / modes de paiement → listes déroulantes dans
  `dashboard.html` (`<select>`)
