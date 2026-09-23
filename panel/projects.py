# ==================== panel/projects.py ====================
"""
Список разрешённых project-имён — раньше был статичным (только env
TESL_PANEL_PROJECTS, требовал передеплоя/рестарта сервиса, чтобы добавить
новую сборку). Теперь — персистентный JSON-файл на диске
(<STORAGE_ROOT>/_meta/projects.json), редактируемый через /admin (см.
app.py) БЕЗ рестарта сервиса. env-переменная используется только как
СИД при самом первом запуске (файла ещё нет) — дальше она не
перечитывается, единственный источник правды — этот файл.
"""
import json
import re
import threading
from pathlib import Path
from typing import List

from . import config

_lock = threading.Lock()

# Разрешены буквы/цифры/подчёркивание/дефис — то же имя используется как
# сегмент файлового пути (storage.py::_project_root), поэтому никаких "/",
# ".." и т.п. в принципе не может пройти этот паттерн.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _meta_path() -> Path:
    p = Path(config.STORAGE_ROOT).resolve() / "_meta" / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _save(names: List[str]) -> None:
    p = _meta_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"projects": sorted(set(names))}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def _load() -> List[str]:
    p = _meta_path()
    if not p.is_file():
        seed = sorted(set(config.INITIAL_PROJECTS))
        _save(seed)
        return seed
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("projects", []))
    except Exception:
        return []


def is_valid_name(name: str) -> bool:
    return bool(_PROJECT_NAME_RE.match(name))


def list_projects() -> List[str]:
    with _lock:
        return _load()


def is_allowed(name: str) -> bool:
    return name in list_projects()


def add_project(name: str) -> bool:
    """False — имя не прошло валидацию. True — добавлено (или уже было,
    идемпотентно)."""
    if not is_valid_name(name):
        return False
    with _lock:
        names = _load()
        if name not in names:
            names.append(name)
            _save(names)
    return True


def remove_project(name: str) -> None:
    """Идемпотентно — не ошибка, если имени уже не было в списке."""
    with _lock:
        names = _load()
        if name in names:
            names.remove(name)
            _save(names)
