"""Референсы клиента: скачать из Instagram сразу (ссылки CDN истекают) и загрузить в Files API.

В историю модели попадает блок image с file_id: он не меняется между ходами,
поэтому история остаётся «только дописываемой», а картинка читается из кэша промпта.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx2 as httpx

log = logging.getLogger(__name__)

SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_BYTES = 5 * 1024 * 1024  # лимит Claude API на одну картинку


class ImageStore:
    def __init__(self, client: Any, retention_days: int, http: httpx.AsyncClient | None = None):
        self.client = client
        self.expires_in = retention_days * 86400
        self.http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def download(self, url: str) -> tuple[bytes, str] | None:
        resp = await self.http.get(url)
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in SUPPORTED_TYPES:
            log.info("skip attachment with content-type %s", mime)
            return None
        if len(resp.content) > MAX_BYTES:
            log.info("skip attachment of %d bytes", len(resp.content))
            return None
        return resp.content, mime

    async def upload(self, data: bytes, mime: str, name: str) -> str:
        file = (name, data, mime)
        try:
            # Файл сам удалится по истечении срока хранения; purge удаляет его и явно.
            meta = await self.client.files.upload(file=file, expires_in_seconds=self.expires_in)
        except anthropic.BadRequestError:
            log.warning("upload with expiry rejected, retrying without it")
            meta = await self.client.files.upload(file=file)
        return meta.id

    async def ingest(self, url: str, name: str) -> str | None:
        """Возвращает file_id или None, если картинку не удалось получить."""
        got = await self.download(url)
        if not got:
            return None
        data, mime = got
        return await self.upload(data, mime, name)

    async def delete(self, file_id: str) -> None:
        try:
            await self.client.files.delete(file_id)
        except anthropic.NotFoundError:
            pass  # уже истёк или удалён
