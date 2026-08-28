#!/usr/bin/env python3
"""Gemini API OneClick gateway extension layer.

The mature routing implementation lives in ``gateway_legacy.py``. This thin
entrypoint keeps its behavior intact while adding the redesigned management
API surface, Google usage-limit aggregation and Russian-only user-facing
localization.
"""
from __future__ import annotations

import asyncio as _asyncio
import re as _re
from datetime import datetime as _datetime, timezone as _timezone

import httpx as _httpx
import uvicorn as _uvicorn
from fastapi import HTTPException as _HTTPException, Request as _Request
from fastapi.exception_handlers import http_exception_handler as _http_exception_handler
from fastapi.responses import HTMLResponse as _HTMLResponse

import gateway_legacy as _legacy

# Preserve every existing route/helper/state object. Existing route functions
# still execute in gateway_legacy and therefore retain the tested routing
# semantics; this module only extends/patches presentation-facing behavior.
globals().update({
    name: value
    for name, value in vars(_legacy).items()
    if not name.startswith("__")
})


# ── User-facing localization ──────────────────────────────────────────

_MODEL_PROFILE_LABELS = {
    "chat_fast": "Быстрый чат",
    "chat_pro": "Pro чат",
    "chat_thinking": "Рассуждение",
    "prompt_optimize": "Оптимизация промпта",
    "image_default": "Генерация изображений",
    "image_edit": "Редактирование изображений",
    "video_default": "Генерация видео",
}
_legacy.MODEL_PROFILE_SLOTS.update(_MODEL_PROFILE_LABELS)
MODEL_PROFILE_SLOTS = _legacy.MODEL_PROFILE_SLOTS

_REPLACEMENTS = (
    ("请求过于频繁，请稍后再试", "Слишком много попыток. Повторите позже."),
    ("未授权", "Не авторизовано"),
    ("仅内部网络可访问", "Доступ разрешён только из внутренней сети"),
    ("Cookie 未就绪或已过期", "Cookie не готов или истёк"),
    ("仅生文，需更新 Cookie", "Доступен только текст; обновите Cookie"),
    ("Cookie 过期，已标记需更新", "Cookie истёк; требуется обновление"),
    ("Cookie 已更新，正在重载 Slot", "Cookie обновлён; Slot перезагружается"),
    ("Cookie 已更新，正在重建容器", "Cookie обновлён; контейнер пересоздаётся"),
    ("Slot 部署失败", "Не удалось развернуть Slot"),
    ("容器重建失败", "Не удалось пересоздать контейнер"),
    ("容器重建超时", "Тайм-аут пересоздания контейнера"),
    ("docker compose 命令未找到", "Команда docker compose не найдена"),
    ("已部署", "Развёрнуто"),
    ("已删除", "Удалено"),
    ("模型列表已刷新", "Список моделей обновлён"),
    ("模型槽位已更新", "Профили моделей обновлены"),
    ("模型刷新拿到的是旧的静态 3.0 列表，容器侧模型发现仍未修复", "Получен устаревший статический список моделей 3.0; runtime-discovery требует проверки"),
    ("无可用容器获取模型列表", "Нет доступного аккаунта для получения списка моделей"),
    ("分组名不能为空", "Имя группы не может быть пустым"),
    ("新名称不能为空", "Новое имя не может быть пустым"),
    ("分组名只能包含小写字母、数字、下划线、横杠", "Имя группы может содержать строчные латинские буквы, цифры, подчёркивание и дефис"),
    ("不存在，请先创建", "не существует; сначала создайте её"),
    ("不存在", "не существует"),
    ("已存在", "уже существует"),
    ("批量分组", "Групповое назначение"),
    ("默认", "по умолчанию"),
    ("冷却", "Пауза"),
    ("个容器", "контейнеров"),
    ("分组", "Группа"),
    ("已写入", "Сохранено"),
    ("到", "в"),
)
_CJK_RE = _re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def localize_user_text(value):
    """Translate known legacy messages and guarantee that CJK is never shown in UI."""
    if not isinstance(value, str):
        return value
    text = value
    for source, target in _REPLACEMENTS:
        text = text.replace(source, target)
    text = _CJK_RE.sub("", text)
    text = _re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = _re.sub(r"\s{2,}", " ", text).strip()
    return text or "Сообщение OneClick"


_original_add_log = _legacy.add_log


def _localized_add_log(level: str, container_num: int | None, message: str):
    return _original_add_log(level, container_num, localize_user_text(message))


_legacy.add_log = _localized_add_log
add_log = _localized_add_log


@app.exception_handler(_HTTPException)
async def _localized_http_exception_handler(request: _Request, exc: _HTTPException):
    detail = exc.detail
    if isinstance(detail, str):
        detail = localize_user_text(detail)
    elif isinstance(detail, list):
        detail = [localize_user_text(item) if isinstance(item, str) else item for item in detail]
    localized = _HTTPException(status_code=exc.status_code, detail=detail, headers=exc.headers)
    return await _http_exception_handler(request, localized)


# ── Usage dashboard API ──────────────────────────────────────────────


def _worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def _usage_unavailable(c, reason: str, error: str = "") -> dict:
    return {
        "num": c.num,
        "available": False,
        "reason": reason,
        "error": localize_user_text(error) if error else "",
        "usage": {"available": False, "reason": reason},
        "sessions": {"active": 0, "items": []},
    }


