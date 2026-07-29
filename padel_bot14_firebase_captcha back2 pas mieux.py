#!/usr/bin/env python3
"""
Bot de réservation automatique - Padel Country Club Cornebarrieu
Plateforme : Gestion-Sports — Version Playwright (navigateur headless)
"""

import asyncio
import logging
import os
import re
import json
import tempfile
from datetime import datetime, timedelta
from patchright.async_api import async_playwright, Page
import random
# playwright-stealth retiré : ses patchs JS (navigator.plugins factice, navigator.webdriver
# réécrit via Object.defineProperty, window.chrome appauvri) sont des signatures connues et
# détectables, redondantes avec le vrai fix apporté par Patchright au niveau du protocole CDP.
# Les garder ne protège plus rien une fois Patchright en place — ça expose une trace de plus.

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    "email":    os.environ.get("PADEL_EMAIL", ""),
    "password": os.environ.get("PADEL_PASSWORD", ""),

    "id_club":  388,
    "id_user":  878037,
    "base_url": "https://the-country-club-toulouse.gestion-sports.com",
    "club_slug":"the-country-club-toulouse",

    "target_weekday": 3,
    "max_slot_time": "18:00",
    "preferred_slots": ["18:00"],
    "duration": 90,
    "nb_reservations": 1,
    "court_ids": [3025],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  FIREBASE
# ─────────────────────────────────────────────

