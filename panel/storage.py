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

from . import builds_db, config


class UnsafePathError(ValueError):
    """rel_path пытается выйти за пределы папки сборки (../, абсолютный путь и т.п.)."""


def _project_root(build_id: str) -> Path:
    """Раскладка на диске по-прежнему именуется по ИМЕНИ сборки (не по
    id) — переименовывать уже опубликованные деревья чанков без доступа к
    реальному серверу было бы рискованно, см. builds_db.py. build_id —
    единственный аргумент, который принимают функции этого модуля;
    имя для физического пути резолвится здесь, один раз."""
    build = builds_db.get_build(build_id)
    if build is None:
        raise ValueError(f"неизвестная сборка: {build_id!r}")
    return Path(config.STORAGE_ROOT).resolve() / build["name"]


def _root_by_name(name: str) -> Path:
    """Для rename_project_dir() — вызывающий (app.py, после успешного
    builds_db.rename_build()) уже знает и старое, и новое имя напрямую,
    резолвить их через build_id здесь бессмысленно (новое имя в БД уже
    записано, старой директории под НОВЫМ именем ещё не существует)."""
    return Path(config.STORAGE_ROOT).resolve() / name


def safe_path(build_id: str, rel_path: str) -> Path:
    """
    Резолвит rel_path ВНУТРИ папки сборки и проверяет, что результат
    реально остался внутри неё (защита от '../../etc/passwd' и абсолютных
    путей в rel_path) — та же дисциплина, что z2r_autobench's
    domain_list_sync.sh применяет к своим путям (realpath + prefix-check
    ПЕРЕД любым обращением к файлу, не после).
    """
    root = _project_root(build_id)
    candidate = (root / rel_path.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError(f"путь вне папки сборки: {rel_path!r}")
    return candidate


def put_bytes(build_id: str, rel_path: str, data: bytes) -> None:
    path = safe_path(build_id, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Пишем во временный файл и атомарно переименовываем — чтобы читающий
    # launcher/операторский GET никогда не увидел частично записанный файл
    # (актуально для крупных чанков при параллельной записи/чтении).
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def get_bytes(build_id: str, rel_path: str) -> Optional[bytes]:
    path = safe_path(build_id, rel_path)
    if not path.is_file():
        return None
    return path.read_bytes()


def exists(build_id: str, rel_path: str) -> bool:
    return safe_path(build_id, rel_path).exists()


def list_chunk_ids(build_id: str) -> List[str]:
    """Список chunk_id, уже лежащих в chunks/<xx>/<id> — для delta-сравнения
    на стороне клиента (аналог NextcloudDAV.list_chunk_ids())."""
    chunks_dir = safe_path(build_id, "chunks")
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


def list_versions(build_id: str) -> List[str]:
    """Имена файлов версий (versions/<key>.json|.db) — для страницы
    /admin/project/<name>, самая свежая публикация первой."""
    versions_dir = safe_path(build_id, "versions")
    if not versions_dir.is_dir():
        return []
    return sorted(
        (f.name for f in versions_dir.iterdir() if f.is_file()),
        reverse=True,
    )


def get_depot_meta(build_id: str) -> Optional[dict]:
    """depot.json, разобранный — если публикаций ещё не было, файла нет,
    возвращаем None (не ошибка, обычное состояние свежедобавленной
    сборки)."""
    data = get_bytes(build_id, "depot.json")
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


def list_files(build_id: str) -> List[dict]:
    """Плоский список ВСЕХ файлов под папкой сборки (chunks/versions/
    depot.json/что угодно ещё туда положили) — для /admin/project/<name>/files.
    Не постранично и не оптимизировано под десятки тысяч чанков реальной
    сборки — для текущего размера использования (пустой/тестовый проект)
    достаточно; если это станет узким местом на боевой сборке, первый
    кандидат на доработку — сворачивать chunks/ в одну строку с общим
    количеством/размером вместо построчного перечисления каждого чанка."""
    root = _project_root(build_id)
    if not root.is_dir():
        return []
    out = []
    for f in root.rglob("*"):
        if f.is_file():
            rel = f.relative_to(root).as_posix()
            out.append({"path": rel, "size": f.stat().st_size})
    out.sort(key=lambda e: e["path"])
    return out


def delete_file(build_id: str, rel_path: str) -> bool:
    """True — файл существовал и удалён. False — его и так не было
    (идемпотентно, не ошибка)."""
    path = safe_path(build_id, rel_path)
    if not path.is_file():
        return False
    path.unlink()
    return True


def delete_dir_by_name(name: str) -> None:
    """Удаляет ВСЁ содержимое сборки с диска, по ИМЕНИ, не по build_id —
    вызывается ПОСЛЕ builds_db.delete_build(build_id) (тот уже вернул имя
    и убрал строку из реестра, так что _project_root(build_id) больше не
    резолвится — отсюда и отдельная by-name версия, не переиспользуем
    _project_root())."""
    root = _root_by_name(name)
    if root.exists():
        shutil.rmtree(root)


def rename_dir(old_name: str, new_name: str) -> bool:
    """Физически переименовывает папку сборки на диске — вызывается
    ПОСЛЕ builds_db.rename_build() (та уже проверила конфликт имён и
    обновила БД), здесь просто выполняется сама файловая операция.
    True — переименовано (или папки не было вовсе, тогда её физически
    создавать сейчас нет смысла — появится при следующей публикации),
    False — на месте нового имени уже что-то физически есть (не должно
    случиться при нормальной работе, поскольку builds_db гарантирует
    уникальность имён, но диск может отличаться от БД, если кто-то
    руками что-то трогал — тогда лучше явно отказать, чем молча
    затереть)."""
    old_root = _root_by_name(old_name)
    new_root = _root_by_name(new_name)
    if not old_root.exists():
        return True
    if new_root.exists():
        return False
    old_root.rename(new_root)
    return True
