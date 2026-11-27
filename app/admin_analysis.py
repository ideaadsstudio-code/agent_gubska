"""Страница /admin/analysis: анализ переписки аккаунта и черновики базы знаний.

Сервер сам забирает последние диалоги через API (токен страницы уже есть), обезличивает их,
оставляет только тату-диалоги и пишет черновики. Сырая переписка нигде не сохраняется:
в базе остаются только черновики, статистика и список возможных личных контактов (ники).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any

from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .classifier import TopicClassifier
from .config import Settings
from .history_analysis import Message, Thread, analyze_threads, estimate, find_owner, read_export
from .service import Service
from .telegram import esc

log = logging.getLogger(__name__)

STATE_KEY = "analysis:latest"
MAX_UPLOAD_BYTES = 2 * 1024**3


def threads_from_api(conversations: list[dict], owner_id: str, owner_name: str) -> list[Thread]:
    threads = []
    for conv in conversations:
        msgs = conv.get("messages", [])
        others = sorted({m["from_username"] or m["from_id"] for m in msgs if m["from_id"] != owner_id})
        messages = [
            Message(sender=owner_name if m["from_id"] == owner_id else (m["from_username"] or m["from_id"]),
                    text=m["text"], ts=m["ts"])
            for m in sorted(msgs, key=lambda m: m["ts"])
        ]
        path = others[0] if others else "unknown"
        threads.append(Thread(path=path, participants=[owner_name, *others], messages=messages))
    return threads


class AnalysisRunner:
    def __init__(self, settings: Settings, service: Service):
        self.settings = settings
        self.service = service
        self.running = False

    def state(self) -> dict[str, Any]:
        raw = self.service.db.kv_get(STATE_KEY)
        return json.loads(raw) if raw else {"status": "idle"}

    def save(self, **fields: Any) -> None:
        state = self.state()
        state.update(fields, updated_at=time.time())
        self.service.db.kv_set(STATE_KEY, json.dumps(state, ensure_ascii=False))

    async def run(self, max_conversations: int, max_usd: float) -> None:
        """Источник — переписка через API Instagram."""
        async def load() -> tuple[list[Thread], str]:
            self.save(step="Загрузка диалогов из Instagram")
            ig = self.service.ig
            owner_id, owner_name = await ig.own_account()
            owner_name = owner_name or "artist"
            conversations = await ig.fetch_history(max_conversations, progress=lambda m: self.save(step=m))
            return [t for t in threads_from_api(conversations, owner_id, owner_name) if t.messages], owner_name

        await self._run(load, max_usd, source="api")

    async def run_export(self, path: Path, max_conversations: int, max_usd: float) -> None:
        """Источник — ZIP-выгрузка из Instagram (Центр аккаунтов → Экспорт информации → Сообщения, JSON).
        Файл удаляется сразу после разбора."""
        async def load() -> tuple[list[Thread], str]:
            self.save(step="Разбор выгрузки")
            try:
                threads = await asyncio.to_thread(read_export, path)
            except zipfile.BadZipFile:
                raise ValueError("Файл не похож на ZIP-архив выгрузки Instagram")
            finally:
                path.unlink(missing_ok=True)
            owner = find_owner(threads)
            threads = [t for t in threads if t.messages]
            threads.sort(key=lambda t: t.messages[-1].ts, reverse=True)
            return threads[:max_conversations], owner

        await self._run(load, max_usd, source="export")

    async def _run(self, load, max_usd: float, source: str) -> None:
        if self.running:
            return
        self.running = True
        try:
            self.save(status="running", started_at=time.time(), error="", source=source)
            threads, owner_name = await load()
            model = self.settings.analysis_model
            tokens, usd = estimate(threads, owner_name, model)
            self.save(conversations=len(threads), estimate_usd=round(usd, 2), account=owner_name)
            if usd > max_usd:
                self.save(status="too_expensive", step=f"Оценка ${usd:.2f} больше лимита ${max_usd:.2f}")
                return
            client = self.service.agent.client
            classifier = self.service.classifier or TopicClassifier(client, self.settings.classifier_model)
            result = await analyze_threads(threads, owner_name, client, classifier,
                                           progress=lambda m: self.save(step=m), model=model)
            if result["cost_usd"]:
                self.service.db.record_usage(None, f"{model} (analysis)", 0, 0, 0, 0, result["cost_usd"])
            self.save(status="done", step="Готово", finished_at=time.time(), **{
                k: result.get(k) for k in ("tattoo", "personal", "personal_contacts", "batches", "cost_usd",
                                           "knowledge_base", "style_guide", "test_cases")
            })
            await self.service.alert(
                f"📚 Анализ переписки @{esc(owner_name)} готов: тату-диалогов {result['tattoo']}, "
                f"прочих {result['personal']}, потрачено ${result['cost_usd']:.2f}. Черновики на странице /admin/analysis."
            )
        except Exception as e:
            log.exception("analysis failed")
            detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            self.save(status="error", error=detail[:500])
            await self.service.alert(f"Анализ переписки не удался: {esc(detail)}")
        finally:
            self.running = False


def _page(body: str) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Анализ переписки</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--accent:#0a66ff;--border:#ddd;--warn:#b25000}}
@media (prefers-color-scheme: dark){{:root{{--bg:#121214;--card:#1c1c1f;--text:#f2f2f3;--muted:#9a9aa1;--accent:#3a82ff;--border:#333;--warn:#ffb067}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}}
.muted{{color:var(--muted)}} .warn{{color:var(--warn)}}
textarea{{width:100%;box-sizing:border-box;min-height:320px;font:13px/1.45 ui-monospace,monospace;padding:10px;
border-radius:10px;border:1px solid var(--border);background:var(--bg);color:var(--text)}}
button,input[type=number]{{font:inherit;border-radius:10px;padding:8px 14px}}
button{{background:var(--accent);color:#fff;border:0;cursor:pointer}}
input[type=number]{{border:1px solid var(--border);background:var(--bg);color:var(--text);width:110px}}
label{{margin-right:12px}} h2{{margin-top:0}}
</style></head><body><main>{body}</main></body></html>"""