def init_firebase():
    """Initialise Firebase Admin SDK depuis le secret GitHub."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
        if not service_account_json:
            log.warning("FIREBASE_SERVICE_ACCOUNT manquant — écriture Firebase désactivée")
            return None

        # Écrire le JSON dans un fichier temporaire
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(service_account_json)
            tmp_path = f.name

        cred = credentials.Certificate(tmp_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        log.info("Firebase initialisé avec succès")
        return db

    except Exception as e:
        log.warning(f"Firebase init error : {e}")
        return None


def write_reservation_to_firebase(db, date_str: str, heure: str, statut: str, terrain: str = "", email: str = ""):
    """Écrit une réservation dans Firestore."""
    if not db:
        return
    try:
        from firebase_admin import firestore as fs
        doc_ref = db.collection("padel").document("tracker")
        doc = doc_ref.get()
        data = doc.to_dict() if doc.exists else {}

        reservations = data.get("bot_reservations", [])
        reservations.append({
            "date": date_str,
            "heure": heure,
            "terrain": terrain,
            "statut": statut,
            "email": email,
            "bot": "bot14",
            "timestamp": datetime.now().isoformat(),
        })

        doc_ref.set({"bot_reservations": reservations}, merge=True)
        log.info(f"Réservation écrite dans Firebase : {date_str} {heure} {terrain} — {statut}")

    except Exception as e:
        log.warning(f"Firebase write error : {e}")


# ─────────────────────────────────────────────
#  ALERTE EMAIL (secours si CAPTCHA malgré Patchright)
# ─────────────────────────────────────────────

def send_status_email(date_str: str, lines: list):
    """Envoie un email récapitulatif après chaque exécution du bot (succès ou échec)."""
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_password = os.environ.get("GMAIL_PASSWORD", "")
    if not gmail_user or not gmail_password:
        log.warning("GMAIL_USER / GMAIL_PASSWORD non définis — email de statut non envoyé")
        return

    dest = os.environ.get("ALERT_EMAIL_TO", gmail_user)
    body_text = "\n".join(lines) if lines else "Aucune information disponible."
    success = any("confirmée" in l.lower() for l in lines)
    icon = "✅" if success else "⚠️"

    try:
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(
            f"Récapitulatif du bot pour le {date_str} :\n\n{body_text}\n\n"
            f"Plateforme : {CONFIG['base_url']}"
        )
        msg["Subject"] = f"{icon} Bot Padel — {date_str}"
        msg["From"] = gmail_user
        msg["To"] = dest

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, [dest], msg.as_string())
        log.info(f"Email de statut envoyé à {dest}")
    except Exception as e:
        log.warning(f"Échec envoi email de statut : {e}")


# ─────────────────────────────────────────────
#  LOGIQUE DATE
# ─────────────────────────────────────────────

def next_target_date() -> str:
    today = datetime.now()
    target_weekday = CONFIG["target_weekday"]
    current_weekday = today.weekday()
    if current_weekday < target_weekday:
        days_to_next = target_weekday - current_weekday
    elif current_weekday > target_weekday:
        days_to_next = 7 - (current_weekday - target_weekday)
    else:
        days_to_next = 7
    total_days = days_to_next + 14
    return (today + timedelta(days=total_days)).strftime("%Y-%m-%d")


def slot_within_limit(slot_time: str) -> bool:
    max_time = CONFIG.get("max_slot_time")
    if not max_time:
        return True
    return slot_time <= max_time


# ─────────────────────────────────────────────
#  PLAYWRIGHT
# ─────────────────────────────────────────────

async def screenshot(page: Page, name: str):
    try:
        await page.screenshot(path=f"{name}.png", full_page=True)
        log.info(f"Capture : {name}.png")
    except Exception:
        pass


async def login(page: Page) -> bool:
    log.info("Chargement de la page de connexion...")
    await page.goto(CONFIG["base_url"] + "/connexion.php", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    await screenshot(page, "debug_01_login_page")

    # Profil Chrome persistant : la session peut déjà être active, auquel cas
    # /connexion.php redirige directement vers l'espace membre (/appli/...) sans
    # jamais afficher de formulaire — pas la peine de chercher un champ email.
    if "/appli/" in page.url:
        log.info(f"Session déjà active (redirection vers {page.url}) — formulaire de connexion ignoré")
        await close_popup(page)
        return True

    email_sel = 'input[type="email"], input[name="email"], input[name="login"], input[placeholder*="mail"]'
    try:
        await page.wait_for_selector(email_sel, state="visible", timeout=10000)
        await page.fill(email_sel, CONFIG["email"])
        log.info("Email renseigné")
    except Exception as e:
        log.error(f"Champ email non trouvé : {e}")
        return False

    await page.wait_for_timeout(500)

    pwd_visible = False
    try:
        pwd_el = page.locator('input[type="password"]').first
        pwd_visible = await pwd_el.is_visible()
    except Exception:
        pass

    if pwd_visible:
        await page.fill('input[type="password"]', CONFIG["password"])
        await page.wait_for_timeout(300)
        await page.click('button[type="submit"], input[type="submit"]')
    else:
        await page.click(
            'button[type="submit"], input[type="submit"], '
            'button:has-text("CONNEXION / INSCRIPTION"), button:has-text("Suivant")'
        )
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_selector('input[type="password"]', state="visible", timeout=10000)
        except Exception:
            log.error("Champ mot de passe invisible")
            return False

        await page.fill('input[type="password"]', CONFIG["password"])
        await page.wait_for_timeout(800)
        try:
            btn = page.locator('button:has-text("SE CONNECTER")').first
            if await btn.count() > 0:
                await btn.click()
            else:
                await page.click('button[type="submit"], input[type="submit"]')
        except Exception as e:
            log.error(f"Erreur clic bouton : {e}")

    await page.wait_for_timeout(8000)
    await screenshot(page, "debug_03_after_login")

    if "connexion" in page.url:
        log.error("Toujours sur la page de connexion")
        return False

    log.info("Connexion réussie")
    await close_popup(page)
    return True


async def close_popup(page: Page):
    try:
        for attempt in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            for sel in ['button:has-text("Compris")', 'button:has-text("Fermer")',
                        '[class*="close"]', '[class*="modal"] button']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    pass
            await page.evaluate("""
                document.querySelectorAll('[class*="modal"],[class*="popup"],[class*="overlay"]')
                    .forEach(m => { m.style.display='none'; m.remove(); });
                document.body.style.overflow='auto';
            """)
            await page.wait_for_timeout(500)
    except Exception as e:
        log.warning(f"close_popup : {e}")


async def navigate_to_reservations(page: Page) -> bool:
    log.info("Navigation vers la page de réservation...")
    try:
        await page.goto(CONFIG["base_url"] + "/appli/Reservation", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        await screenshot(page, "debug_04_reservation_page")
        return True
    except Exception as e:
        log.error(f"Erreur navigation : {e}")
        return False


async def select_date(page: Page, date_str: str) -> bool:
    log.info(f"Sélection de la date : {date_str}")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day = str(dt.day)
        months_fr = ["jan","fév","mar","avr","mai","juin","juil","aoû","sep","oct","nov","déc"]
        pattern = day + months_fr[dt.month - 1]

        await page.wait_for_timeout(2000)
        date_divs = page.locator('[class*="dateEle"]')
        count = await date_divs.count()
        log.info(f"Boutons dateEle trouvés : {count}")

        all_texts = []
        for i in range(count):
            txt = await date_divs.nth(i).text_content()
            if txt:
                all_texts.append(txt.strip().replace(" ", "").replace("\n", "").replace("\r", ""))
        log.info(f"Textes dateEle : {all_texts}")

        for i in range(count):
            if pattern.lower() in all_texts[i].lower():
                log.info(f"Clic sur dateEle [{i}] : {repr(all_texts[i])}")
                await date_divs.nth(i).click()
                await page.wait_for_timeout(3000)
                log.info("Date sélectionnée avec succès")
                return True

        log.error(f"Pattern '{pattern}' non trouvé — date hors calendrier. Abandon.")
        return False
    except Exception as e:
        log.error(f"Erreur sélection date : {e}")
        return False


async def reserve_slots(page: Page, date_str: str, db=None, dry_run: bool = False):
    reserved = 0
    results = []  # (heure, statut) — pour le récapitulatif email de fin de run

    if not await navigate_to_reservations(page):
        return 0, results

    if not await select_date(page, date_str):
        log.error("Date introuvable dans le calendrier — réservation annulée.")
        return 0, results
    await page.wait_for_timeout(2000)

    # Fermer panneau Je cherche
    try:
        valider = page.locator('button:has-text("Valider"), div.button:has-text("Valider")').first
        if await valider.count() > 0 and await valider.is_visible():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            await page.mouse.click(250, 30)
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    await screenshot(page, "debug_05_slots")
    log.info("Recherche des créneaux disponibles...")
    slots_to_book = []

    slots_info = await page.evaluate("""
        () => {
            const divs = document.querySelectorAll('div.heure');
            // Libellés de terrain visibles sur la page (ex: "Padel - Terrain n.6 - Outdoor")
            const terrainLabels = [];
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length === 0) {
                    const t = el.textContent.trim();
                    if (/Terrain\\s*n\\.?\\s*\\d+/i.test(t) && t.length < 60) {
                        const rect = el.getBoundingClientRect();
                        terrainLabels.push({ text: t, top: rect.top + window.scrollY });
                    }
                }
            });

            const result = [];
            divs.forEach((d, i) => {
                const txt = d.textContent.trim();
                if (/^\\d{2}:\\d{2}$/.test(txt)) {
                    const rect = d.getBoundingClientRect();
                    const divTop = rect.top + window.scrollY;
                    // Terrain le plus proche juste au-dessus de ce créneau
                    let terrain = '';
                    let bestTop = -Infinity;
                    terrainLabels.forEach(tl => {
                        if (tl.top <= divTop + 5 && tl.top > bestTop) {
                            bestTop = tl.top;
                            terrain = tl.text;
                        }
                    });
                    result.push({ index: i, text: txt, terrain: terrain });
                }
            });
            return result;
        }
    """)
    log.info(f"Créneaux libres trouvés : {[s['text'] for s in slots_info]}")

    all_heure_divs = page.locator("div.heure")

    for pref_time in CONFIG["preferred_slots"]:
        if len(slots_to_book) >= CONFIG["nb_reservations"]:
            break
        if not slot_within_limit(pref_time):
            continue
        match = next((s for s in slots_info if s["text"] == pref_time), None)
        if match:
            div = all_heure_divs.nth(match["index"])
            slots_to_book.append({"time": pref_time, "el": div, "index": match["index"], "terrain": match.get("terrain", "")})
            log.info(f"  Créneau retenu : {pref_time} (index {match['index']}, terrain : {match.get('terrain', '?')})")
        else:
            log.info(f"  Indisponible : {pref_time}")

    if not slots_to_book:
        log.warning("Aucun créneau disponible")
        # Écrire l'échec dans Firebase
        write_reservation_to_firebase(db, date_str, "N/A", "aucun_creneau_disponible")
        results.append(("N/A", "aucun_creneau_disponible"))
        return 0, results

    for slot in slots_to_book:
        if dry_run:
            log.info(f"[DRY-RUN] Réservation simulée : {slot['time']}")
            reserved += 1
            write_reservation_to_firebase(db, date_str, slot['time'], "dry_run")
            results.append((slot['time'], "dry_run"))
            continue

        try:
            log.info(f"Réservation du créneau {slot['time']}...")

            await page.evaluate(f"""
                () => {{
                    const divs = document.querySelectorAll('div.heure');
                    const d = divs[{slot['index']}];
                    if (d) d.click();
                }}
            """)
            await page.wait_for_timeout(3000)
            await screenshot(page, f"debug_06_slot_{slot['time'].replace(':', 'h')}")

            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1000)

            # Délais humains avant Je réserve
            await page.wait_for_timeout(random.randint(800, 2000))
            await page.mouse.move(random.randint(100, 500), random.randint(100, 400))
            await page.wait_for_timeout(random.randint(300, 700))

            # Je réserve
            je_reserve = await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const t = el.textContent.trim();
                        if ((t === 'Je réserve' || t.startsWith('Je réserve') || t.startsWith('Je reserve'))
                            && el.children.length <= 2) {
                            el.scrollIntoView(); el.click();
                            return el.tagName + '|' + t;
                        }
                    }
                    return null;
                }
            """)
            if je_reserve:
                log.info(f"Je réserve cliqué : {repr(je_reserve)}")
                await page.wait_for_timeout(3000)
                await screenshot(page, f"debug_07_je_reserve_{slot['time'].replace(':', 'h')}")
            else:
                log.warning(f"Je réserve non trouvé pour {slot['time']}")

            await page.wait_for_timeout(3000)

            # Valider ma réservation
            valider = await page.evaluate("""
                () => {
                    const selectors = ['[class*="resa"]','[class*="bg-c-club"]','[class*="bg-c-green"]','button','p.button'];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const t = el.textContent.trim();
                            if (t === 'Valider ma réservation' || t === 'Valider ma reservation') {
                                el.scrollIntoView(); el.click();
                                return el.tagName + '|' + t;
                            }
                        }
                    }
                    return null;
                }
            """)
            if valider:
                log.info(f"Valider ma réservation cliqué : {repr(valider)}")
                # Attendre plus longtemps pour laisser le temps à la page de charger
                await page.wait_for_timeout(8000)
                await screenshot(page, f"debug_08_confirmed_{slot['time'].replace(':', 'h')}")

                page_text = await page.evaluate("() => document.body.innerText")
                # Le terrain n'apparaît pas sur l'écran de confirmation (juste un message
                # générique) — on utilise celui capturé avant le clic, associé au créneau.
                terrain_name = slot.get("terrain", "")
                log.info(f"Terrain associé au créneau : {terrain_name or '(non détecté)'}")
                email_used = CONFIG.get("email", "")

                # Détection CAPTCHA par logique d'exclusion :
                # Si toujours sur page paiement (contient "garantie" ou "carte") = CAPTCHA probable
                if "validée" in page_text.lower() or "réservation validée" in page_text.lower():
                    log.info(f"✅ Réservation validée : {slot['time']}")
                    reserved += 1
                    write_reservation_to_firebase(db, date_str, slot['time'], "confirmée", terrain_name, email_used)
                    results.append((slot['time'], "confirmée"))
                elif ("garantie" in page_text.lower() or
                      "valider ma réservation" in page_text.lower() or
                      "carte" in page_text.lower() and "bancaire" in page_text.lower()):
                    log.warning("⚠️ CAPTCHA probable — toujours sur page paiement après 8s")
                    write_reservation_to_firebase(db, date_str, slot['time'], "echec_captcha", terrain_name, email_used)
                    results.append((slot['time'], "echec_captcha"))
                else:
                    log.warning(f"Confirmation non détectée pour {slot['time']} — non comptabilisée comme réussie")
                    write_reservation_to_firebase(db, date_str, slot['time'], "probable", terrain_name, email_used)
                    results.append((slot['time'], "probable"))
            else:
                log.warning(f"Valider ma réservation non trouvé pour {slot['time']}")
                write_reservation_to_firebase(db, date_str, slot['time'], "echec_validation", "", CONFIG.get("email", ""))
                results.append((slot['time'], "echec_validation"))

            await page.goto(CONFIG["base_url"] + "/appli/Reservation", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await select_date(page, date_str)
            await page.wait_for_timeout(2000)
            slots_info = await page.evaluate("""
                () => {
                    const divs = document.querySelectorAll('div.heure');
                    const result = [];
                    divs.forEach((d, i) => {
                        const txt = d.textContent.trim();
                        if (/^\\d{2}:\\d{2}$/.test(txt)) result.push({ index: i, text: txt });
                    });
                    return result;
                }
            """)
            all_heure_divs = page.locator("div.heure")

        except Exception as e:
            log.error(f"Erreur réservation {slot['time']} : {e}")
            write_reservation_to_firebase(db, date_str, slot['time'], f"erreur: {str(e)[:50]}")
            results.append((slot['time'], f"erreur: {str(e)[:50]}"))

    return reserved, results


async def check_existing_reservations(page: Page, date_str: str) -> int:
    log.info(f"Vérification des réservations existantes pour le {date_str}...")
    try:
        await page.goto(CONFIG["base_url"] + "/appli/Agenda", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        await page.evaluate("""
            () => {
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    if (el.textContent.trim() === 'Liste' && el.children.length === 0) {
                        el.click(); return;
                    }
                }
            }
        """)
        await page.wait_for_timeout(4000)

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        months_fr = ["janvier","février","mars","avril","mai","juin",
                     "juillet","août","septembre","octobre","novembre","décembre"]
        days_fr = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
        date_formatted = f"{days_fr[dt.weekday()]} {dt.day} {months_fr[dt.month-1]} {dt.year}"
        log.info(f"Recherche de : '{date_formatted}'")

        await page.wait_for_timeout(2000)
        body_text = await page.evaluate("() => document.body.innerText")
        log.info(f"Texte agenda (500 chars) : {body_text[:500]}")

        count = body_text.lower().count(date_formatted.lower())
        log.info(f"Réservations existantes pour '{date_formatted}' : {count}")
        return count

    except Exception as e:
        log.warning(f"Impossible de vérifier l'agenda : {e}")
        return 0


async def run(dry_run: bool = False):
    log.info("=" * 50)
    log.info("Bot Padel — Country Club Cornebarrieu (Playwright)")
    log.info("=" * 50)

    if not CONFIG["email"]:
        log.error("Variables PADEL_EMAIL / PADEL_PASSWORD manquantes.")
        return

    # Initialiser Firebase
    db = init_firebase()

    target_date = next_target_date()
    day_names = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    log.info(f"Date cible : {target_date} ({day_names[CONFIG['target_weekday']]})")

    status_lines = []

    async with async_playwright() as p:
        # Viewport légèrement aléatoire pour paraître humain
        vw = random.randint(1260, 1400)
        vh = random.randint(800, 900)
        # launch_persistent_context() retourne directement un BrowserContext
        # (il n'existe pas d'objet Browser séparé dans ce mode, donc pas de .new_context() possible) :
        # toutes les options de contexte doivent être passées ici en un seul appel.
        context = await p.chromium.launch_persistent_context(
            user_data_dir=r"C:\Users\Arnaud\AppData\Local\Google\Chrome\User Data\Default",
            headless=False,  # Visible
            channel="chrome",  # Utiliser le vrai Chrome installé
            args=["--disable-blink-features=AutomationControlled"],
            # Pas de user_agent custom : celui du vrai Chrome reste cohérent avec ses
            # en-têtes Sec-CH-UA (client hints) et n'a pas besoin d'être mis à jour à
            # chaque montée de version de Chrome.
            viewport={"width": vw, "height": vh},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            color_scheme="dark",
        )
        # Pas de add_init_script ni de stealth_async ici : avec un vrai Chrome piloté par
        # Patchright, navigator.webdriver, navigator.plugins et window.chrome ont déjà des
        # valeurs natives cohérentes. Les réécrire à la main est ce qui les rend détectables.
        page = await context.new_page()

        try:
            if not await login(page):
                log.error("Abandon — connexion échouée.")
                write_reservation_to_firebase(db, target_date, "N/A", "echec_connexion")
                status_lines.append("Connexion échouée — le bot n'a pas pu se connecter.")
                send_status_email(target_date, status_lines)
                return

            existing = await check_existing_reservations(page, target_date)
            nb_to_reserve = CONFIG["nb_reservations"] - existing

            if nb_to_reserve <= 0:
                log.info(f"✅ {existing} réservation(s) déjà en place — rien à faire.")
                write_reservation_to_firebase(db, target_date, "N/A", "deja_reservé")
                status_lines.append(f"{existing} réservation(s) déjà en place — rien à faire.")
            else:
                log.info(f"{existing} existante(s) — il en manque {nb_to_reserve}.")
                original_nb = CONFIG["nb_reservations"]
                CONFIG["nb_reservations"] = nb_to_reserve
                reserved, slot_results = await reserve_slots(page, target_date, db=db, dry_run=dry_run)
                CONFIG["nb_reservations"] = original_nb
                log.info(f"Résultat : {reserved + existing}/{original_nb} réservation(s) au total")
                status_lines.append(f"{reserved + existing}/{original_nb} réservation(s) obtenue(s) au total.")
                status_lines.extend(f"  {heure} → {statut}" for heure, statut in slot_results)

            send_status_email(target_date, status_lines)

        except Exception as e:
            log.error(f"Erreur inattendue : {e}")
            write_reservation_to_firebase(db, target_date, "N/A", f"erreur_inattendue: {str(e)[:50]}")
            await screenshot(page, "debug_error_final")
            status_lines.append(f"Erreur inattendue : {e}")
            send_status_email(target_date, status_lines)
        finally:
            await context.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))
