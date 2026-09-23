# ==================== panel/reports_storage.py ====================
"""
Хранилище отчётов с клиентов (лаунчеров) — прямой запрос пользователя:
раздел "Отчёты" в панели, два вида (см. TESL/launcher/core/crash_logger.py
и debug_log.py — уже существующие, СЕЙЧАС отправляются на WebDAV, не
сюда): крэш-репорты Skyrim (crash-логи/Papyrus-лог/сейв) и лог отладки
лаунчера. Эта панель пока НЕ является их реальным получателем — лаунчер
намеренно не трогается в этом заходе (стоящая инструкция пользователя,
см. TESL-Manager/CLAUDE.md — "TESL (лаунчер) — пока не трогаем его"),
это инфраструктура ГОТОВАЯ принять отчёты, когда/если лаунчер
переключат сюда отдельным, более поздним шагом.

Раскладка на диске: <STORAGE_ROOT>/_reports/<report_type>/<username>/
<timestamp>/<filename> — та же трёхуровневая структура
(remote_path/username/timestamp/), что уже использует
crash_logger.py::upload_files() на WebDAV, специально повторена здесь
1:1, чтобы при будущем переключении транспорта раскладка не менялась,
только клиент, который её пишет.
"""
import re
import shutil
from pathlib import Path
from typing import List, Optional

from . import config

REPORTS_DIR = "_reports"

# Два вида, см. докстринг модуля — TESL/launcher/config.py's
# CRASH_LOG_REMOTE_PATH / DEBUG_LOG_REMOTE_PATH их же и называют.
REPORT_TYPES = {
    "crash":     "Крэш-репорты (Skyrim)",
    "debug_log": "Логи отладки лаунчера",
}

# Тот же принцип, что и у builds_db._NAME_RE — сегмент файлового пути,
# никаких "/", ".." и т.п.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class UnsafePathError(ValueError):
    pass


def is_valid_report_type(report_type: str) -> bool:
    return report_type in REPORT_TYPES


def is_valid_segment(s: str) -> bool:
    return bool(_SEGMENT_RE.match(s))


def _reports_root() -> Path:
    return Path(config.STORAGE_ROOT).resolve() / REPORTS_DIR


def _type_root(report_type: str) -> Path:
    if not is_valid_report_type(report_type):
        raise ValueError(f"неизвестный тип отчёта: {report_type!r}")
    return _reports_root() / report_type


def _entry_dir(report_type: str, username: str, timestamp: str) -> Path:
    if not is_valid_segment(username) or not is_valid_segment(timestamp):
        raise UnsafePathError("некорректное имя пользователя или метка времени")
    root = _type_root(report_type)
    candidate = (root / username / timestamp).resolve()
    if root not in candidate.parents:
        raise UnsafePathError("путь вне папки отчётов")
    return candidate


def list_usernames(report_type: str) -> List[str]:
    root = _type_root(report_type)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def list_timestamps(report_type: str, username: str) -> List[str]:
    if not is_valid_segment(username):
        return []
    root = _type_root(report_type) / username
    if not root.is_dir():
        return []
    return sorted((d.name for d in root.iterdir() if d.is_dir()), reverse=True)


def list_files(report_type: str, username: str, timestamp: str) -> List[dict]:
    d = _entry_dir(report_type, username, timestamp)
    if not d.is_dir():
        return []
    out = [
        {"name": f.name, "size": f.stat().st_size}
        for f in d.iterdir() if f.is_file()
    ]
    out.sort(key=lambda e: e["name"])
    return out


def get_file_path(report_type: str, username: str, timestamp: str, filename: str) -> Path:
    if not is_valid_segment(filename):
        raise UnsafePathError(f"некорректное имя файла: {filename!r}")
    d = _entry_dir(report_type, username, timestamp)
    candidate = (d / filename).resolve()
    if d not in candidate.parents and candidate != d:
        raise UnsafePathError("путь вне папки отчёта")
    return candidate


def put_file(report_type: str, username: str, timestamp: str, filename: str, data: bytes) -> None:
    path = get_file_path(report_type, username, timestamp, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def delete_entry(report_type: str, username: str, timestamp: str) -> bool:
    """Удаляет ОДНУ папку-отчёт (<type>/<username>/<timestamp>/) целиком.
    True — существовала и удалена, False — её и так не было."""
    d = _entry_dir(report_type, username, timestamp)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def count_entries(report_type: str) -> int:
    """Сколько всего <username>/<timestamp> папок этого типа — для
    сводки на верхнем уровне /admin/reports, без обхода списков файлов."""
    total = 0
    for username in list_usernames(report_type):
        total += len(list_timestamps(report_type, username))
    return total
