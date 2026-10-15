# ============================================================================
# APP.PY — Application de Gestion Locative (Gestion de Cité)
# ----------------------------------------------------------------------------
# Application Flask ultra-simple, en un seul fichier, avec base SQLite locale.
# Elle gère :
#   - le côté ADMINISTRATEUR (bailleur) : configuration des cités/logements,
#     gestion des locataires (entrées/sorties), caisse et encaissements.
#   - le côté PUBLIC : un formulaire pour qu'un futur locataire se propose
#     ("candidature") pour un logement dans une cité.
#
# Aucune dépendance lourde : Flask + sqlite3 (inclus dans Python standard).
# Le fichier de base de données "cite_manager.db" est créé automatiquement
# au premier lancement, avec quelques données de test.
# ============================================================================

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from unicodedata import combining, normalize as normaliser_unicode
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import (
    Flask, g, render_template, request, redirect,
    url_for, session, flash, send_file, abort,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# ----------------------------------------------------------------------------
# CONFIGURATION GÉNÉRALE
# ----------------------------------------------------------------------------

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

if os.environ.get("RENDER") and (
    not os.environ.get("SECRET_KEY") or not os.environ.get("MOT_DE_PASSE_ADMIN")
):
    raise RuntimeError("SECRET_KEY et MOT_DE_PASSE_ADMIN doivent être définis sur Render.")

# Clé secrète nécessaire pour utiliser les "sessions" (souvenir de connexion admin).
# Une clé aléatoire de développement évite d’utiliser une valeur de secours connue.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

# Chemin SQLite (sur Render : /var/data/cite_manager.db via DATABASE_PATH).
CHEMIN_BASE_PAR_DEFAUT = "/var/data/cite_manager.db" if os.environ.get("RENDER") else Path(__file__).with_name("cite_manager.db")
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", CHEMIN_BASE_PAR_DEFAUT))
NOM_BASE_DE_DONNEES = str(DATABASE_PATH)  # Alias conservé pour compatibilité.
SQLITE_TIMEOUT = 10
CHARGER_DONNEES_DEMO = os.environ.get("LOAD_DEMO_DATA", "").lower() in {"1", "true", "oui"}
FUSEAU_HORAIRE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Africa/Douala"))
NOMS_MOIS_FRANCAIS = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def normaliser_mois_annee(valeur):
    """Convertit un mois saisi en libellé français canonique, ex. « Octobre 2026 »."""
    texte = (valeur or "").strip()
    if not texte:
        return None

    texte_sans_accents = "".join(
        caractere for caractere in normaliser_unicode("NFKD", texte)
        if not combining(caractere)
    ).casefold()
    mois_par_nom = {
        "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    }

    morceaux = re.fullmatch(r"([a-zA-ZÀ-ÿ]+|\d{1,2})[\s/-]*(\d{4})?", texte)
    format_annee_mois = re.fullmatch(r"(\d{4})-(\d{1,2})", texte)
    if format_annee_mois:
        annee, numero_mois = int(format_annee_mois.group(1)), int(format_annee_mois.group(2))
    elif morceaux:
        token_mois, token_annee = morceaux.groups()
        token_normalise = "".join(
            caractere for caractere in normaliser_unicode("NFKD", token_mois)
            if not combining(caractere)
        ).casefold()
        numero_mois = mois_par_nom.get(token_normalise) if not token_mois.isdigit() else int(token_mois)
        annee = int(token_annee) if token_annee else datetime.now(FUSEAU_HORAIRE).year
    else:
        return None

    if not numero_mois or not 1 <= numero_mois <= 12 or not 1900 <= annee <= 2200:
        return None
    return f"{NOMS_MOIS_FRANCAIS[numero_mois - 1]} {annee}"


def mois_annee_options(nombre_mois=36):
    """Retourne trois années de mois passés et futurs, avec le mois courant sélectionnable."""
    maintenant = datetime.now(FUSEAU_HORAIRE)
    options = []
    for decalage in range(-12, nombre_mois - 12):
        index = maintenant.year * 12 + maintenant.month - 1 + decalage
        annee, mois_index = divmod(index, 12)
        options.append(f"{NOMS_MOIS_FRANCAIS[mois_index]} {annee}")
    return options

# Le service refuse de démarrer avec un mot de passe admin connu par défaut.
MOT_DE_PASSE_ADMIN = os.environ.get("MOT_DE_PASSE_ADMIN")
if not MOT_DE_PASSE_ADMIN:
    raise RuntimeError("Définissez un mot de passe fort dans MOT_DE_PASSE_ADMIN avant le lancement.")


@app.before_request
def verifier_csrf():
    """Vérifie un jeton de session sur chaque formulaire modifiant des données."""
    if request.method == "POST":
        attendu = session.get("csrf_token", "")
        fourni = request.form.get("csrf_token", "")
        if not attendu or not hmac.compare_digest(attendu, fourni):
            abort(400)


@app.context_processor
def fournir_jeton_csrf():
    """Expose le jeton CSRF aux formulaires Jinja2."""
    from flask import session as session_flask
    if "csrf_token" not in session_flask:
        session_flask["csrf_token"] = os.urandom(32).hex()
    return {"csrf_token": session_flask["csrf_token"]}


app.jinja_env.filters["masquer_cni"] = lambda valeur: (
    f"{valeur[:2]}{'*' * max(len(valeur) - 5, 0)}{valeur[-3:]}" if valeur and len(valeur) > 5 else "••••••"
)
app.jinja_env.filters["masquer_telephone"] = lambda valeur: (
    f"{valeur[:2]}{'*' * max(len(valeur) - 4, 0)}{valeur[-2:]}" if valeur and len(valeur) > 4 else "••••••"
)


# ----------------------------------------------------------------------------
# CONNEXION À LA BASE DE DONNÉES
# ----------------------------------------------------------------------------
# On utilise "g", un objet spécial de Flask qui permet de garder une seule
# connexion à la base de données par requête, puis on la ferme proprement.

def obtenir_connexion():
    """Retourne une connexion SQLite ouverte pour la requête en cours."""
    if "connexion_bd" not in g:
        g.connexion_bd = sqlite3.connect(str(DATABASE_PATH), timeout=SQLITE_TIMEOUT)
        # Permet d'accéder aux colonnes par leur nom (ex: ligne["nom_cite"])
        g.connexion_bd.row_factory = sqlite3.Row
        # Active la vérification des clés étrangères (intégrité des données)
        g.connexion_bd.execute("PRAGMA foreign_keys = ON")
    return g.connexion_bd


@app.teardown_appcontext
def fermer_connexion(exception=None):
    """Ferme automatiquement la connexion à la fin de chaque requête."""
    connexion = g.pop("connexion_bd", None)
    if connexion is not None:
        connexion.close()


# ----------------------------------------------------------------------------
# CRÉATION DES TABLES + DONNÉES DE TEST (exécuté une seule fois)
# ----------------------------------------------------------------------------

def initialiser_base_de_donnees():
    """
    Crée les tables si elles n'existent pas encore, et insère quelques
    données de démonstration si la base est vide (premier lancement).
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connexion = sqlite3.connect(str(DATABASE_PATH), timeout=SQLITE_TIMEOUT)
    connexion.execute("PRAGMA foreign_keys = ON")
    connexion.execute("PRAGMA journal_mode = WAL")
    curseur = connexion.cursor()

    # --- Table CITE : les concessions/immeubles du bailleur ---
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS cite (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_cite TEXT NOT NULL,
            quartier TEXT,
            ville TEXT
        )
    """)

    # --- Table LOGEMENT : chaque lot (chambre/studio/appart) d'une cité ---
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS logement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cite_id INTEGER NOT NULL,
            numero TEXT NOT NULL,
            type_logement TEXT NOT NULL,       -- Chambre / Studio / Appartement / Boutique
            prix_loyer INTEGER NOT NULL,       -- en FCFA
            statut TEXT NOT NULL DEFAULT 'Libre',  -- Libre / Occupé
            FOREIGN KEY (cite_id) REFERENCES cite(id)
        )
    """)

    # --- Table LOCATAIRE : personne installée dans un logement ---
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS locataire (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            telephone TEXT,
            cni TEXT,
            logement_id INTEGER,
            date_entree TEXT,
            caution_versee INTEGER DEFAULT 0,
            FOREIGN KEY (logement_id) REFERENCES logement(id)
        )
    """)

    # --- Table PAIEMENT : chaque encaissement de loyer ---
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS paiement (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            locataire_id INTEGER NOT NULL,
            mois_annee TEXT NOT NULL,          -- ex: "Novembre 2026"
            montant INTEGER NOT NULL,
            mode_paiement TEXT NOT NULL,       -- MTN MoMo / Orange Money / Espèces
            date_paiement TEXT NOT NULL,
            reference TEXT UNIQUE,
            jeton_acces TEXT UNIQUE,
            FOREIGN KEY (locataire_id) REFERENCES locataire(id)
        )
    """)

    # Migration non destructive pour les bases créées avant l'ajout des reçus.
    colonnes_paiement = {ligne[1] for ligne in curseur.execute("PRAGMA table_info(paiement)")}
    if "reference" not in colonnes_paiement:
        curseur.execute("ALTER TABLE paiement ADD COLUMN reference TEXT")
    if "jeton_acces" not in colonnes_paiement:
        curseur.execute("ALTER TABLE paiement ADD COLUMN jeton_acces TEXT")
    curseur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paiement_reference ON paiement(reference)")
    curseur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paiement_jeton_acces ON paiement(jeton_acces)")
    paiements_sans_reference = curseur.execute(
        "SELECT id FROM paiement WHERE reference IS NULL OR reference = ''"
    ).fetchall()
    for (paiement_id,) in paiements_sans_reference:
        curseur.execute(
            "UPDATE paiement SET reference = ? WHERE id = ?",
            (f"RECU-{paiement_id:08d}", paiement_id),
        )
    paiements_sans_jeton = curseur.execute(
        "SELECT id FROM paiement WHERE jeton_acces IS NULL OR jeton_acces = ''"
    ).fetchall()
    for (paiement_id,) in paiements_sans_jeton:
        curseur.execute(
            "UPDATE paiement SET jeton_acces = ? WHERE id = ?",
            (secrets.token_urlsafe(32), paiement_id),
        )

    # --- Table CANDIDATURE : demandes envoyées par le formulaire public ---
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS candidature (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            telephone TEXT,
            cni TEXT,
            cite_id INTEGER,
            message TEXT,
            date_demande TEXT,
            statut TEXT DEFAULT 'En attente',  -- En attente / Traitée
            FOREIGN KEY (cite_id) REFERENCES cite(id)
        )
    """)

    # Limitation des formulaires publics, verrouillage persistant de connexion et audit.
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS tentative_candidature (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empreinte_ip TEXT NOT NULL,
            date_tentative TEXT NOT NULL
        )
    """)
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS tentative_connexion (
            empreinte_ip TEXT PRIMARY KEY,
            echecs INTEGER NOT NULL DEFAULT 0,
            verrouille_jusqua TEXT
        )
    """)
    curseur.execute("""
        CREATE TABLE IF NOT EXISTS journal_admin (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acteur TEXT NOT NULL,
            action TEXT NOT NULL,
            date_action TEXT NOT NULL
        )
    """)

    connexion.commit()

    # --- Insertion de données de test SEULEMENT si la table "cite" est vide ---
    curseur.execute("SELECT COUNT(*) FROM cite")
    if curseur.fetchone()[0] == 0 and CHARGER_DONNEES_DEMO:

        # 2 cités de test
        curseur.execute(
            "INSERT INTO cite (nom_cite, quartier, ville) VALUES (?, ?, ?)",
            ("Cité Bonheur", "Ngousso", "Yaoundé"),
        )
        curseur.execute(
            "INSERT INTO cite (nom_cite, quartier, ville) VALUES (?, ?, ?)",
            ("Résidence Les Palmiers", "Mendong", "Yaoundé"),
        )

        # 6 logements de test répartis sur les 2 cités
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (1, 'A1', 'Chambre', 25000, 'Occupé')""")
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (1, 'A2', 'Studio', 50000, 'Occupé')""")
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (1, 'A3', 'Studio', 50000, 'Libre')""")
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (2, 'B1', 'Appartement', 90000, 'Occupé')""")
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (2, 'B2', 'Chambre', 20000, 'Libre')""")
        curseur.execute("""INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
                           VALUES (2, 'B3', 'Boutique', 60000, 'Libre')""")

        # 3 locataires installés dans les logements occupés (id 1, 2 et 4)
        curseur.execute("""INSERT INTO locataire (nom, telephone, cni, logement_id, date_entree, caution_versee)
                           VALUES ('Jean Mbarga', '677010203', '110022334', 1, '2026-01-05', 25000)""")
        curseur.execute("""INSERT INTO locataire (nom, telephone, cni, logement_id, date_entree, caution_versee)
                           VALUES ('Aïcha Ngono', '699112233', '110099887', 2, '2026-02-10', 50000)""")
        curseur.execute("""INSERT INTO locataire (nom, telephone, cni, logement_id, date_entree, caution_versee)
                           VALUES (?, '655667788', '110055443', 4, '2025-11-20', 90000)""", ("Paul Eto'o Fils",))

        # Quelques paiements de test
        curseur.execute("""INSERT INTO paiement (locataire_id, mois_annee, montant, mode_paiement, date_paiement)
                           VALUES (1, 'Septembre 2026', 25000, 'MTN MoMo', '2026-09-03')""")
        curseur.execute("""INSERT INTO paiement (locataire_id, mois_annee, montant, mode_paiement, date_paiement)
                           VALUES (2, 'Septembre 2026', 50000, 'Orange Money', '2026-09-05')""")
        curseur.execute("""INSERT INTO paiement (locataire_id, mois_annee, montant, mode_paiement, date_paiement)
                           VALUES (3, 'Août 2026', 90000, 'Espèces', '2026-08-28')""")

        connexion.commit()

    # Garantit référence et lien privé à tous les paiements, y compris les données de démonstration.
    paiements_incomplets = curseur.execute(
        "SELECT id FROM paiement WHERE reference IS NULL OR reference = '' OR jeton_acces IS NULL OR jeton_acces = ''"
    ).fetchall()
    for (paiement_id,) in paiements_incomplets:
        reference = f"RECU-{paiement_id:08d}"
        jeton_acces = secrets.token_urlsafe(32)
        curseur.execute(
            "UPDATE paiement SET reference = COALESCE(NULLIF(reference, ''), ?), "
            "jeton_acces = COALESCE(NULLIF(jeton_acces, ''), ?) WHERE id = ?",
            (reference, jeton_acces, paiement_id),
        )
    paiements = curseur.execute("SELECT id, mois_annee FROM paiement").fetchall()
    for paiement_id, mois_enregistre in paiements:
        mois_normalise = normaliser_mois_annee(mois_enregistre)
        if mois_normalise and mois_normalise != mois_enregistre:
            curseur.execute(
                "UPDATE paiement SET mois_annee = ? WHERE id = ?",
                (mois_normalise, paiement_id),
            )
    connexion.commit()
    connexion.close()


def empreinte_ip(ip):
    """Retourne une empreinte non lisible de l’adresse IP pour les limites anti-abus."""
    return hashlib.sha256((ip or "inconnue").encode("utf-8")).hexdigest()


def telephone_valide(valeur):
    """Vérifie et normalise un numéro camerounais au format national à 9 chiffres."""
    numero = re.sub(r"[\s.-]", "", (valeur or "").strip())
    if numero.startswith("+237"):
        numero = numero[4:]
    elif numero.startswith("237") and len(numero) == 12:
        numero = numero[3:]
    return numero if re.fullmatch(r"6\d{8}", numero) else None


def logement_libre_existant(bd, logement_id):
    """Retourne un logement réellement libre ou None si l’identifiant est invalide/occupé."""
    return bd.execute(
        "SELECT id FROM logement WHERE id = ? AND statut = 'Libre'", (logement_id,)
    ).fetchone()


def journaliser_action(action):
    """Ajoute une action administrative sans jamais enregistrer de donnée personnelle."""
    obtenir_connexion().execute(
        "INSERT INTO journal_admin (acteur, action, date_action) VALUES (?, ?, ?)",
        ("administrateur", action, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def tracer(action):
    """Enregistre une action sans valeurs de formulaire ni identifiants personnels."""
    bd = obtenir_connexion()
    journaliser_action(action)
    bd.commit()


# ----------------------------------------------------------------------------
# PROTECTION DES PAGES ADMIN (mini "login required")
# ----------------------------------------------------------------------------

def connexion_admin_requise(fonction):
    """Protège les pages administrateur : un accès sans session valide est redirigé vers la connexion."""
    @wraps(fonction)
    def fonction_protegee(*args, **kwargs):
        if not session.get("admin_connecte"):
            return redirect(url_for("connexion_admin"))
        return fonction(*args, **kwargs)
    return fonction_protegee


# ============================================================================
# PARTIE 1 — CÔTÉ PUBLIC (visiteurs / futurs locataires)
# ============================================================================

@app.route("/")
def accueil():
    """Page publique d'accueil : présente les cités et propose de postuler."""
    bd = obtenir_connexion()
    cites = bd.execute("SELECT * FROM cite ORDER BY nom_cite").fetchall()
    statistiques = bd.execute("""
        SELECT COUNT(*) AS total_logements,
               SUM(CASE WHEN statut = 'Libre' THEN 1 ELSE 0 END) AS logements_libres
        FROM logement
    """).fetchone()
    return render_template(
        "accueil.html", cites=cites,
        total_logements=statistiques["total_logements"] or 0,
        logements_libres=statistiques["logements_libres"] or 0,
    )


