# ==================== panel/depot_view.py ====================
"""
Компонентно-сгруппированный, отфильтрованный от служебных путей вид на
файлы сборки для `/admin/project/<name>/files` — зеркалит TESL-Manager::
depot_sync_manager/depot_files_tab.py "Группировка по компонентам +
'как на скачанном клиенте'" (2026-09-28) на сервере, см. её докстринг
за полную мотивацию (та же — "перенос функционала менеджера кроме
заливки релизов", 2026-09-28).

Компонентные файлы (`Skyrim/...`/`MO2p/...`/`MO2ext/...`) — ЛОГИЧЕСКИЕ
пути из `depot_manifest.json` (пишет TESL-Manager при публикации через
`execute_sync_packed()`), они не существуют на диске как отдельные
файлы в упакованном/chunked-режиме — реальные байты лежат в
`chunks/<xx>/<id>`/`packs/pack-NNNNN.bin`, адресуемых по хэшу.
Поэтому они **read-only здесь** (см. `is_component_path()` — все
мутирующие роуты в app.py обязаны проверить это ПЕРЕД любой записью/
удалением по пути) — то же обоснование, что и в депот_files_tab.py:
единственный безопасный способ поменять их содержимое — отредактировать
исходники компонента на машине оператора и опубликовать сборку заново
через TESL-Manager, не редактировать чанк вручную (это развалит все
остальные файлы, ссылающиеся на тот же чанк по хэшу).
"""
import json
from typing import Dict, List

from . import storage

COMPONENT_GROUPS = ("Skyrim", "MO2p", "MO2ext")

_SERVICE_PATHS = {
    "depot.json", "depot_manifest.json", "chunk_index.db",
    "extras_manifest.json", "poster.png",
}
_SERVICE_PREFIXES = ("chunks/", "packs/", "versions/")


def is_service_path(path: str) -> bool:
    return path in _SERVICE_PATHS or path.startswith(_SERVICE_PREFIXES)


def is_component_path(rel_path: str) -> bool:
    head = rel_path.split("/", 1)[0]
    return head in COMPONENT_GROUPS


def component_files(build_id: str) -> Dict[str, int]:
    """path -> size, из depot_manifest.json (реально опубликованная
    структура компонентов на момент ПОСЛЕДНЕЙ публикации) — пусто, если
    публикаций ещё не было или файл битый/отсутствует."""
    data = storage.get_bytes(build_id, "depot_manifest.json")
    if data is None:
        return {}
    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception:
        return {}
    out: Dict[str, int] = {}
    for path, entry in (manifest.get("files") or {}).items():
        size = entry.get("size") if isinstance(entry, dict) else None
        out[path] = size if isinstance(size, int) else 0
    return out


def visible_files(build_id: str) -> List[dict]:
    """Плоский список отображаемых файлов — реальные не-служебные файлы
    (documents/patch/patchs/что-угодно-руками-добавленное, полный CRUD)
    + виртуальные компонентные пути из depot_manifest.json (read-only).
    Служебное хранилище депо (chunks/packs/versions/depot.json/...)
    скрыто целиком, как и в депот_files_tab.py."""
    out = [
        {"path": f["path"], "size": f["size"], "component": False}
        for f in storage.list_files(build_id)
        if not is_service_path(f["path"])
    ]
    out += [
        {"path": path, "size": size, "component": True}
        for path, size in component_files(build_id).items()
    ]
    out.sort(key=lambda e: e["path"])
    return out


def render_tree(build_id: str, current_dir: str) -> dict:
    """{'dirs': [...], 'files': [...]} для ТЕКУЩЕЙ директории
    (current_dir — относительный путь без слэша на конце, '' — корень).
    Подпапка — агрегированные file_count/total_size, component=True
    только если ВСЕ файлы под ней компонентные (смешанная папка
    — обычный ✏️ значок, компонентная целиком — 🔒)."""
    prefix = f"{current_dir}/" if current_dir else ""
    dirs: Dict[str, dict] = {}
    files = []
    for f in visible_files(build_id):
        path = f["path"]
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if not rest:
            continue
        if "/" in rest:
            dirname = rest.split("/", 1)[0]
            d = dirs.setdefault(dirname, {
                "name": dirname, "path": prefix + dirname,
                "file_count": 0, "total_size": 0, "component": True,
            })
            d["file_count"] += 1
            d["total_size"] += f["size"]
            d["component"] = d["component"] and f["component"]
        else:
            files.append({"name": rest, "path": path, "size": f["size"], "component": f["component"]})
    return {
        "dirs": sorted(dirs.values(), key=lambda d: d["name"]),
        "files": sorted(files, key=lambda f: f["name"]),
    }
