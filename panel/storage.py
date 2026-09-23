# ==================== panel/storage.py ====================
"""
Файловое хранилище депо на диске сервера — прямая замена Nextcloud/WebDAV
для НОВЫХ публикаций (см. CLAUDE.md). Раскладка на диске намеренно
идентична тому, что уже лежит на WebDAV (chunks/<xx>/<id>, versions/<key>.json,
depot.json, poster.png) — тот же формат, который уже понимает TESL-лаунчер
(core/chunk_manifest_db.py) и TESL-Manager (chunk_manager.py) — эта служба
меняет только транспорт (HTTP PUT сюда вместо WebDAV PUT в Nextcloud), не
формат данных.
"""
import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

from . import config, projects


class UnsafePathError(ValueError):
    """rel_path пытается выйти за пределы папки проекта (../, абсолютный путь и т.п.)."""


def _project_root(project: str) -> Path:
    if not projects.is_allowed(project):
        raise ValueError(f"неизвестный project: {project!r}")
    return Path(config.STORAGE_ROOT).resolve() / project


def safe_path(project: str, rel_path: str) -> Path:
    """
    Резолвит rel_path ВНУТРИ папки проекта и проверяет, что результат
    реально остался внутри неё (защита от '../../etc/passwd' и абсолютных
    путей в rel_path) — та же дисциплина, что z2r_autobench's
    domain_list_sync.sh применяет к своим путям (realpath + prefix-check
    ПЕРЕД любым обращением к файлу, не после).
    """
    root = _project_root(project)
    candidate = (root / rel_path.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError(f"путь вне папки проекта: {rel_path!r}")
    return candidate


def put_bytes(project: str, rel_path: str, data: bytes) -> None:
    path = safe_path(project, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Пишем во временный файл и атомарно переименовываем — чтобы читающий
    # launcher/операторский GET никогда не увидел частично записанный файл
    # (актуально для крупных чанков при параллельной записи/чтении).
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def get_bytes(project: str, rel_path: str) -> Optional[bytes]:
    path = safe_path(project, rel_path)
    if not path.is_file():
        return None
    return path.read_bytes()


def exists(project: str, rel_path: str) -> bool:
    return safe_path(project, rel_path).exists()


def list_chunk_ids(project: str) -> List[str]:
    """Список chunk_id, уже лежащих в chunks/<xx>/<id> — для delta-сравнения
    на стороне клиента (аналог NextcloudDAV.list_chunk_ids())."""
    chunks_dir = safe_path(project, "chunks")
    if not chunks_dir.is_dir():
        return []
    ids = []
    for sub in chunks_dir.iterdir():
        if not sub.is_dir():
            continue
        for f in sub.iterdir():
            if f.is_file():
                ids.append(f.name)
    return ids


def list_versions(project: str) -> List[str]:
    """Имена файлов версий (versions/<key>.json|.db) — для страницы
    /admin/project/<name>, самая свежая публикация первой."""
    versions_dir = safe_path(project, "versions")
    if not versions_dir.is_dir():
        return []
    return sorted(
        (f.name for f in versions_dir.iterdir() if f.is_file()),
        reverse=True,
    )


def get_depot_meta(project: str) -> Optional[dict]:
    """depot.json, разобранный — если публикаций ещё не было, файла нет,
    возвращаем None (не ошибка, обычное состояние свежедобавленного
    проекта)."""
    data = get_bytes(project, "depot.json")
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


# Выше этого размера файл не редактируется инлайн в /admin (только
# просмотр/скачивание/удаление) — большие чанки (до 4MB, см.
# TESL-Manager's chunk_manager.py::DEFAULT_CHUNK_SIZE) незачем гонять
# туда-обратно текстовым полем в браузере.
MAX_INLINE_EDIT_BYTES = 256 * 1024


def list_files(project: str) -> List[dict]:
    """Плоский список ВСЕХ файлов под папкой проекта (chunks/versions/
    depot.json/что угодно ещё туда положили) — для /admin/project/<name>/files.
    Не постранично и не оптимизировано под десятки тысяч чанков реальной
    сборки — для текущего размера использования (пустой/тестовый проект)
    достаточно; если это станет узким местом на боевой сборке, первый
    кандидат на доработку — сворачивать chunks/ в одну строку с общим
    количеством/размером вместо построчного перечисления каждого чанка."""
    root = _project_root(project)
    if not root.is_dir():
        return []
    out = []
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            out.append({"path": rel, "size": f.stat().st_size})
    out.sort(key=lambda e: e["path"])
    return out


def delete_file(project: str, rel_path: str) -> bool:
    """True — файл существовал и удалён. False — его и так не было
    (идемпотентно, не ошибка)."""
    path = safe_path(project, rel_path)
    if not path.is_file():
        return False
    path.unlink()
    return True


def delete_project_dir(project: str) -> None:
    """Удаляет ВСЁ содержимое проекта с диска — вызывающий (app.py) уже
    убрал имя из projects.py's allowlist к этому моменту или делает это
    сразу следом; порядок не важен для самой функции, но app.py удаляет
    директорию ПЕРЕД тем, как убрать имя из списка — чтобы `_project_root()`
    ниже ещё проходило проверку `is_allowed`, а не падало раньше времени."""
    root = _project_root(project)
    if root.exists():
        shutil.rmtree(root)