@app.route("/inscription", methods=["GET", "POST"])
def inscription():
    """
    Formulaire PUBLIC : un futur locataire se propose pour une cité.
    Cela crée une "candidature" que l'administrateur devra valider
    (dans l'onglet Locataires) pour l'installer réellement dans un logement.
    """
    bd = obtenir_connexion()

    if request.method == "POST":
        if request.form.get("website", "").strip():
            abort(400)
        maintenant = datetime.now()
        ip_hash = empreinte_ip(request.remote_addr)
        bd.execute(
            "DELETE FROM tentative_candidature WHERE date_tentative < ?",
            ((maintenant - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),),
        )
        demandes = bd.execute(
            "SELECT COUNT(*) FROM tentative_candidature WHERE empreinte_ip = ? AND date_tentative >= ?",
            (ip_hash, (maintenant - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()[0]
        if demandes >= 5:
            abort(429)
        bd.execute(
            "INSERT INTO tentative_candidature (empreinte_ip, date_tentative) VALUES (?, ?)",
            (ip_hash, maintenant.strftime("%Y-%m-%d %H:%M:%S")),
        )

        nom = request.form.get("nom", "").strip()
        telephone_saisi = request.form.get("telephone", "").strip()
        telephone = telephone_saisi.replace(" ", "")
        if telephone.startswith("+237"):
            telephone = telephone[4:]
        if not re.fullmatch(r"6\d{8}", telephone):
            abort(400)
        cni = request.form.get("cni", "").strip()
        cite_id = request.form.get("cite_id", "").strip()
        if not nom or not cni or not cite_id or not request.form.get("consentement"):
            abort(400)
        if not bd.execute("SELECT 1 FROM cite WHERE id = ?", (cite_id,)).fetchone():
            abort(400)
        message = request.form.get("message", "")
        date_demande = datetime.now().strftime("%Y-%m-%d %H:%M")

        if bd.execute(
            "SELECT 1 FROM candidature WHERE cni = ? AND cite_id = ? AND statut = 'En attente'",
            (cni, cite_id),
        ).fetchone():
            flash("Une candidature est déjà en attente pour cette cité.", "erreur")
            return redirect(url_for("inscription"))

        bd.execute("""
            INSERT INTO candidature (nom, telephone, cni, cite_id, message, date_demande, statut)
            VALUES (?, ?, ?, ?, ?, ?, 'En attente')
        """, (nom, telephone, cni, cite_id, message, date_demande))
        bd.commit()

        return redirect(url_for("merci"))

    cites = bd.execute("SELECT * FROM cite ORDER BY nom_cite").fetchall()
    return render_template("inscription.html", cites=cites)


@app.route("/merci")
def merci():
    """Page de confirmation après l'envoi du formulaire public."""
    return render_template("merci.html")


# ============================================================================
# PARTIE 2 — CONNEXION / DÉCONNEXION ADMINISTRATEUR
# ============================================================================

@app.route("/admin/login", methods=["GET", "POST"])
def connexion_admin():
    """Authentifie l’administrateur avec un mot de passe simple et sécurise l’accès au dashboard."""
    bd = obtenir_connexion()
    ip_hash = empreinte_ip(request.remote_addr)
    if request.method == "POST":
        maintenant = datetime.now()
        blocage = bd.execute(
            "SELECT echecs, verrouille_jusqua FROM tentative_connexion WHERE empreinte_ip = ?",
            (ip_hash,),
        ).fetchone()
        if blocage and blocage["verrouille_jusqua"] and blocage["verrouille_jusqua"] > maintenant.strftime("%Y-%m-%d %H:%M:%S"):
            abort(429)
        if blocage and blocage["verrouille_jusqua"]:
            bd.execute("DELETE FROM tentative_connexion WHERE empreinte_ip = ?", (ip_hash,))
            blocage = None
        mot_de_passe_saisi = request.form.get("mot_de_passe", "")
        if mot_de_passe_saisi == MOT_DE_PASSE_ADMIN:
            bd.execute("DELETE FROM tentative_connexion WHERE empreinte_ip = ?", (ip_hash,))
            session["admin_connecte"] = True
            journaliser_action("connexion")
            bd.commit()
            return redirect(url_for("tableau_de_bord_admin"))
        else:
            echecs = (blocage["echecs"] if blocage else 0) + 1
            verrou = (maintenant + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S") if echecs >= 5 else None
            bd.execute("""
                INSERT INTO tentative_connexion (empreinte_ip, echecs, verrouille_jusqua)
                VALUES (?, ?, ?)
                ON CONFLICT(empreinte_ip) DO UPDATE SET echecs = excluded.echecs,
                    verrouille_jusqua = excluded.verrouille_jusqua
            """, (ip_hash, echecs, verrou))
            bd.commit()
            flash("Mot de passe incorrect.", "erreur")

    return render_template("login.html")


@app.route("/admin/logout", methods=["POST"])
@connexion_admin_requise
def deconnexion_admin():
    """Supprime la session active de l’administrateur pour le déconnecter proprement."""
    session.pop("admin_connecte", None)
    tracer("deconnexion")
    return redirect(url_for("accueil"))


@app.route("/admin/sauvegarde")
@connexion_admin_requise
def sauvegarder_base():
    """Télécharge une sauvegarde SQLite cohérente via l’API backup."""
    source = sqlite3.connect(str(DATABASE_PATH), timeout=SQLITE_TIMEOUT)
    temporaire = tempfile.NamedTemporaryFile(prefix="cite-manager-", suffix=".db", delete=False)
    temporaire.close()
    try:
        with sqlite3.connect(temporaire.name) as cible:
            source.backup(cible)
    finally:
        source.close()

    reponse = send_file(temporaire.name, as_attachment=True, download_name="cite_manager_sauvegarde.db")
    reponse.call_on_close(lambda: Path(temporaire.name).unlink(missing_ok=True))
    return reponse


@app.route("/admin/candidatures/purger", methods=["POST"])
@connexion_admin_requise
def purger_candidatures_traitees():
    """Supprime les candidatures traitées datant de plus de 90 jours."""
    bd = obtenir_connexion()
    limite = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M")
    resultat = bd.execute(
        "DELETE FROM candidature WHERE statut = 'Traitée' AND date_demande < ?", (limite,)
    )
    journaliser_action(f"purge_candidatures:{resultat.rowcount}")
    bd.commit()
    flash(f"{resultat.rowcount} candidature(s) traitée(s) purgée(s).", "succes")
    return redirect(url_for("tableau_de_bord_admin", onglet="locataires"))


@app.errorhandler(400)
def erreur_requete_invalide(erreur):
    return render_template("erreur.html", code=400, message="La demande est invalide."), 400


@app.errorhandler(404)
def page_introuvable(erreur):
    return render_template("erreur.html", code=404, message="Cette page est introuvable."), 404


@app.errorhandler(429)
def trop_de_requetes(erreur):
    return render_template("erreur.html", code=429, message="Trop de tentatives. Réessayez plus tard."), 429


@app.errorhandler(500)
def erreur_serveur(erreur):
    app.logger.error("Erreur interne lors du traitement d’une requête")
    return render_template("erreur.html", code=500, message="Une erreur interne est survenue."), 500


# ============================================================================
# PARTIE 3 — TABLEAU DE BORD ADMINISTRATEUR (3 onglets)
# ============================================================================

@app.route("/admin")
@connexion_admin_requise
def tableau_de_bord_admin():
    """Page principale du gestionnaire : configuration, locataires et caisse, selon l’onglet choisi."""
    bd = obtenir_connexion()
    onglet = request.args.get("onglet", "configuration")

    # --- Données communes utiles à plusieurs onglets ---
    cites = bd.execute("SELECT * FROM cite ORDER BY nom_cite").fetchall()

    # --- ONGLET 1 : CONFIGURATION — logements groupés par cité + taux d'occupation ---
    logements = bd.execute("""
        SELECT logement.*, cite.nom_cite
        FROM logement
        JOIN cite ON cite.id = logement.cite_id
        ORDER BY cite.nom_cite, logement.numero
    """).fetchall()

    statistiques_par_cite = []
    for cite in cites:
        logements_de_la_cite = [l for l in logements if l["cite_id"] == cite["id"]]
        nb_total = len(logements_de_la_cite)
        nb_occupes = len([l for l in logements_de_la_cite if l["statut"] == "Occupé"])
        statistiques_par_cite.append({
            "cite": cite,
            "nb_total": nb_total,
            "nb_occupes": nb_occupes,
            "nb_libres": nb_total - nb_occupes,
            "liste_logements": logements_de_la_cite,
        })

    # --- ONGLET 2 : LOCATAIRES — locataires actuels + candidatures en attente ---
    locataires = bd.execute("""
        SELECT locataire.*, logement.numero, logement.type_logement, cite.nom_cite
        FROM locataire
        JOIN logement ON logement.id = locataire.logement_id
        JOIN cite ON cite.id = logement.cite_id
        ORDER BY locataire.nom
    """).fetchall()

    logements_libres = [l for l in logements if l["statut"] == "Libre"]

    candidatures_en_attente = bd.execute("""
        SELECT candidature.*, cite.nom_cite
        FROM candidature
        JOIN cite ON cite.id = candidature.cite_id
        WHERE candidature.statut = 'En attente'
        ORDER BY candidature.date_demande DESC
    """).fetchall()

    # --- ONGLET 3 : CAISSE — historique des paiements + calculs du mois ---
    paiements = bd.execute("""
        SELECT paiement.*, locataire.nom AS nom_locataire
        FROM paiement
        JOIN locataire ON locataire.id = paiement.locataire_id
        ORDER BY paiement.date_paiement DESC
    """).fetchall()

    # Mois en cours au format "Septembre 2026" (en français)
    noms_mois_francais = [
        "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
        "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
    ]
    maintenant = datetime.now(FUSEAU_HORAIRE)
    mois_courant_libelle = f"{NOMS_MOIS_FRANCAIS[maintenant.month - 1]} {maintenant.year}"

    # L'attendu suit les locataires réellement installés, pas un statut de logement qui pourrait être périmé.
    montant_attendu_ce_mois = bd.execute("""
        SELECT COALESCE(SUM(logement.prix_loyer), 0)
        FROM locataire
        JOIN logement ON logement.id = locataire.logement_id
        WHERE locataire.logement_id IS NOT NULL
    """).fetchone()[0]

    # Distingue la date réelle d'encaissement du mois de loyer auquel le paiement est affecté.
    mois_calendaire = maintenant.strftime("%Y-%m")
    total_encaisse_ce_mois = sum(
        p["montant"] for p in paiements if p["date_paiement"].startswith(mois_calendaire)
    )
    total_affecte_mois_courant = sum(
        p["montant"] for p in paiements
        if normaliser_mois_annee(p["mois_annee"]) == mois_courant_libelle
    )
    montant_restant_a_percevoir = max(
        montant_attendu_ce_mois - total_affecte_mois_courant, 0
    )

    return render_template(
        "dashboard.html",
        onglet=onglet,
        cites=cites,
        statistiques_par_cite=statistiques_par_cite,
        locataires=locataires,
        logements_libres=logements_libres,
        candidatures_en_attente=candidatures_en_attente,
        paiements=paiements,
        mois_courant_libelle=mois_courant_libelle,
        mois_annee_options=mois_annee_options(),
        total_encaisse_ce_mois=total_encaisse_ce_mois,
        total_affecte_mois_courant=total_affecte_mois_courant,
        montant_attendu_ce_mois=montant_attendu_ce_mois,
        montant_restant_a_percevoir=montant_restant_a_percevoir,
    )


@app.route("/admin/locataire/<int:locataire_id>")
@connexion_admin_requise
def fiche_locataire(locataire_id):
    """Affiche la fiche administrative, seul écran où la CNI complète est visible."""
    locataire = obtenir_connexion().execute("""
        SELECT locataire.*, logement.numero, logement.type_logement, cite.nom_cite
        FROM locataire
        LEFT JOIN logement ON logement.id = locataire.logement_id
        LEFT JOIN cite ON cite.id = logement.cite_id
        WHERE locataire.id = ?
    """, (locataire_id,)).fetchone()
    if locataire is None:
        abort(404)
    tracer("fiche_locataire_consultee")
    return render_template("fiche_locataire.html", locataire=locataire)


# ----------------------------------------------------------------------------
# ACTIONS — ONGLET 1 : CONFIGURATION DE LA CITÉ
# ----------------------------------------------------------------------------

@app.route("/admin/cite/ajouter", methods=["POST"])
@connexion_admin_requise
def ajouter_nouvelle_cite():
    """Crée une nouvelle cité qui pourra ensuite accueillir des logements et des locataires."""
    bd = obtenir_connexion()
    bd.execute(
        "INSERT INTO cite (nom_cite, quartier, ville) VALUES (?, ?, ?)",
        (request.form["nom_cite"], request.form["quartier"], request.form["ville"]),
    )
    bd.commit()
    tracer("cite_ajoutee")
    flash("Cité ajoutée avec succès.", "succes")
    return redirect(url_for("tableau_de_bord_admin", onglet="configuration"))


@app.route("/admin/logement/ajouter", methods=["POST"])
@connexion_admin_requise
def ajouter_nouveau_logement():
    """Ajoute un logement libre dans une cité existante afin de pouvoir l'attribuer plus tard."""
    bd = obtenir_connexion()
    bd.execute("""
        INSERT INTO logement (cite_id, numero, type_logement, prix_loyer, statut)
        VALUES (?, ?, ?, ?, 'Libre')
    """, (
        request.form["cite_id"],
        request.form["numero"],
        request.form["type_logement"],
        request.form["prix_loyer"],
    ))
    bd.commit()
    tracer("logement_ajoute")
    flash("Logement ajouté avec succès.", "succes")
    return redirect(url_for("tableau_de_bord_admin", onglet="configuration"))


# ----------------------------------------------------------------------------
# ACTIONS — ONGLET 2 : GESTION DES LOCATAIRES
# ----------------------------------------------------------------------------

@app.route("/admin/locataire/installer", methods=["POST"])
@connexion_admin_requise
def installer_nouveau_locataire():
    """Attribue un logement libre à un locataire et active immédiatement son statut "Occupé"."""
    bd = obtenir_connexion()
    logement_id = request.form.get("logement_id", "")
    telephone = telephone_valide(request.form.get("telephone", ""))
    try:
        caution = int(request.form.get("caution_versee", ""))
    except ValueError:
        abort(400)
    if not request.form.get("nom", "").strip() or not request.form.get("cni", "").strip():
        abort(400)
    if not telephone or caution < 0 or not logement_libre_existant(bd, logement_id):
        abort(400)
    logement_revendique = bd.execute(
        "UPDATE logement SET statut = 'Occupé' WHERE id = ? AND statut = 'Libre'",
        (logement_id,),
    )
    if logement_revendique.rowcount != 1:
        bd.rollback()
        abort(400)
    date_entree = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")

    bd.execute("""
        INSERT INTO locataire (nom, telephone, cni, logement_id, date_entree, caution_versee)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        request.form["nom"],
        telephone,
        request.form["cni"],
        logement_id,
        date_entree,
        caution,
    ))

    bd.commit()
    tracer("locataire_installe")

    flash("Locataire installé avec succès.", "succes")
    return redirect(url_for("tableau_de_bord_admin", onglet="locataires"))


@app.route("/admin/locataire/liberer/<int:locataire_id>", methods=["POST"])
@connexion_admin_requise
def liberer_logement_locataire(locataire_id):
    """Détache un locataire d'un logement pour le remettre à disposition sans supprimer son historique."""
    bd = obtenir_connexion()
    locataire = bd.execute("SELECT * FROM locataire WHERE id = ?", (locataire_id,)).fetchone()

    if locataire and locataire["logement_id"] is not None:
        bd.execute("UPDATE logement SET statut = 'Libre' WHERE id = ?", (locataire["logement_id"],))
        bd.execute("UPDATE locataire SET logement_id = NULL WHERE id = ?", (locataire_id,))
        bd.commit()
        tracer("logement_libere")
        flash("Logement libéré avec succès.", "succes")

    return redirect(url_for("tableau_de_bord_admin", onglet="locataires"))


@app.route("/admin/candidature/traiter/<int:candidature_id>", methods=["POST"])
@connexion_admin_requise
def traiter_candidature_locataire(candidature_id):
    """Valide une candidature publique en créant immédiatement le locataire et le contrat de logement."""
    bd = obtenir_connexion()
    candidature = bd.execute("SELECT * FROM candidature WHERE id = ?", (candidature_id,)).fetchone()
    if candidature is None:
        abort(404)
    if candidature["statut"] != "En attente":
        abort(400)
    logement_id = request.form.get("logement_id", "")
    if not logement_libre_existant(bd, logement_id):
        abort(400)
    logement_revendique = bd.execute(
        "UPDATE logement SET statut = 'Occupé' WHERE id = ? AND statut = 'Libre'",
        (logement_id,),
    )
    if logement_revendique.rowcount != 1:
        bd.rollback()
        abort(400)
    date_entree = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")

    bd.execute("""
        INSERT INTO locataire (nom, telephone, cni, logement_id, date_entree, caution_versee)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (candidature["nom"], candidature["telephone"], candidature["cni"], logement_id, date_entree))

    candidature_traitee = bd.execute(
        "UPDATE candidature SET statut = 'Traitée' WHERE id = ? AND statut = 'En attente'",
        (candidature_id,),
    )
    if candidature_traitee.rowcount != 1:
        bd.rollback()
        abort(400)
    bd.commit()
    tracer("candidature_traitee")

    flash("Candidature validée : locataire installé.", "succes")
    return redirect(url_for("tableau_de_bord_admin", onglet="locataires"))


# ----------------------------------------------------------------------------
# ACTIONS — ONGLET 3 : CAISSE & ENCAISSEMENTS
# ----------------------------------------------------------------------------

@app.route("/admin/paiement/ajouter", methods=["POST"])
@connexion_admin_requise
def enregistrer_paiement():
    """Enregistre un paiement de loyer pour un locataire et l’ajoute à l’historique de caisse."""
    bd = obtenir_connexion()
    date_paiement = datetime.now(FUSEAU_HORAIRE).strftime("%Y-%m-%d")
    mois_annee = normaliser_mois_annee(request.form.get("mois_annee", ""))
    locataire_id = request.form.get("locataire_id", "")
    mode_paiement = request.form.get("mode_paiement", "")
    if mode_paiement not in {"MTN MoMo", "Orange Money", "Espèces"}:
        abort(400)
    try:
        montant = int(request.form.get("montant", ""))
    except ValueError:
        abort(400)
    locataire = bd.execute(
        "SELECT 1 FROM locataire WHERE id = ? AND logement_id IS NOT NULL", (locataire_id,)
    ).fetchone()
    if mois_annee is None or montant <= 0 or locataire is None:
        abort(400)

    bd.execute("""
        INSERT INTO paiement (locataire_id, mois_annee, montant, mode_paiement, date_paiement, reference)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, (
        locataire_id,
        mois_annee,
        montant,
        mode_paiement,
        date_paiement,
    ))
    paiement_id = bd.execute("SELECT last_insert_rowid()").fetchone()[0]
    reference = f"LOY-{datetime.now(FUSEAU_HORAIRE):%Y%m%d}-{paiement_id:06d}"
    jeton_acces = secrets.token_urlsafe(32)
    bd.execute(
        "UPDATE paiement SET reference = ?, jeton_acces = ? WHERE id = ?",
        (reference, jeton_acces, paiement_id),
    )
    bd.commit()
    tracer("paiement_enregistre")

    flash("Paiement enregistré avec succès.", "succes")
    return redirect(url_for("notification_recu", paiement_id=paiement_id))


@app.route("/admin/paiement/<int:paiement_id>/notification")
@connexion_admin_requise
def notification_recu(paiement_id):
    """Confirme au bailleur que le reçu est prêt et fournit son lien partageable."""
    paiement = obtenir_connexion().execute("""
        SELECT paiement.id, paiement.reference, paiement.jeton_acces,
               locataire.nom AS nom_locataire, locataire.telephone
        FROM paiement JOIN locataire ON locataire.id = paiement.locataire_id
        WHERE paiement.id = ?
    """, (paiement_id,)).fetchone()
    if paiement is None:
        abort(404)
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if base_url:
        lien_recu = f"{base_url}{url_for('recu_partage', jeton=paiement['jeton_acces'])}"
    else:
        lien_recu = url_for("recu_partage", jeton=paiement["jeton_acces"], _external=True)
    numero_whatsapp = re.sub(r"\D", "", paiement["telephone"] or "")
    if numero_whatsapp.startswith("0"):
        numero_whatsapp = "237" + numero_whatsapp[1:]
    elif len(numero_whatsapp) == 9:
        numero_whatsapp = "237" + numero_whatsapp
    message = (
        f"Bonjour {paiement['nom_locataire']}, votre reçu de loyer "
        f"({paiement['reference']}) est prêt. Consultez-le et imprimez-le ici : {lien_recu}"
    )
    lien_whatsapp = f"https://wa.me/{numero_whatsapp}?{urlencode({'text': message})}" if numero_whatsapp else None
    return render_template(
        "notification_recu.html", paiement=paiement, lien_recu=lien_recu,
        lien_whatsapp=lien_whatsapp,
    )


@app.route("/admin/paiement/<int:paiement_id>/recu")
@connexion_admin_requise
def recu_paiement(paiement_id):
    """Affiche un reçu imprimable pour un paiement enregistré."""
    paiement = obtenir_connexion().execute("""
        SELECT paiement.*, locataire.nom AS nom_locataire,
               logement.numero AS numero_logement, cite.nom_cite
        FROM paiement
        JOIN locataire ON locataire.id = paiement.locataire_id
        LEFT JOIN logement ON logement.id = locataire.logement_id
        LEFT JOIN cite ON cite.id = logement.cite_id
        WHERE paiement.id = ?
    """, (paiement_id,)).fetchone()
    if paiement is None:
        abort(404)
    return render_template("recu_paiement.html", paiement=paiement)


@app.route("/recu/<jeton>")
def recu_partage(jeton):
    """Permet au locataire de consulter son reçu avec un lien privé non devinable."""
    paiement = obtenir_connexion().execute("""
        SELECT paiement.*, locataire.nom AS nom_locataire,
               logement.numero AS numero_logement, cite.nom_cite
        FROM paiement
        JOIN locataire ON locataire.id = paiement.locataire_id
        LEFT JOIN logement ON logement.id = locataire.logement_id
        LEFT JOIN cite ON cite.id = logement.cite_id
        WHERE paiement.jeton_acces = ?
    """, (jeton,)).fetchone()
    if paiement is None:
        abort(404)
    reponse = app.make_response(
        render_template("recu_paiement.html", paiement=paiement, partage=True)
    )
    reponse.headers["Cache-Control"] = "no-store, private"
    reponse.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return reponse


# ----------------------------------------------------------------------------
# LANCEMENT DE L'APPLICATION
# ----------------------------------------------------------------------------

# Gunicorn importe le module sans exécuter le bloc __main__ : la base doit
# tout de même être créée/migrée avant la première requête.
initialiser_base_de_donnees()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print(" Application de Gestion Locative — démarrage")
    print(f" Réseau local : http://IP_DU_PC:{port}/")
    print(f" Administration : http://IP_DU_PC:{port}/admin/login")
    print("=" * 60)
    # 0.0.0.0 rend le serveur accessible aux autres appareils du réseau local.
    app.run(host="0.0.0.0", port=port, debug=False)
