"""
Główny moduł programu Email-to-Asana.
Odczytuje wiadomości e-mail i na podstawie reguł tworzy zadania w Asana.

Użycie:
    python main.py
    python main.py --config config.json --rules rules.json
    python main.py --dry-run  (tryb testowy bez tworzenia zadań)
"""

import json
import logging
import argparse
import sys
import os
from typing import Optional
from dataclasses import dataclass, field

from email_reader import EmailReader, EmailMessage
from asana_client import AsanaClient

# Konfiguracja logowania
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("email_to_asana.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


# ===== Struktury danych =====

@dataclass
class RecipientRule:
    """Reguła dopasowania adresata do projektu i workspace."""
    phrase: str
    project_gid: str
    workspace_gid: str
    description: str = ""


@dataclass
class SectionRule:
    """Reguła dopasowania treści do sekcji."""
    tag: str
    section_name: str
    description: str = ""


@dataclass
class RulesConfig:
    """Konfiguracja wszystkich reguł."""
    recipient_rules: list = field(default_factory=list)
    section_rules: list = field(default_factory=list)


@dataclass
class TaskCreationResult:
    """Wynik tworzenia zadania w Asana."""
    success: bool
    task_gid: str = ""
    task_name: str = ""
    project_gid: str = ""
    section_name: str = ""
    error_message: str = ""
    email_subject: str = ""
    email_sender: str = ""


# ===== Ładowanie konfiguracji =====

def load_json_file(filepath: str) -> dict:
    """Ładuje plik JSON i zwraca słownik."""
    if not os.path.exists(filepath):
        logger.error(f"Plik nie istnieje: {filepath}")
        raise FileNotFoundError(f"Nie znaleziono pliku: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info(f"Załadowano plik: {filepath}")
    return data


def load_rules(filepath: str) -> RulesConfig:
    """
    Ładuje reguły z pliku JSON.

    Args:
        filepath: Ścieżka do pliku rules.json

    Returns:
        Obiekt RulesConfig z załadowanymi regułami
    """
    data = load_json_file(filepath)
    config = RulesConfig()

    # Ładowanie reguł adresatów
    for rule_data in data.get("recipient_rules", []):
        rule = RecipientRule(
            phrase=rule_data["phrase"].lower(),
            project_gid=rule_data["project_gid"],
            workspace_gid=rule_data["workspace_gid"],
            description=rule_data.get("description", "")
        )
        config.recipient_rules.append(rule)
        logger.debug(
            f"Załadowano regułę adresata: '{rule.phrase}' -> "
            f"Projekt: {rule.project_gid}"
        )

    # Ładowanie reguł sekcji
    for rule_data in data.get("section_rules", []):
        rule = SectionRule(
            tag=rule_data["tag"],
            section_name=rule_data["section_name"],
            description=rule_data.get("description", "")
        )
        config.section_rules.append(rule)
        logger.debug(
            f"Załadowano regułę sekcji: '{rule.tag}' -> "
            f"'{rule.section_name}'"
        )

    logger.info(
        f"Załadowano {len(config.recipient_rules)} reguł adresatów "
        f"i {len(config.section_rules)} reguł sekcji"
    )
    return config


# ===== Silnik reguł =====

def match_recipient_rule(
    recipients: list,
    rules: list
) -> Optional[RecipientRule]:
    """
    Dopasowuje adresatów do reguł na podstawie NAZWY adresata.
    Sprawdza, czy nazwa wyświetlana adresata zawiera podaną frazę z reguł.

    Args:
        recipients: Lista słowników [{'name': '...', 'email': '...'}]
        rules: Lista reguł RecipientRule

    Returns:
        Dopasowana reguła lub None
    """
    for recipient in recipients:
        display_name = recipient["name"]
        email_addr = recipient["email"]
        
        # Jeżeli nazwa wyświetlana jest pusta (np. mail wysłany na sam adres),
        # jako fallback bierzemy część przed '@' z adresu e-mail lub cały adres.
        search_target = display_name if display_name else email_addr
        search_target_lower = search_target.lower()

        for rule in rules:
            if rule.phrase in search_target_lower:
                logger.info(
                    f"Dopasowano regułę adresata! "
                    f"Nazwa: '{search_target}' zawiera frazę: '{rule.phrase}' -> "
                    f"Projekt GID: {rule.project_gid}, Workspace GID: {rule.workspace_gid}"
                )
                return rule

    # Logowanie wszystkich sprawdzonych nazw w celu ułatwienia debugowania
    checked_names = [r["name"] if r["name"] else r["email"] for r in recipients]
    logger.info(
        f"Brak dopasowania reguły dla nazw adresatów: {checked_names}"
    )
    return None


def match_section_rule(
    body: str,
    rules: list
) -> Optional[SectionRule]:
    """
    Dopasowuje treść e-mail do reguły sekcji.
    Sprawdza PIERWSZE 20 ZNAKÓW treści wiadomości.

    Args:
        body: Pełna treść wiadomości
        rules: Lista reguł SectionRule

    Returns:
        Dopasowana reguła sekcji lub None
    """
    # Sprawdzamy pierwsze 20 znaków
    first_chars = body[:20] if body else ""

    for rule in rules:
        if rule.tag in first_chars:
            logger.info(
                f"Dopasowano regułę sekcji: znaleziono '{rule.tag}' "
                f"w pierwszych 20 znakach -> sekcja '{rule.section_name}'"
            )
            return rule

    logger.info(
        f"Brak dopasowania reguły sekcji dla: '{first_chars}...'"
    )
    return None


# ===== Przetwarzanie e-mail -> zadanie Asana =====

def build_task_description(email_msg: EmailMessage, matched_tag: str = "") -> str:
    """
    Buduje opis zadania na podstawie wiadomości e-mail.
    Dodaje informację o nadawcy na początku opisu.

    Args:
        email_msg: Wiadomość e-mail

    Returns:
        Sformatowany opis zadania
    """
    parts = []

    # Informacja o nadawcy
    parts.append(f"Nadawca: {email_msg.sender_email}")

    if email_msg.sender_name:
        parts.append(f"Nazwa nadawcy: {email_msg.sender_name}")

    parts.append(f"Data: {email_msg.raw_date}")
    parts.append("")  # pusta linia
    parts.append("--- Treść wiadomości ---")
    parts.append("")

    # Treść wiadomości
    body = email_msg.get_body()
      
    # Jeśli przekazano znacznik (np. {B}), usuwamy jego pierwsze wystąpienie
    if body and matched_tag:
        # Usuwamy tag i ewentualne białe znaki (spacje, entery) na początku tekstu
        body = body.replace(matched_tag, "", 1).strip()
        
    parts.append(body if body else "(brak treści)")
    
    return "\n".join(parts)


def process_email_message(
    email_msg: EmailMessage,
    rules: RulesConfig,
    asana: AsanaClient,
    dry_run: bool = False
) -> TaskCreationResult:
    """
    Przetwarza pojedynczą wiadomość e-mail i tworzy zadanie w Asana.

    Proces:
    1. Dopasowanie reguły adresata -> projekt + workspace
    2. Dopasowanie reguły sekcji -> sekcja w projekcie
    3. Utworzenie zadania
    4. Przypisanie do sekcji (jeśli dopasowano)
    5. Dodanie obserwujących (CC)
    6. Przesłanie załączników

    Args:
        email_msg: Wiadomość e-mail
        rules: Konfiguracja reguł
        asana: Klient Asana API
        dry_run: Tryb testowy (bez tworzenia zadań)

    Returns:
        Wynik tworzenia zadania
    """
    result = TaskCreationResult(
        success=False,
        email_subject=email_msg.subject,
        email_sender=email_msg.sender_email
    )

    # --- KROK 1: Dopasowanie reguły adresata ---
    recipient_rule = match_recipient_rule(
        email_msg.get_all_recipients(),
        rules.recipient_rules
    )

    if not recipient_rule:
        result.error_message = (
            f"Brak reguły dla adresatów: {email_msg.get_all_recipients()}. "
            f"Zadanie nie zostanie utworzone."
        )
        logger.warning(result.error_message)
        return result

    result.project_gid = recipient_rule.project_gid

    # --- KROK 2: Dopasowanie reguły sekcji ---
    body = email_msg.get_body()
    section_rule = match_section_rule(body, rules.section_rules)

    matched_tag = ""

    if section_rule:
        result.section_name = section_rule.section_name
        matched_tag = section_rule.tag  # Zapamiętujemy znacznik (np. "{B}")    

    # --- KROK 3: Przygotowanie danych zadania ---
    task_name = email_msg.subject or "(Brak tematu)"
    task_description = build_task_description(email_msg, matched_tag)
    
    # --- KROK 3.5: Pobranie GID nadawcy w Asana (na podstawie adresu e-mail) ---
    assignee_gid = None
    if not dry_run:
        try:
            logger.info(f"Szukanie konta Asana dla nadawcy: {email_msg.sender_email}...")
            assignee_gid = asana.find_user_by_email(
                email_msg.sender_email,
                recipient_rule.workspace_gid
            )
            if assignee_gid:
                logger.info(f"Dopasowano nadawcę do użytkownika Asana GID: {assignee_gid}")
            else:
                logger.warning(
                    f"Nadawca {email_msg.sender_email} nie posiada konta w tej Asanie. "
                    f"Zadanie nie zostanie automatycznie przypisane."
                )
        except Exception as e:
            logger.warning(f"Błąd podczas sprawdzania użytkownika w Asanie: {e}")

    logger.info(f"{'... [DRY RUN] ' if dry_run else ''}Tworzenie zadania:")
    logger.info(f"  Nazwa: {task_name}")
    logger.info(f"  Projekt: {recipient_rule.project_gid}")
    logger.info(f"  Workspace: {recipient_rule.workspace_gid}")
    logger.info(f"  Sekcja: {section_rule.section_name if section_rule else 'brak'}")
    logger.info(f"  Nadawca: {email_msg.sender_email} (GID: {assignee_gid or 'Brak'})")
    logger.info(f"  CC: {email_msg.get_cc_emails()}")
    logger.info(f"  Załączniki: {len(email_msg.attachments)}")

    if dry_run:
        result.success = True
        result.task_name = task_name
        logger.info("[DRY RUN] Zadanie nie zostało utworzone (tryb testowy)")
        return result

    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Tworzenie zadania:")
    logger.info(f"  Nazwa: {task_name}")
    logger.info(f"  Projekt: {recipient_rule.project_gid}")
    logger.info(f"  Workspace: {recipient_rule.workspace_gid}")
    logger.info(f"  Sekcja: {section_rule.section_name if section_rule else 'brak'}")
    logger.info(f"  Nadawca: {email_msg.sender_email}")
    logger.info(f"  CC: {email_msg.get_cc_emails()}")
    logger.info(f"  Załączniki: {len(email_msg.attachments)}")

    if dry_run:
        result.success = True
        result.task_name = task_name
        logger.info("[DRY RUN] Zadanie nie zostało utworzone (tryb testowy)")
        return result

    # --- KROK 4: Utworzenie zadania w Asana ---
    try:
        task = asana.create_task(
            workspace_gid=recipient_rule.workspace_gid,
            project_gid=recipient_rule.project_gid,
            name=task_name,
            notes=task_description,
            assignee=assignee_gid  # <-- Tu przekazujemy bezpieczny GID użytkownika (lub None)
        )

        task_gid = task.get("gid")
        if not task_gid:
            result.error_message = "Nie otrzymano GID zadania z Asana API"
            logger.error(result.error_message)
            return result

        result.task_gid = task_gid
        result.task_name = task_name

    except Exception as e:
        result.error_message = f"Błąd tworzenia zadania: {e}"
        logger.error(result.error_message)
        return result

    # --- KROK 5: Przypisanie do sekcji ---
    if section_rule:
        try:
            section_gid = asana.find_section_gid(
                recipient_rule.project_gid,
                section_rule.section_name
            )
            if section_gid:
                asana.add_task_to_section(task_gid, section_gid)
                logger.info(
                    f"Zadanie przypisane do sekcji: "
                    f"'{section_rule.section_name}'"
                )
            else:
                logger.warning(
                    f"Nie znaleziono sekcji '{section_rule.section_name}' "
                    f"w projekcie {recipient_rule.project_gid}. "
                    f"Zadanie pozostaje bez sekcji."
                )
        except Exception as e:
            logger.warning(f"Błąd przypisywania do sekcji: {e}")

    # --- KROK 6: Dodanie komentarza z adresem nadawcy ---
    try:
        sender_comment = (
            f"Zadanie utworzone z wiadomości e-mail.\n"
            f"Nadawca: {email_msg.sender_email}"
        )
        if email_msg.sender_name:
            sender_comment += f" ({email_msg.sender_name})"

        asana.add_comment(task_gid, sender_comment)
    except Exception as e:
        logger.warning(f"Błąd dodawania komentarza: {e}")

    # --- KROK 7: Dodanie obserwujących (CC) ---
    cc_emails = email_msg.get_cc_emails()
    if cc_emails:
        try:
            asana.add_followers(task_gid, cc_emails)
        except Exception as e:
            logger.warning(f"Błąd dodawania obserwujących: {e}")

    # --- KROK 8: Przesłanie załączników ---
    for attachment in email_msg.attachments:
        try:
            temp_path = attachment.save_to_temp()
            asana.upload_attachment(
                task_gid,
                temp_path,
                filename=attachment.filename
            )
        except Exception as e:
            logger.warning(
                f"Błąd przesyłania załącznika "
                f"'{attachment.filename}': {e}"
            )
        finally:
            attachment.cleanup()

    result.success = True
    logger.info(
        f"Zadanie utworzone pomyślnie: '{task_name}' "
        f"(GID: {task_gid})"
    )
    return result


def process_all_emails(
    config: dict,
    rules: RulesConfig,
    dry_run: bool = False
) -> list:
    """
    Główna funkcja przetwarzająca - odczytuje e-maile i tworzy zadania.

    Args:
        config: Pełna konfiguracja (email + asana)
        rules: Konfiguracja reguł
        dry_run: Tryb testowy

    Returns:
        Lista wyników TaskCreationResult
    """
    results = []

    # Inicjalizacja klienta Asana
    asana = AsanaClient(config["asana"]["personal_access_token"])

    if not dry_run:
        try:
            asana.verify_connection()
        except Exception as e:
            logger.error(f"Nie można połączyć się z Asana: {e}")
            return results

    # Odczyt e-maili
    logger.info("Rozpoczynanie odczytu wiadomości e-mail...")

    with EmailReader(config["email"]) as reader:
        messages = reader.fetch_unread_messages(mark_as_read=not dry_run)

    if not messages:
        logger.info("Brak nowych wiadomości do przetworzenia.")
        return results

    logger.info(f"Pobrano {len(messages)} wiadomości do przetworzenia")

    # Przetwarzanie każdej wiadomości
    for i, email_msg in enumerate(messages, 1):
        logger.info(
            f"\n{'='*60}\n"
            f"Przetwarzanie wiadomości {i}/{len(messages)}: "
            f"'{email_msg.subject}'\n"
            f"{'='*60}"
        )

        try:
            result = process_email_message(
                email_msg, rules, asana, dry_run
            )
            results.append(result)
        except Exception as e:
            logger.error(
                f"Nieoczekiwany błąd przetwarzania wiadomości "
                f"'{email_msg.subject}': {e}"
            )
            results.append(TaskCreationResult(
                success=False,
                email_subject=email_msg.subject,
                email_sender=email_msg.sender_email,
                error_message=str(e)
            ))
        finally:
            # Czyszczenie plików tymczasowych
            email_msg.cleanup_attachments()

    return results


def print_summary(results: list):
    """Wyświetla podsumowanie przetwarzania."""
    if not results:
        print("\nBrak wiadomości do przetworzenia.")
        return

    print(f"\n{'='*60}")
    print("PODSUMOWANIE PRZETWARZANIA")
    print(f"{'='*60}")

    created = [r for r in results if r.success and r.task_gid]
    skipped = [r for r in results if r.success and not r.task_gid]
    failed = [r for r in results if not r.success and r.error_message
              and "Brak reguły" not in r.error_message]
    no_rule = [r for r in results if not r.success
               and "Brak reguły" in (r.error_message or "")]

    print(f"\nŁącznie przetworzonych: {len(results)}")
    print(f"  Utworzone zadania:     {len(created)}")
    print(f"  Pominięte (brak reguły): {len(no_rule)}")
    print(f"  Tryb testowy:          {len(skipped)}")
    print(f"  Błędy:                 {len(failed)}")

    if created:
        print(f"\nUtworzono zadania:")
        for r in created:
            section_info = f" [sekcja: {r.section_name}]" if r.section_name else ""
            print(
                f"  ✓ '{r.task_name}' (GID: {r.task_gid})"
                f"{section_info}"
            )

    if no_rule:
        print(f"\nPominięte (brak reguły adresata):")
        for r in no_rule:
            print(f"  - '{r.email_subject}' od {r.email_sender}")

    if failed:
        print(f"\nBłędy:")
        for r in failed:
            print(f"  ✗ '{r.email_subject}': {r.error_message}")

def apply_env_overrides(config: dict) -> dict:
    """
    Nadpisuje wartości z config.json danymi ze zmiennych środowiskowych.
    Odporna na puste ciągi znaków z GitHub Secrets.
    """
    def parse_int(val):
        return int(val) if str(val).isdigit() else 993

    def parse_bool(val):
        return str(val).lower() in ("true", "1", "yes")

    env_mapping = {
        "EMAIL_IMAP_SERVER": ("email", "imap_server", str),
        "EMAIL_IMAP_PORT":   ("email", "imap_port", parse_int),
        "EMAIL_USERNAME":    ("email", "username", str),
        "EMAIL_PASSWORD":    ("email", "password", str),
        "EMAIL_USE_SSL":     ("email", "use_ssl", parse_bool),
        "EMAIL_MAILBOX":     ("email", "mailbox", str),
        "ASANA_TOKEN":       ("asana", "personal_access_token", str),
    }

    for env_var, (section, key, converter) in env_mapping.items():
        value = os.environ.get(env_var)
        # Bierzemy pod uwagę tylko wartości, które nie są puste
        if value is not None and str(value).strip() != "":
            if section not in config:
                config[section] = {}
            try:
                config[section][key] = converter(value)
                logger.info(f"Nadpisano konfigurację z ENV: {section}.{key}")
            except Exception as e:
                logger.warning(f"Błąd konwersji zmiennej ENV {env_var}: {e}")

    return config

def main():
    """Punkt wejścia programu."""
    parser = argparse.ArgumentParser(
        description="Email-to-Asana: Tworzenie zadań Asana z wiadomości e-mail"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Ścieżka do pliku konfiguracyjnego (domyślnie: config.json)"
    )
    parser.add_argument(
        "--rules",
        default="rules.json",
        help="Ścieżka do pliku reguł (domyślnie: rules.json)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Tryb testowy - nie tworzy zadań, nie oznacza maili"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Szczegółowe logowanie (DEBUG)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        logger.info("TRYB TESTOWY (DRY RUN) - żadne zadania nie będą tworzone")

    try:
        # Ładowanie konfiguracji z pliku + nadpisanie zmiennymi ENV
        if os.path.exists(args.config):
            config = load_json_file(args.config)
        else:
            # Na GitHub Actions plik config.json może nie istnieć
            config = {"email": {}, "asana": {}}
            logger.info("Plik config.json nie znaleziony — używam zmiennych ENV")

        config = apply_env_overrides(config)
        rules = load_rules(args.rules)

        # Walidacja konfiguracji
        if "email" not in config:
            raise ValueError(
                "Brak sekcji 'email' w pliku konfiguracyjnym"
            )
        if "asana" not in config:
            raise ValueError(
                "Brak sekcji 'asana' w pliku konfiguracyjnym"
            )
        if not rules.recipient_rules:
            logger.warning(
                "Brak zdefiniowanych reguł adresatów - "
                "żadne zadania nie zostaną utworzone"
            )

        # Przetwarzanie
        results = process_all_emails(config, rules, dry_run=args.dry_run)

        # Podsumowanie
        print_summary(results)

        # Kod wyjścia
        errors = [r for r in results if not r.success
                  and "Brak reguły" not in (r.error_message or "")]
        sys.exit(1 if errors else 0)

    except FileNotFoundError as e:
        logger.error(f"Nie znaleziono pliku: {e}")
        sys.exit(2)
    except json.JSONDecodeError as e:
        logger.error(f"Błąd parsowania pliku JSON: {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        logger.info("Przerwano przez użytkownika")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()