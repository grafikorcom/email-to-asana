"""
Moduł klienta Asana API.
Obsługuje tworzenie zadań, dodawanie do sekcji, dodawanie obserwujących
i przesyłanie załączników.
"""

import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ASANA_BASE_URL = "https://app.asana.com/api/1.0"


class AsanaAPIError(Exception):
    """Wyjątek dla błędów Asana API."""
    def __init__(self, message: str, status_code: int = None, response: dict = None):
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(self.message)


class AsanaClient:
    """
    Klient Asana API obsługujący tworzenie zadań z pełnym zestawem funkcji:
    - Tworzenie zadania z nazwą, opisem, projektem, workspace
    - Przypisywanie do sekcji
    - Dodawanie obserwujących (followers)
    - Przesyłanie załączników
    - Ustawianie pól niestandardowych
    """

    def __init__(self, personal_access_token: str):
        """
        Inicjalizuje klienta Asana.

        Args:
            personal_access_token: Token osobisty Asana API
        """
        self.token = personal_access_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict = None,
        files: dict = None,
        data: dict = None
    ) -> dict:
        """
        Wykonuje zapytanie do Asana API.

        Args:
            method: Metoda HTTP (GET, POST, PUT, DELETE)
            endpoint: Endpoint API (bez bazowego URL)
            json_data: Dane JSON do wysłania
            files: Pliki do przesłania
            data: Dane formularza

        Returns:
            Odpowiedź jako słownik

        Raises:
            AsanaAPIError: Przy błędzie API
        """
        url = f"{ASANA_BASE_URL}/{endpoint.lstrip('/')}"

        try:
            if files:
                # Przy przesyłaniu plików nie używamy Content-Type w headers
                headers = {"Authorization": f"Bearer {self.token}"}
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    files=files,
                    data=data
                )
            else:
                response = self.session.request(
                    method=method,
                    url=url,
                    json=json_data
                )

            if response.status_code in (200, 201):
                return response.json() if response.text else {}

            # Obsługa błędów
            try:
                error_body = response.json()
            except ValueError:
                error_body = {"message": response.text}

            error_msg = (
                f"Asana API error {response.status_code}: "
                f"{error_body.get('errors', error_body)}"
            )
            logger.error(error_msg)
            raise AsanaAPIError(
                error_msg,
                status_code=response.status_code,
                response=error_body
            )

        except requests.exceptions.RequestException as e:
            logger.error(f"Błąd połączenia z Asana API: {e}")
            raise AsanaAPIError(f"Błąd połączenia: {e}")

    def verify_connection(self) -> dict:
        """Weryfikuje połączenie z Asana API (pobiera dane użytkownika)."""
        result = self._make_request("GET", "/users/me")
        user = result.get("data", {})
        logger.info(
            f"Połączono z Asana jako: {user.get('name')} "
            f"({user.get('email')})"
        )
        return user

    def get_project_sections(self, project_gid: str) -> list:
        """
        Pobiera listę sekcji projektu.

        Args:
            project_gid: GID projektu

        Returns:
            Lista sekcji [{"gid": "...", "name": "..."}, ...]
        """
        result = self._make_request(
            "GET",
            f"/projects/{project_gid}/sections"
        )
        sections = result.get("data", [])
        logger.debug(
            f"Sekcje projektu {project_gid}: "
            f"{[s['name'] for s in sections]}"
        )
        return sections

    def find_section_gid(
        self,
        project_gid: str,
        section_name: str
    ) -> Optional[str]:
        """
        Znajduje GID sekcji po nazwie w danym projekcie.
        """
        sections = self.get_project_sections(project_gid)
        available_sections = []

        target_name_clean = section_name.strip().lower()

        for section in sections:
            # Usuwamy dwukropek z końca nazwy (częsty w Asanie) i białe znaki
            asana_name_clean = section["name"].strip().rstrip(":").strip()
            available_sections.append(asana_name_clean)

            if asana_name_clean.lower() == target_name_clean:
                logger.debug(
                    f"Znaleziono sekcję '{section_name}': {section['gid']}"
                )
                return section["gid"]

        # Jeśli nie znaleziono, logujemy dostępne sekcje, aby ułatwić konfigurację rules.json
        logger.warning(
            f"Nie znaleziono sekcji '{section_name}' w projekcie {project_gid}."
        )
        logger.warning(
            f"Dostępne sekcje w tym projekcie w Asanie to: {available_sections}"
        )
        return None

    def create_task(
        self,
        workspace_gid: str,
        project_gid: str,
        name: str,
        notes: str = "",
        html_notes: str = None,
        assignee: str = None,
        custom_fields: dict = None
    ) -> dict:
        """
        Tworzy nowe zadanie w Asana. Posiada fallback, jeśli assignee (e-mail)
        nie istnieje w danej przestrzeni Asana.
        """
        task_data = {
            "data": {
                "workspace": workspace_gid,
                "projects": [project_gid],
                "name": name,
                "notes": notes,
            }
        }

        if html_notes:
            task_data["data"]["html_notes"] = html_notes

        if assignee:
            task_data["data"]["assignee"] = assignee

        if custom_fields:
            task_data["data"]["custom_fields"] = custom_fields

        try:
            result = self._make_request("POST", "/tasks", json_data=task_data)
            task = result.get("data", {})
            logger.info(f"Utworzono zadanie: '{name}' (GID: {task.get('gid')})")
            return task
        except AsanaAPIError as e:
            # Jeśli błąd wynika z braku użytkownika o tym e-mailu jako assignee
            if assignee and e.status_code == 400:
                logger.warning(
                    f"Nie można przypisać użytkownika '{assignee}' do zadania. "
                    f"Prawdopodobnie ten adres e-mail nie ma konta w tej przestrzeni Asana. "
                    f"Tworzę zadanie bez osoby odpowiedzialnej..."
                )
                # Fallback: ponowna próba, ale bez pola assignee
                task_data["data"].pop("assignee", None)
                result = self._make_request("POST", "/tasks", json_data=task_data)
                return result.get("data", {})
            else:
                raise

    def add_task_to_section(self, task_gid: str, section_gid: str) -> dict:
        """
        Dodaje zadanie do sekcji.

        Args:
            task_gid: GID zadania
            section_gid: GID sekcji

        Returns:
            Odpowiedź API
        """
        data = {
            "data": {
                "task": task_gid
            }
        }

        result = self._make_request(
            "POST",
            f"/sections/{section_gid}/addTask",
            json_data=data
        )
        logger.info(
            f"Zadanie {task_gid} dodane do sekcji {section_gid}"
        )
        return result

    def add_followers(self, task_gid: str, follower_emails: list) -> dict:
        """
        Dodaje obserwujących do zadania na podstawie adresów e-mail.

        Args:
            task_gid: GID zadania
            follower_emails: Lista adresów e-mail obserwujących

        Returns:
            Odpowiedź API
        """
        if not follower_emails:
            return {}

        data = {
            "data": {
                "followers": follower_emails
            }
        }

        try:
            result = self._make_request(
                "POST",
                f"/tasks/{task_gid}/addFollowers",
                json_data=data
            )
            logger.info(
                f"Dodano obserwujących do zadania {task_gid}: "
                f"{follower_emails}"
            )
            return result
        except AsanaAPIError as e:
            # Nie przerywamy procesu jeśli nie można dodać obserwujących
            # (np. użytkownik nie istnieje w Asana)
            logger.warning(
                f"Nie udało się dodać obserwujących "
                f"{follower_emails}: {e}"
            )
            return {}

    def add_comment(self, task_gid: str, text: str) -> dict:
        """
        Dodaje komentarz do zadania.

        Args:
            task_gid: GID zadania
            text: Treść komentarza

        Returns:
            Odpowiedź API
        """
        data = {
            "data": {
                "text": text
            }
        }

        result = self._make_request(
            "POST",
            f"/tasks/{task_gid}/stories",
            json_data=data
        )
        logger.debug(f"Dodano komentarz do zadania {task_gid}")
        return result

    def upload_attachment(
        self,
        task_gid: str,
        file_path: str,
        filename: str = None
    ) -> dict:
        """
        Przesyła załącznik do zadania.

        Args:
            task_gid: GID zadania
            file_path: Ścieżka do pliku
            filename: Opcjonalna nazwa pliku

        Returns:
            Dane przesłanego załącznika
        """
        import os

        if not os.path.exists(file_path):
            logger.error(f"Plik nie istnieje: {file_path}")
            return {}

        actual_filename = filename or os.path.basename(file_path)

        with open(file_path, "rb") as f:
            files = {
                "file": (actual_filename, f)
            }
            data = {
                "parent": task_gid
            }

            result = self._make_request(
                "POST",
                f"/attachments",
                files=files,
                data=data
            )

        attachment = result.get("data", {})
        logger.info(
            f"Przesłano załącznik '{actual_filename}' "
            f"do zadania {task_gid}"
        )
        return attachment

    def find_user_by_email(
        self,
        email_address: str,
        workspace_gid: str
    ) -> Optional[str]:
        """
        Znajduje unikalny GID użytkownika Asana na podstawie adresu e-mail.
        """
        try:
            # Asana pozwala pobrać użytkownika bezpośrednio przekazując jego e-mail w ścieżce
            result = self._make_request(
                "GET",
                f"/users/{email_address}"
            )
            user = result.get("data", {})
            if user:
                logger.debug(
                    f"Znaleziono użytkownika w Asana: {user.get('name')} "
                    f"({email_address}) -> GID: {user.get('gid')}"
                )
                return user.get("gid")
        except AsanaAPIError as e:
            logger.debug(
                f"Użytkownik o adresie '{email_address}' nie został znaleziony w Asana: {e}"
            )
        
        return None