def build_router(settings: Settings, service: Service) -> APIRouter:
    router = APIRouter()
    runner = AnalysisRunner(settings, service)

    def check(token: str) -> None:
        if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(status_code=403)

    @router.get("/admin/analysis", response_class=HTMLResponse)
    async def page(token: str = ""):
        check(token)
        st = runner.state()
        status = st.get("status", "idle")
        parts = ["<h1>Анализ переписки и черновики базы знаний</h1>"]
        form = f"""<form method="post" action="/admin/analysis/start">
<input type="hidden" name="token" value="{esc(token)}">
<label>Диалогов, не больше <input type="number" name="max_conversations" value="300" min="10" max="2000"></label>
<label>Лимит расходов, $ <input type="number" name="max_usd" value="15" min="1" max="200" step="1"></label>
<button type="submit">Запустить анализ</button></form>
<p class="muted">Сервер загрузит последние диалоги (по ~20 последних сообщений в каждом), уберёт имена, ники, телефоны
и ссылки, отбросит личные диалоги и составит черновики. Сырая переписка не сохраняется.
Если оценка расходов выше лимита, анализ остановится до вызова модели.</p>"""
        if status == "running" or runner.running:
            parts.append(f"<div class='card'><h2>Идёт анализ…</h2><p>{esc(st.get('step', ''))}</p>"
                         "<p class='muted'>Страница обновится сама.</p></div>"
                         "<script>setTimeout(()=>location.reload(),5000)</script>")
        else:
            if status == "error":
                parts.append(f"<div class='card'><p class='warn'>Ошибка: {esc(st.get('error', ''))}</p></div>")
            if status == "too_expensive":
                parts.append(f"<div class='card'><p class='warn'>{esc(st.get('step', ''))}. "
                             "Уменьшите число диалогов или поднимите лимит.</p></div>")
            parts.append(f"<div class='card'><h2>Из Instagram через API</h2>{form}</div>")
            parts.append(f"""<div class='card'><h2>Из ZIP-выгрузки Instagram</h2>
<p class="muted">Если API не отдаёт переписку (большая история, таймауты Meta), загрузите выгрузку: Instagram мастера →
Центр аккаунтов → Ваша информация и разрешения → Экспорт информации → только «Сообщения», за всё время, формат JSON,
низкое качество медиа. Архив разбирается на сервере и сразу удаляется.</p>
<input type="file" id="zip" accept=".zip,application/zip">
<label>Диалогов, не больше <input type="number" id="zmax" value="300" min="10" max="2000"></label>
<label>Лимит, $ <input type="number" id="zusd" value="15" min="1" max="200"></label>
<button type="button" id="up">Загрузить и запустить</button>
<p id="upstatus" class="muted"></p>
<script>
document.getElementById("up").onclick = () => {{
  const f = document.getElementById("zip").files[0];
  const st = document.getElementById("upstatus");
  if (!f) {{ st.textContent = "Выберите файл .zip"; return; }}
  const q = new URLSearchParams({{token: {json.dumps(token)}, max_conversations: document.getElementById("zmax").value,
                                  max_usd: document.getElementById("zusd").value}});
  const x = new XMLHttpRequest();
  x.open("POST", "/admin/analysis/upload?" + q.toString());
  x.setRequestHeader("Content-Type", "application/zip");
  x.upload.onprogress = (e) => {{ if (e.lengthComputable) st.textContent = "Загрузка: " + Math.round(e.loaded / e.total * 100) + "%"; }};
  x.onload = () => {{ if (x.status < 300) location.reload(); else st.textContent = "Ошибка загрузки: " + x.status + " " + x.responseText; }};
  x.onerror = () => {{ st.textContent = "Сеть оборвалась, попробуйте ещё раз"; }};
  x.send(f);
}};
</script></div>""")
        if status == "done":
            finished = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(st.get("finished_at", 0)))
            parts.append(f"""<div class='card'><h2>Результат от {finished}, @{esc(st.get('account', ''))}</h2>
<p>Диалогов загружено: {st.get('conversations', 0)}. Про тату: {st.get('tattoo', 0)}, прочих: {st.get('personal', 0)}.
Потрачено: ${st.get('cost_usd', 0):.2f}.</p>
<p class="muted">Это черновики: проверьте цены и условия с мастером, прежде чем бот начнёт ими пользоваться.</p></div>""")
            for title, key in (("Черновик базы знаний", "knowledge_base"), ("Стиль мастера", "style_guide"),
                               ("Тестовые вопросы", "test_cases")):
                parts.append(f"<div class='card'><h2>{title}</h2><textarea readonly>{esc(st.get(key) or '')}</textarea></div>")
            contacts = st.get("personal_contacts") or []
            parts.append("<div class='card'><h2>Возможные личные контакты</h2><p class='muted'>Диалоги не про тату. "
                         "Проверьте список и отправьте нужные в Telegram командой /personal @ник.</p>"
                         f"<textarea readonly style='min-height:160px'>{esc(chr(10).join('@' + c for c in contacts))}</textarea></div>")
        return _page("".join(parts))

    @router.post("/admin/analysis/start")
    async def start(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        token = form.get("token", "")
        check(token)
        try:
            max_conversations = int(form.get("max_conversations", 300))
            max_usd = float(form.get("max_usd", 15))
        except ValueError:
            raise HTTPException(status_code=400, detail="bad numbers")
        if not runner.running:
            service._spawn(runner.run(max(10, min(max_conversations, 2000)), max(1.0, min(max_usd, 200.0))))
        return RedirectResponse(f"/admin/analysis?token={quote(token)}", status_code=303)

    @router.post("/admin/analysis/upload")
    async def upload(request: Request, token: str = "", max_conversations: int = 300, max_usd: float = 15):
        check(token)
        if runner.running:
            raise HTTPException(status_code=409, detail="Анализ уже идёт")
        path = Path(settings.db_path).parent / f"upload-{secrets.token_hex(8)}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Файл больше 2 ГБ: выгрузите только сообщения, низкое качество медиа")
                    f.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if size == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Пустой файл")
        service._spawn(runner.run_export(path, max(10, min(max_conversations, 2000)), max(1.0, min(max_usd, 200.0))))
        return {"ok": True, "bytes": size}

    router.runner = runner  # type: ignore[attr-defined]  # для тестов
    return router