async def _fetch_container_usage(c, client: _httpx.AsyncClient, *, force: bool = False) -> dict:
    if not WORKER_MODE:
        return _usage_unavailable(c, "usage_requires_worker_mode")
    try:
        response = await client.get(
            f"{c.url}/usage",
            params={"force": "true" if force else "false"},
            headers=_worker_headers(),
            timeout=15.0,
        )
        if response.status_code != 200:
            return _usage_unavailable(
                c,
                "worker_usage_http_error",
                f"HTTP {response.status_code}: {response.text[:180]}",
            )
        data = response.json()
        if isinstance(data.get("error"), str):
            data["error"] = localize_user_text(data["error"])
        usage = data.get("usage") or {}
        if isinstance(usage.get("error"), str):
            usage["error"] = localize_user_text(usage["error"])
        if isinstance(usage.get("refresh_error"), str):
            usage["refresh_error"] = localize_user_text(usage["refresh_error"])
        return data
    except Exception as exc:
        return _usage_unavailable(c, "worker_usage_unreachable", str(exc)[:180])


@app.get("/api/usage", dependencies=[Depends(verify_panel_auth)])
async def api_usage(force: bool = False):
    """Aggregate Google-reported account limits and active Gemini sessions."""
    ordered = [containers[num] for num in sorted(containers)]
    if not ordered:
        return {
            "accounts": [],
            "fetched_at": _datetime.now(tz=_timezone.utc).isoformat(),
            "mode": ARCH_MODE,
        }

    limits = _httpx.Limits(max_connections=min(max(len(ordered), 4), 16))
    async with _httpx.AsyncClient(limits=limits) as client:
        raw = await _asyncio.gather(
            *(_fetch_container_usage(c, client, force=force) for c in ordered),
            return_exceptions=True,
        )

    accounts = []
    for c, item in zip(ordered, raw, strict=True):
        if isinstance(item, Exception):
            item = _usage_unavailable(c, "usage_fetch_failed", str(item))
        item.update({
            "name": account_names.get(c.num, ""),
            "group": container_groups.get(c.num, ""),
            "enabled": c.enabled,
            "healthy": c.healthy,
            "available_for_routing": c.available,
            "busy": c.busy,
            "needs_cookie": c.needs_cookie,
            "requests": {
                "total": c.total_requests,
                "chat": c.chat_requests,
                "images": c.image_requests,
                "errors": c.total_errors,
            },
        })
        accounts.append(item)

    return {
        "accounts": accounts,
        "fetched_at": _datetime.now(tz=_timezone.utc).isoformat(),
        "mode": ARCH_MODE,
    }


@app.get("/api/usage/{num}", dependencies=[Depends(verify_panel_auth)])
async def api_usage_one(num: int, force: bool = False):
    c = containers.get(num)
    if c is None:
        raise _HTTPException(status_code=404, detail=f"Аккаунт #{num} не найден")
    async with _httpx.AsyncClient() as client:
        item = await _fetch_container_usage(c, client, force=force)
    item.update({
        "name": account_names.get(c.num, ""),
        "group": container_groups.get(c.num, ""),
        "enabled": c.enabled,
        "healthy": c.healthy,
        "available_for_routing": c.available,
        "busy": c.busy,
        "needs_cookie": c.needs_cookie,
    })
    return item


_LEGACY_CJK_GUARD = r"""
<script>
(() => {
  const replacements = new Map([
    ['默认', 'По умолчанию'],
    ['聊天', 'чат'],
    ['图片', 'изображения'],
    ['错误', 'ошибки'],
    ['容器', 'контейнер'],
    ['分组', 'группа'],
    ['加载', 'загрузка'],
    ['保存', 'сохранить'],
    ['删除', 'удалить'],
    ['取消', 'отмена'],
    ['确认', 'подтвердить'],
  ]);
  const cjk = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+/g;
  function clean(value) {
    if (!value) return value;
    let out = value;
    for (const [source, target] of replacements) out = out.replaceAll(source, target);
    return out.replace(cjk, '').replace(/\s{2,}/g, ' ').trim();
  }
  function cleanNode(root) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const next = clean(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
    }
    if (root.querySelectorAll) {
      for (const el of root.querySelectorAll('[title],[placeholder],[aria-label]')) {
        for (const attr of ['title','placeholder','aria-label']) {
          if (el.hasAttribute(attr)) el.setAttribute(attr, clean(el.getAttribute(attr)));
        }
      }
    }
  }
  cleanNode(document.body);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData') cleanNode(record.target.parentElement);
      for (const node of record.addedNodes) {
        if (node.nodeType === Node.ELEMENT_NODE) cleanNode(node);
        else if (node.nodeType === Node.TEXT_NODE && node.parentElement) cleanNode(node.parentElement);
      }
    }
  }).observe(document.body, {childList: true, subtree: true, characterData: true});
})();
</script>
"""


@app.get("/legacy", include_in_schema=False)
async def legacy_management_ui():
    """Serve the classic media studio with a final Russian-only rendering guard."""
    legacy_path = ROOT_DIR / "web" / "legacy.html"
    if not legacy_path.exists():
        raise _HTTPException(status_code=404, detail="Классическая медиа-студия не установлена")
    html = legacy_path.read_text(encoding="utf-8")
    if "</body>" in html:
        html = html.replace("</body>", f"{_LEGACY_CJK_GUARD}</body>", 1)
    else:
        html += _LEGACY_CJK_GUARD
    return _HTMLResponse(html)


if __name__ == "__main__":
    print(f"[gateway] OneClick gateway ({ARCH_MODE}) on {GATEWAY_HOST}:{GATEWAY_PORT}")
    _uvicorn.run("gateway:app", host=GATEWAY_HOST, port=GATEWAY_PORT, log_level="info")
