"""Cliente HTTP fino para a API do Zendesk Support.

Trata autenticação por API token, paginação (cursor e legado) e
re-tentativa em caso de rate limit (HTTP 429) ou erros transitórios (5xx).
"""
import time
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class ZendeskError(Exception):
    """Erro genérico ao falar com a API do Zendesk."""


class ZendeskClient:
    def __init__(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        timeout: int = 30,
        max_retries: int = 4,
    ):
        if not (subdomain and email and api_token):
            raise ValueError(
                "ZENDESK_SUBDOMAIN, ZENDESK_EMAIL e ZENDESK_API_TOKEN são obrigatórios."
            )
        self.base_url = f"https://{subdomain}.zendesk.com"
        self.timeout = timeout
        self.max_retries = max_retries

        self.session = requests.Session()
        # Autenticação por API token: usuário é "<email>/token", senha é o token.
        self.session.auth = (f"{email}/token", api_token)
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------ #
    # Baixo nível
    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        if url.startswith("/"):
            url = self.base_url + url

        attempt = 0
        while True:
            attempt += 1
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)

            # Rate limit: respeita o Retry-After.
            if resp.status_code == 429 and attempt <= self.max_retries:
                wait = int(resp.headers.get("Retry-After", "10"))
                logger.warning("Rate limit (429). Aguardando %ss…", wait)
                time.sleep(wait)
                continue

            # Erros transitórios de servidor: backoff exponencial.
            if resp.status_code >= 500 and attempt <= self.max_retries:
                wait = min(2 ** attempt, 30)
                logger.warning("Erro %s do Zendesk. Retentando em %ss…", resp.status_code, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                raise ZendeskError(f"Não encontrado (404): {url}")

            if not resp.ok:
                raise ZendeskError(
                    f"Zendesk respondeu {resp.status_code}: {resp.text[:500]}"
                )

            return resp.json() if resp.content else {}

    def _get(self, url: str, params: Optional[dict] = None) -> Dict[str, Any]:
        return self._request("GET", url, params=params)

    def _paginate(self, url: str, list_key: str, params: Optional[dict] = None) -> List[dict]:
        """Coleta todos os itens seguindo paginação cursor (meta.has_more /
        links.next) ou legado (next_page)."""
        items: List[dict] = []
        next_url: Optional[str] = url
        next_params = dict(params or {})

        while next_url:
            data = self._get(next_url, params=next_params)
            items.extend(data.get(list_key, []))

            meta = data.get("meta") or {}
            links = data.get("links") or {}
            if meta.get("has_more") and links.get("next"):
                next_url = links["next"]
                next_params = None  # a URL já vem com os parâmetros embutidos
            elif data.get("next_page"):
                next_url = data["next_page"]
                next_params = None
            else:
                next_url = None
        return items

    # ------------------------------------------------------------------ #
    # Alto nível
    # ------------------------------------------------------------------ #
    def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
        data = self._get(f"/api/v2/tickets/{ticket_id}.json")
        return data["ticket"]

    def get_user(self, user_id: int) -> Dict[str, Any]:
        data = self._get(f"/api/v2/users/{user_id}.json")
        return data["user"]

    def get_ticket_audits(self, ticket_id: int) -> List[dict]:
        """Histórico completo do ticket, em ordem cronológica crescente."""
        audits = self._paginate(
            f"/api/v2/tickets/{ticket_id}/audits.json",
            list_key="audits",
            params={"page[size]": 100},
        )
        audits.sort(key=lambda a: a.get("created_at", ""))
        return audits

    def get_custom_statuses(self) -> List[dict]:
        data = self._get("/api/v2/custom_statuses.json")
        return data.get("custom_statuses", [])

    def get_ticket_field(self, field_id: int) -> Dict[str, Any]:
        data = self._get(f"/api/v2/ticket_fields/{field_id}.json")
        return data["ticket_field"]

    def update_ticket_custom_field(self, ticket_id: int, field_id: int, value) -> Dict[str, Any]:
        """Grava um valor em um campo customizado do ticket (PUT)."""
        payload = {"ticket": {"custom_fields": [{"id": field_id, "value": value}]}}
        data = self._request(
            "PUT",
            f"/api/v2/tickets/{ticket_id}.json",
            json=payload,
        )
        return data.get("ticket", {})

    def update_ticket_tags(
        self,
        ticket_id: int,
        tags: List[str],
        updated_stamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Substitui tags pela lista informada, preservando a união calculada."""
        ticket = {"tags": sorted(set(tags))}
        if updated_stamp:
            ticket["safe_update"] = True
            ticket["updated_stamp"] = updated_stamp
        data = self._request(
            "PUT",
            f"/api/v2/tickets/{ticket_id}.json",
            json={"ticket": ticket},
        )
        return data.get("ticket", {})

    def add_private_comment_with_tags(
        self,
        ticket_id: int,
        body: str,
        tags: List[str],
        updated_stamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adiciona observação interna e grava tags de controle no mesmo update."""
        ticket = {
            "comment": {"body": body, "public": False},
            "tags": sorted(set(tags)),
        }
        if updated_stamp:
            ticket["safe_update"] = True
            ticket["updated_stamp"] = updated_stamp
        data = self._request(
            "PUT",
            f"/api/v2/tickets/{ticket_id}.json",
            json={"ticket": ticket},
        )
        return data.get("ticket", {})

    def search_tickets(self, query: str) -> List[dict]:
        """Busca tickets via Search API. Ex.: 'status:pending tags:prestador'."""
        return self._paginate(
            "/api/v2/search.json",
            list_key="results",
            params={"query": query},
        )
