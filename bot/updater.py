"""Самообновление на Render.

Render сам обновляет сервис после каждого коммита, только если сервис привязан к подключённому
аккаунту GitHub. Сервис, созданный по публичной ссылке на репозиторий (кнопка «Deploy to Render»
без входа через GitHub), обновляется только вручную — и бот месяцами остаётся на старой версии.

Что делает этот модуль:
* раз в 30 минут смотрит номер последней версии в репозитории (файл bot/cloud.py на GitHub);
* если есть новее — пишет владельцу и, если задан RENDER_DEPLOY_HOOK_URL (Render → Settings →
  Deploy Hook), сам запускает обновление. Это секретная ссылка: хранится только в Environment,
  нигде не показывается.
"""

from __future__ import annotations

import logging
import os
import re
import time

import aiohttp

from .cloud import BOT_VERSION

log = logging.getLogger(__name__)

DEFAULT_REPO = "Ski1los/ai-casher-bruh"
CHECK_SEC = 30 * 60
RETRIGGER_SEC = 2 * 3600  # если обновление не встало (например, сборка упала) — пробуем снова не чаще
NOTIFY_SEC = 24 * 3600  # напоминание без deploy hook — не чаще раза в сутки
HOOK_RE = re.compile(r"^https://api\.render\.com/deploy/srv-[A-Za-z0-9]+\?key=[A-Za-z0-9_-]+$")
VERSION_RE = re.compile(r'^BOT_VERSION\s*=\s*"(\d+)"', re.M)

# Состояние для /status, /update и /setup
UPDATE: dict = {"latest": None, "checked_at": 0.0, "error": None, "triggered": {}, "notified": {}}

MANUAL_STEPS = (
    "Обновить сейчас (1 минута): render.com → сервис <b>ai-casher-bot</b> → кнопка "
    "<b>Manual Deploy</b> → <b>Deploy latest commit</b>. Через 3–5 минут бот напишет «🆕 Бот обновлён»."
)
HOOK_STEPS = (
    "Чтобы дальше бот обновлялся <b>сам</b> (один раз, 2 минуты):\n"
    "1) render.com → сервис <b>ai-casher-bot</b> → <b>Settings</b> → найди <b>Deploy Hook</b> → "
    "нажми значок копирования;\n"
    "2) там же <b>Environment</b> → <b>Add Environment Variable</b> → ключ <code>RENDER_DEPLOY_HOOK_URL</code>, "
    "значение — вставь скопированное → <b>Save Changes</b>.\n"
    "Эта ссылка секретная: никому её не присылай, в чат тоже не надо."
)


def repo() -> str:
    return os.getenv("RENDER_GIT_REPO_SLUG", "").strip() or DEFAULT_REPO


def branch() -> str:
    return os.getenv("RENDER_GIT_BRANCH", "").strip() or "main"


def hook_url(env=None) -> str | None:
    """Deploy hook из Environment, если он похож на настоящий (иначе None)."""
    env = os.environ if env is None else env
    url = (env.get("RENDER_DEPLOY_HOOK_URL") or "").strip()
    return url if HOOK_RE.match(url) else None


def hook_problem(env=None) -> str | None:
    """Понятная ошибка, если переменная задана, но ссылка неправильная."""
    env = os.environ if env is None else env
    raw = (env.get("RENDER_DEPLOY_HOOK_URL") or "").strip()
    if raw and not HOOK_RE.match(raw):
        return ("RENDER_DEPLOY_HOOK_URL задан, но это не ссылка Deploy Hook. Она начинается с "
                "https://api.render.com/deploy/srv-… и содержит ?key=… Скопируй её заново в Settings → Deploy Hook")
    return None


def parse_version(text: str) -> str | None:
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def is_newer(latest: str | None, current: str = BOT_VERSION) -> bool:
    try:
        return latest is not None and int(latest) > int(current)
    except ValueError:
        return False


def outdated() -> bool:
    return is_newer(UPDATE.get("latest"))


async def fetch_latest(session: aiohttp.ClientSession, url: str | None = None) -> str | None:
    """Номер версии в репозитории (ветка, которую развернул Render)."""
    url = url or f"https://raw.githubusercontent.com/{repo()}/{branch()}/bot/cloud.py"
    async with session.get(url, params={"t": str(int(time.time()))}, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise RuntimeError(f"GitHub ответил {r.status}")
        return parse_version(await r.text())


async def trigger(session: aiohttp.ClientSession, url: str) -> tuple[bool, str]:
    """Запускает обновление через Deploy Hook. (успех, текст для человека)."""
    try:
        async with session.post(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status in (200, 201, 202):
                return True, "Render принял команду обновиться"
            if r.status in (401, 403, 404):
                return False, f"Render отклонил ссылку Deploy Hook ({r.status}): скопируй её заново в Settings → Deploy Hook"
            return False, f"Render ответил {r.status}, попробую позже"
    except Exception as e:
        return False, f"нет связи с Render: {type(e).__name__}"


async def check(session: aiohttp.ClientSession, url: str | None = None) -> str | None:
    try:
        latest = await fetch_latest(session, url)
        UPDATE.update(latest=latest, checked_at=time.time(), error=None)
        return latest
    except Exception as e:
        UPDATE.update(checked_at=time.time(), error=f"{type(e).__name__}: {e}"[:200])
        log.info("Проверка обновлений: %s", e)
        return None


def should_trigger(latest: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - UPDATE["triggered"].get(latest, 0) > RETRIGGER_SEC


def should_notify(latest: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - UPDATE["notified"].get(latest, 0) > NOTIFY_SEC


def status_line() -> str:
    """Одна строка для /setup, /doctor, страницы состояния."""
    latest = UPDATE.get("latest")
    if is_newer(latest):
        return (f"доступна новая версия v{latest} (у тебя v{BOT_VERSION})"
                + (": обновляюсь сам" if hook_url() else ": Render сам не обновил"))
    if latest:
        return f"последняя версия v{BOT_VERSION}" + (", обновляется сам ✅" if hook_url() else "")
    return ""
