"""
Moduł odpowiedzialny za odczyt wiadomości e-mail z serwera IMAP.
Obsługuje pobieranie nowych (nieprzeczytanych) wiadomości wraz z załącznikami.
"""

import imaplib
import email
from email.message import Message  # Poprawiony import
from email.header import decode_header
from email.utils import parseaddr
import os
import tempfile
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EmailAttachment:
    """Reprezentuje załącznik wiadomości e-mail."""
    filename: str
    content: bytes
    content_type: str
    temp_path: Optional[str] = None

    def save_to_temp(self) -> str:
        """Zapisuje załącznik do pliku tymczasowego i zwraca ścieżkę."""
        temp_dir = tempfile.mkdtemp(prefix="email_asana_")
        safe_filename = self.filename.replace("/", "_").replace("\\", "_")
        self.temp_path = os.path.join(temp_dir, safe_filename)

        with open(self.temp_path, "wb") as f:
            f.write(self.content)

        logger.debug(f"Załącznik zapisany tymczasowo: {self.temp_path}")
        return self.temp_path

    def cleanup(self):
        """Usuwa plik tymczasowy załącznika."""
        if self.temp_path and os.path.exists(self.temp_path):
            os.remove(self.temp_path)
            temp_dir = os.path.dirname(self.temp_path)
            if os.path.isdir(temp_dir) and not os.listdir(temp_dir):
                os.rmdir(temp_dir)
            logger.debug(f"Usunięto plik tymczasowy: {self.temp_path}")


@dataclass
class EmailMessage:
    """Reprezentuje odczytaną wiadomość e-mail."""
    message_id: str
    subject: str
    sender_email: str
    sender_name: str
    recipients: list  # lista słowników [{'name': '...', 'email': '...'}]
    cc_recipients: list  # lista słowników [{'name': '...', 'email': '...'}]
    body_text: str
    body_html: str
    attachments: list = field(default_factory=list)
    raw_date: str = ""

    def get_all_recipients(self) -> list:
        """Zwraca pełną listę adresatów TO."""
        return self.recipients

    def get_cc_emails(self) -> list:
        """Zwraca listę samych POPRAWNYCH adresów e-mail z CC."""
        import re
        # Proste wyrażenie regularne do walidacji adresu e-mail
        email_regex = re.compile(r"[^@]+@[^@]+\.[^@]+")
        
        valid_emails = []
        for r in self.cc_recipients:
            email_addr = r.get("email", "").strip()
            if email_addr and email_regex.match(email_addr):
                valid_emails.append(email_addr)
            else:
                logger.warning(
                    f"Pominięto niepoprawny adres CC (obserwatora): '{email_addr}'"
                )
        return valid_emails

    def get_body(self) -> str:
        """Zwraca treść wiadomości - preferuje tekst, fallback na HTML."""
        if self.body_text:
            return self.body_text.strip()
        if self.body_html:
            return self.body_html.strip()
        return ""

    def cleanup_attachments(self):
        """Czyści wszystkie pliki tymczasowe załączników."""
        for att in self.attachments:
            att.cleanup()


