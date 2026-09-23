# ==================== panel/config.py ====================
"""
Конфигурация — всё через переменные окружения, никаких реальных серверных
путей/токенов в git (см. CLAUDE.md "Секреты и пути — не в git"). На реальном
сервере значения задаются в systemd unit-файле (infra/tesl-panel.service,
EnvironmentFile=) — сам файл со значениями НЕ коммитится, только .template.
"""
import os

# Корень хранилища на диске — на реальном сервере это примонтированный том
# (см. infra/README за инструкцией), сюда НЕ хардкодить реальный путь.
# Локально/в CI по умолчанию — относительная папка ./data, безопасна для
# тестов, никогда не используется в проде (там STORAGE_ROOT всегда задан
# явно через окружение).
STORAGE_ROOT = os.environ.get("TESL_PANEL_STORAGE_ROOT", "./data")

# Bearer-токен для /api/depot/* (запись/листинг) — тот же принцип, что и
# DAV_PASSWORD в TESL/TESL-Manager ("не секрет в строгом смысле", встроен в
# клиентские инструменты, но не публикуется в открытом виде без необходимости).
# Публичные read-эндпоинты (/ , /api/download/<...>) токен не требуют вообще.
UPLOAD_TOKEN = os.environ.get("TESL_PANEL_UPLOAD_TOKEN", "")

# GitHub-репозитории, откуда берутся ссылки на скачивание — публичные releases,
# без токена (rate limit 60/час на IP хватает с учётом кэша ниже).
GITHUB_REPOS = {
    "launcher": {"owner": "scp-oss", "repo": "TESL", "title": "TESL (лаунчер)"},
    "manager":  {"owner": "scp-oss", "repo": "TESL-Manager", "title": "TESL-Manager"},
}

# Кэш ответа GitHub API на этот срок (секунды) — публичный API без токена
# ограничен 60 запросами/час на IP; при заметной посещаемости страницы это
# исчерпалось бы за пару минут без кэша.
GITHUB_CACHE_TTL = int(os.environ.get("TESL_PANEL_GITHUB_CACHE_TTL", "300"))

# Список известных "проектов" (первый сегмент пути в /api/depot/<project>/...)
# — держим как allowlist, а не принимаем любую строку от клиента, чтобы
# нельзя было передать что-то вроде "../../etc" в качестве project и выйти
# за пределы STORAGE_ROOT (см. storage.py::_safe_path — там же вторая,
# независимая проверка на уровне пути, это первая, на уровне имени проекта).
ALLOWED_PROJECTS = set(
    p.strip() for p in os.environ.get("TESL_PANEL_PROJECTS", "TESVAE").split(",") if p.strip()
)
