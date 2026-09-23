# ==================== panel/builds_db.py ====================
"""
Реестр "сборок" (то, что раньше называлось "проект", `projects.py`) —
прямой запрос пользователя (2026-09-23): имя сборки не должно быть
ключом связи между панелью и менеджером, потому что сборки создаются
независимо в обоих местах — нужен настоящий стабильный id, а не строка,
которую легко случайно рассинхронизировать (опечатка, регистр,
переименование). Полноценно: SQLite вместо JSON-списка (`projects.py`,
теперь удалён), `id` (UUID4 hex) — реальный ключ везде, где сборка
адресуется программно (`/api/*`), имя — только для чтения человеком (в
`/admin` URL-ах, в списках) и как имя папки на диске (см. ниже, почему
это НЕ то же самое, что использовать имя как identity).

Важно отличать от `chunk_index.db` (`pack_writer.py`, TESL-Manager) —
ЭТОТ файл ("локальная бд для сборки" по формулировке пользователя) НЕ
трогается и не заменяется, он остаётся своим отдельным SQLite ВНУТРИ
папки каждой сборки (`<build_dir>/chunk_index.db`) и решает совсем
другую задачу (где физически лежит чанк внутри pack-файлов). Этот
модуль — реестр САМИХ сборок (что существует, какое у чего имя), один
файл на всю панель (`<STORAGE_ROOT>/_meta/builds.db`), не имеет
никакого отношения к содержимому конкретной сборки.

Имя сборки СОХРАНЯЕТСЯ как имя папки на диске (storage.py менять раскладку
уже опубликованных сборок было бы рискованно без доступа к реальному
серверу) — rename_build() физически переименовывает папку тем же
заходом, что меняет строку в БД, так что имя-как-путь и имя-в-реестре
никогда не расходятся.
"""
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import config

_lock = threading.Lock()

# Тот же паттерн, что был у старого projects.py::_PROJECT_NAME_RE —
# буквы/цифры/подчёркивание/дефис, используется как сегмент файлового
# пути (storage.py), поэтому "/", ".." и т.п. в принципе не проходят.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _db_path() -> Path:
    p = Path(config.STORAGE_ROOT).resolve() / "_meta" / "builds.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    # Новое соединение на каждый вызов — эта БД живёт только на пути
    # управления сборками (list/create/delete/rename), не на горячем
    # пути записи/чтения чанков (тот остаётся чистой файловой операцией
    # в storage.py) — частота вызовов низкая, постоянное соединение
    # ради этого не оправдано, а per-call соединение проще и безопаснее
    # при нескольких gunicorn-воркерах (нет расшаренного состояния).
    conn = sqlite3.connect(str(_db_path()), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS builds ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL UNIQUE,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_legacy_if_needed(conn: sqlite3.Connection) -> None:
    """Один раз переносит имена из старого <STORAGE_ROOT>/_meta/projects.json
    (до этого захода — единственный источник правды) в builds.db, выдавая
    каждому свежий id. Идемпотентно — если в builds уже есть хоть одна
    строка, ничего не делает; специально НЕ трогает и не удаляет старый
    projects.json (оставляем как есть на диске — не мешает, но и незачем
    трогать файл, который сам код больше не читает)."""
    count = conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
    if count > 0:
        return

    import json
    legacy_path = Path(config.STORAGE_ROOT).resolve() / "_meta" / "projects.json"
    names: List[str] = []
    if legacy_path.is_file():
        try:
            names = list(json.loads(legacy_path.read_text(encoding="utf-8")).get("projects", []))
        except Exception:
            names = []
    if not names:
        # Свежая установка без legacy-файла — сидируем тем же, чем раньше
        # сидировался projects.json (config.INITIAL_PROJECTS).
        names = sorted(set(config.INITIAL_PROJECTS))

    now = _now()
    for name in names:
        if not _NAME_RE.match(name):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO builds (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, name, now, now),
        )
    conn.commit()


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def list_builds() -> List[dict]:
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            rows = conn.execute(
                "SELECT id, name, created_at, updated_at FROM builds ORDER BY name"
            ).fetchall()
            return [
                {"id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3]}
                for r in rows
            ]
        finally:
            conn.close()


def get_build(build_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if row is None:
                return None
            return {"id": row[0], "name": row[1], "created_at": row[2], "updated_at": row[3]}
        finally:
            conn.close()


def get_build_by_name(name: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at FROM builds WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return {"id": row[0], "name": row[1], "created_at": row[2], "updated_at": row[3]}
        finally:
            conn.close()


def is_allowed(build_id: str) -> bool:
    return get_build(build_id) is not None


def create_build(name: str) -> "tuple[Optional[dict], str]":
    """(build, "") при успехе, (None, причина) при отказе."""
    name = name.strip()
    if not is_valid_name(name):
        return None, f"недопустимое имя: {name!r} (только буквы/цифры/_/-, до 64 симв.)"
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            existing = conn.execute("SELECT id FROM builds WHERE name = ?", (name,)).fetchone()
            if existing:
                # Идемпотентно, тот же принцип, что был у projects.add_project() —
                # повторное создание уже существующего имени не ошибка.
                return get_build_by_name(name), ""
            build_id = uuid.uuid4().hex
            now = _now()
            conn.execute(
                "INSERT INTO builds (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (build_id, name, now, now),
            )
            conn.commit()
            return {"id": build_id, "name": name, "created_at": now, "updated_at": now}, ""
        finally:
            conn.close()


def delete_build(build_id: str) -> Optional[dict]:
    """Возвращает удалённую запись (чтобы вызывающий знал имя — для
    удаления папки на диске) или None, если такого id не было."""
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
            conn.commit()
            return {"id": row[0], "name": row[1], "created_at": row[2], "updated_at": row[3]}
        finally:
            conn.close()


def rename_build(build_id: str, new_name: str) -> "tuple[bool, str]":
    """(True, старое_имя) при успехе — вызывающий (app.py) переименовывает
    папку на диске тем же old_name/new_name, (False, причина) при отказе.
    Переименование папки НЕ делается здесь — этот модуль ничего не знает
    про storage.py/файловую систему, разделение ответственности то же,
    что и у storage.py самого (никогда не трогает builds_db)."""
    new_name = new_name.strip()
    if not is_valid_name(new_name):
        return False, f"недопустимое имя: {new_name!r} (только буквы/цифры/_/-, до 64 симв.)"
    with _lock:
        conn = _connect()
        try:
            _migrate_legacy_if_needed(conn)
            row = conn.execute("SELECT name FROM builds WHERE id = ?", (build_id,)).fetchone()
            if row is None:
                return False, "сборка не найдена"
            old_name = row[0]
            if old_name == new_name:
                return True, old_name
            clash = conn.execute("SELECT id FROM builds WHERE name = ?", (new_name,)).fetchone()
            if clash:
                return False, f"имя уже занято другой сборкой: {new_name!r}"
            conn.execute(
                "UPDATE builds SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, _now(), build_id),
            )
            conn.commit()
            return True, old_name
        finally:
            conn.close()