class EmailReader:
    """
    Klasa do odczytu wiadomości e-mail z serwera IMAP.
    """

    def __init__(self, config: dict):
        self.server = config["imap_server"]
        self.port = config.get("imap_port", 993)
        self.username = config["username"]
        self.password = config["password"]
        self.use_ssl = config.get("use_ssl", True)
        self.mailbox = config.get("mailbox", "INBOX")
        self.connection = None

    def connect(self):
        try:
            if self.use_ssl:
                self.connection = imaplib.IMAP4_SSL(self.server, self.port)
            else:
                self.connection = imaplib.IMAP4(self.server, self.port)

            self.connection.login(self.username, self.password)
            logger.info(f"Połączono z serwerem {self.server} jako {self.username}")
        except imaplib.IMAP4.error as e:
            logger.error(f"Błąd logowania do serwera IMAP: {e}")
            raise
        except Exception as e:
            logger.error(f"Błąd połączenia z serwerem IMAP: {e}")
            raise

    def disconnect(self):
        if self.connection:
            try:
                self.connection.logout()
                logger.info("Rozłączono z serwerem IMAP")
            except Exception as e:
                logger.warning(f"Błąd podczas rozłączania: {e}")

    def _decode_header_value(self, value: str) -> str:
        if value is None:
            return ""

        decoded_parts = decode_header(value)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                charset = charset or "utf-8"
                try:
                    result.append(part.decode(charset, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(part)

        return " ".join(result)

    def _parse_email_addresses(self, header_value: str) -> list:
        """
        Parsuje nagłówek z adresami e-mail za pomocą standardowej biblioteki.
        Poprawnie radzi sobie z przecinkami w nazwach (np. "Klimkiewicz, Grzegorz").
        """
        if not header_value:
            return []

        from email.utils import getaddresses

        decoded = self._decode_header_value(header_value)
        addresses = []

        # getaddresses automatycznie radzi sobie z formatowaniem adresów e-mail
        parsed_addresses = getaddresses([decoded])

        for name, email_addr in parsed_addresses:
            email_addr_clean = email_addr.strip().lower()
            name_clean = name.strip()

            # Zapisujemy tylko jeśli rzeczywiście wyodrębniono adres e-mail
            if email_addr_clean and "@" in email_addr_clean:
                addresses.append({
                    "name": name_clean,
                    "email": email_addr_clean
                })
                logger.debug(f"Wyodrębniono adres: Name='{name_clean}', Email='{email_addr_clean}'")

        return addresses

    def _extract_body(self, msg: Message) -> tuple:
        """Wyodrębnia treść wiadomości (tekst i HTML)."""
        body_text = ""
        body_html = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    continue

                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue

                    charset = part.get_content_charset() or "utf-8"
                    try:
                        decoded_payload = payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        decoded_payload = payload.decode("utf-8", errors="replace")

                    if content_type == "text/plain" and not body_text:
                        body_text = decoded_payload
                    elif content_type == "text/html" and not body_html:
                        body_html = decoded_payload
                except Exception as e:
                    logger.warning(f"Błąd wyodrębniania treści: {e}")
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        decoded = payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        decoded = payload.decode("utf-8", errors="replace")

                    if msg.get_content_type() == "text/html":
                        body_html = decoded
                    else:
                        body_text = decoded
            except Exception as e:
                logger.warning(f"Błąd wyodrębniania treści: {e}")

        return body_text, body_html

    def _extract_attachments(self, msg: Message) -> list:
        """Wyodrębnia załączniki z wiadomości e-mail."""
        attachments = []

        if not msg.is_multipart():
            return attachments

        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" not in content_disposition:
                filename = part.get_filename()
                if not filename:
                    continue

            filename = part.get_filename()
            if filename:
                filename = self._decode_header_value(filename)
                content = part.get_payload(decode=True)

                if content:
                    attachment = EmailAttachment(
                        filename=filename,
                        content=content,
                        content_type=part.get_content_type()
                    )
                    attachments.append(attachment)
                    logger.debug(f"Znaleziono załącznik: {filename}")

        return attachments

    def _parse_message(self, msg_data: bytes) -> EmailMessage:
        """Parsuje surowe dane wiadomości."""
        msg = email.message_from_bytes(msg_data)

        # Wyodrębnienie nagłówków
        subject = self._decode_header_value(msg.get("Subject", ""))
        from_header = msg.get("From", "")
        sender_name, sender_email = parseaddr(self._decode_header_value(from_header))

        # Adresaci TO i CC
        to_addresses = self._parse_email_addresses(msg.get("To", ""))
        cc_addresses = self._parse_email_addresses(msg.get("Cc", ""))

        # Treść
        body_text, body_html = self._extract_body(msg)

        # Załączniki
        attachments = self._extract_attachments(msg)

        return EmailMessage(
            message_id=msg.get("Message-ID", ""),
            subject=subject,
            sender_email=sender_email.lower() if sender_email else "",
            sender_name=sender_name,
            recipients=to_addresses,
            cc_recipients=cc_addresses,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            raw_date=msg.get("Date", "")
        )

    def fetch_unread_messages(self, mark_as_read: bool = True) -> list:
        if not self.connection:
            self.connect()

        messages = []
        try:
            status, data = self.connection.select(self.mailbox)
            if status != "OK":
                logger.error(f"Nie można otworzyć skrzynki: {self.mailbox}")
                return messages

            status, msg_ids = self.connection.search(None, "UNSEEN")
            if status != "OK":
                return messages

            msg_id_list = msg_ids[0].split()
            if not msg_id_list:
                logger.info("Brak nowych wiadomości")
                return messages

            logger.info(f"Znaleziono {len(msg_id_list)} nowych wiadomości")

            for msg_id in msg_id_list:
                try:
                    status, msg_data = self.connection.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    email_msg = self._parse_message(raw_email)
                    messages.append(email_msg)

                    logger.info(f"Pobrano: '{email_msg.subject}'")

                    if mark_as_read:
                        self.connection.store(msg_id, "+FLAGS", "\\Seen")

                except Exception as e:
                    logger.error(f"Błąd przetwarzania wiadomości {msg_id}: {e}")

        except Exception as e:
            logger.error(f"Błąd pobierania wiadomości: {e}")

        return messages

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False