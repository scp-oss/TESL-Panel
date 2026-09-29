# ==================== panel/storage_cluster.py ====================
"""
Кластер мест хранения — прямой запрос пользователя (2026-09-29):
"добавь возможность добавлять папки ещё" + уточнение "сделаем как
кластер папки, то есть одна папка на одном разделе, другая на другом
(речь о серверной части), мы их объединяем в кластер и запоминаем...
какая разница где хранить чанки, единственное — на сервере HDD, а
значит пишем последовательно, чтоб быстрее потом читать и скачивать".

Модель: STORAGE_ROOT (config.py) больше не единственное место, куда
может физически лечь новая сборка — это список "членов" (папок,
обычно на разных смонтированных дисках/разделах). Каждая СБОРКА
целиком (все её chunks/packs/versions) живёт на РОВНО ОДНОМ члене —
выбирается один раз, при создании сборки (builds_db.py::create_build(),
см. её "storage_root" колонку), и никогда не меняется автоматически
(переезд сборки между дисками — не то, что этот модуль делает; см.
docstring pick_member_for_new_build() ниже за причину). Это
СОЗНАТЕЛЬНО, а не половинчатая реализация "настоящего" распределённого
хранилища — ровно то, о чём предупреждала не выбранная пользователем
альтернатива ("выбрать раздел для хранения на сервере"): раз одна
сборка целиком лежит на одном разделе, последовательная запись pack-
файлов на HDD (см. TESL-Manager/CLAUDE.md "Алгоритм заливки под
SSD/HDD") остаётся последовательной ВНУТРИ каждого диска — ничего не
размазывается по кластеру мид-паблишинга.

Список членов кластера хранится в файле `<STORAGE_ROOT>/_meta/
storage_cluster.json` (та же STORAGE_ROOT/_meta, где уже живёт
`builds.db`, см. builds_db.py — один общий "системный" каталог для
метаданных панели, не per-build). Идемпотентно сидируется один раз из
`TESL_PANEL_STORAGE_ROOTS` (env, через запятую) при первом обращении,
если файла ещё нет — тот же паттерн, что уже применяет
builds_db.py::_migrate_legacy_if_needed() для legacy projects.json.
После первого запуска файл — источник истины, дальше добавление папок
идёт через add_member() (вызывается из /admin/settings, см. app.py),
не через переменную окружения (та работает только как сид для пустой
установки).
"""
import json
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, system_stats

_lock = threading.Lock()


def _registry_path() -> Path:
    p = Path(config.STORAGE_ROOT).resolve() / "_meta" / "storage_cluster.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_raw() -> List[str]:
    """Список путей-членов как строки, без метрик диска (дёшево, для
    внутреннего использования — list_members() ниже добавляет метрики
    поверх этого). Сидирует файл из TESL_PANEL_STORAGE_ROOTS/STORAGE_ROOT
    при первом обращении, если файла ещё нет — после этого файл главный,
    переменная окружения больше не перечитывается."""
    reg = _registry_path()
    if reg.is_file():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            members = list(data.get("members", []))
            if members:
                return members
        except Exception:
            pass
    # Файла ещё нет (или он пуст/битый) — сидируем текущим STORAGE_ROOT
    # плюс всё, что явно перечислено в TESL_PANEL_STORAGE_ROOTS, если
    # задано. STORAGE_ROOT всегда первый — он же и есть "_meta"
    # (реестр builds.db/этот файл никогда сами не переезжают между
    # членами кластера, только содержимое конкретных сборок).
    seed = [str(Path(config.STORAGE_ROOT).resolve())]
    for p in config.EXTRA_STORAGE_ROOTS:
        rp = str(Path(p).resolve())
        if rp not in seed:
            seed.append(rp)
    _save_raw(seed)
    return seed


def _save_raw(members: List[str]) -> None:
    _registry_path().write_text(
        json.dumps({"members": members}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_members() -> List[dict]:
    """Каждый член + его метрики диска (shutil.disk_usage через
    system_stats.disk_usage() — то же самое, что уже использует
    /admin/dashboard для системных дисков, не отдельная реализация).
    `reachable: False` — путь не существует/не примонтирован сейчас
    (не бросаем исключение — это админский экран, должен показать
    проблему, а не упасть на ней)."""
    out = []
    with _lock:
        raw = _load_raw()
    for path in raw:
        Path(path).mkdir(parents=True, exist_ok=True) if not Path(path).exists() else None
        usage = system_stats.disk_usage(path)
        if usage is None:
            out.append({"path": path, "reachable": False, "free": 0, "total": 0, "used": 0, "percent": 0.0})
        else:
            out.append({"path": path, "reachable": True, **usage})
    return out


def add_member(path: str) -> Tuple[bool, str]:
    """Добавляет папку в кластер — прямой запрос "добавь возможность
    добавлять папки ещё". Не переносит уже существующие сборки, ничего
    не публикует — просто делает путь ДОСТУПНЫМ для НОВЫХ сборок (см.
    pick_member_for_new_build() ниже)."""
    path = path.strip()
    if not path:
        return False, "путь не может быть пустым"
    try:
        resolved = Path(path).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"не удалось создать/открыть папку: {e}"
    resolved_str = str(resolved)
    with _lock:
        members = _load_raw()
        if resolved_str in members:
            return True, "уже в кластере"
        members.append(resolved_str)
        _save_raw(members)
    return True, ""


def remove_member(path: str) -> Tuple[bool, str]:
    """Убирает папку из списка КАНДИДАТОВ для новых сборок — НЕ трогает
    уже созданные там сборки (их storage_root в builds_db остаётся
    прежним, они продолжают читаться/писаться оттуда как раньше, см.
    _project_root() в storage.py) и не удаляет ничего физически с
    диска. Последний оставшийся член убрать нельзя — кластеру всегда
    нужно хотя бы одно место для новых сборок."""
    resolved_str = str(Path(path).resolve())
    with _lock:
        members = _load_raw()
        if resolved_str not in members:
            return False, "такого пути нет в кластере"
        if len(members) <= 1:
            return False, "нельзя убрать последний оставшийся member кластера"
        members.remove(resolved_str)
        _save_raw(members)
    return True, ""


def pick_member_for_new_build() -> str:
    """Куда положить НОВУЮ сборку — прямая цитата пользователя: "какая
    разница где хранить чанки". Политика: член с БОЛЬШИМ количеством
    свободного места на момент создания — простое, предсказуемое
    выравнивание нагрузки по кластеру (не даёт одному диску забиться,
    пока соседний пустует, при этом навсегда закрепляет ЦЕЛУЮ сборку
    за одним разделом — см. докстринг модуля, почему это важно для
    последовательной записи на HDD). Недостижимые (`reachable=False`)
    члены исключаются. Если недостижимы вообще все — возвращает
    исторический STORAGE_ROOT как последний резерв (то же поведение,
    что было ДО появления кластера, не тихий сбой)."""
    members = list_members()
    reachable = [m for m in members if m["reachable"]]
    if not reachable:
        return str(Path(config.STORAGE_ROOT).resolve())
    best = max(reachable, key=lambda m: m["free"])
    return best["path"]
