# ==================== panel/extras_manifest.py ====================
"""
Документы (.ini-шаблоны настроек), патчи (.bat/.py, применяются лаунчером
по порядку) и вспомогательные файлы патчей (patchs/) — серверная сторона
контракта `extras_manifest.json`, зеркалящая TESL-Manager::
depot_sync_manager/documents_tab.py (нэйминг патчей, enabled/order-
семантика, patchs/ без записей в манифесте) 1:1 — см. её докстринг за
полную историю решений, здесь они не переобосновываются заново.

**Зачем это здесь, а не только в TESL-Manager**: прямой запрос
пользователя (2026-09-28) — "перенести функционал менеджера кроме
заливки релизов" в панель, чтобы документами/патчами/постером можно
было управлять из браузера, без десктоп-приложения на своей машине.
Публикация НОВОЙ версии сборки (сканирование локальных компонентов,
chunking, pack_writer) остаётся исключительно в TESL-Manager — она
требует доступа к папке компонента на диске ОПЕРАТОРА, у панели такого
источника нет и не будет. Всё, что здесь реализовано, работает только
поверх уже опубликованного депо, теми же примитивами (storage.py), что
использует и generic `/admin/project/<name>/files`.

Отличие от documents_tab.py: там это HTTP-клиент (PanelHTTP) снаружи,
здесь — прямой вызов storage.py изнутри самого процесса панели (нет
своего же HTTP до самого себя). Формат `extras_manifest.json` и общий
алгоритм (upsert сохраняет enabled/order уже существующей записи,
next_order — плотная очередь, move_patch — нормализация+своп) скопированы
буквально — если контракт когда-нибудь изменится на одной стороне,
проверить другую (TESL-Manager/CLAUDE.md уже просит то же самое для
патчей внутри TESL-Manager).
"""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import List, Optional

from . import storage

MANIFEST_PATH = "extras_manifest.json"
DOCS_PREFIX = "documents/"
PATCH_PREFIX = "patch/"
PATCHFILES_PREFIX = "patchs/"   # см. documents_tab.py — буквально "patchs",
                                 # не опечатка, это уже часть публичного контракта

_PATCH_SEQ_RE = re.compile(r"^(\d+)_")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize(s: str) -> str:
    return _SANITIZE_RE.sub("-", s.strip()) or "x"


def load_manifest(build_id: str) -> dict:
    data = storage.get_bytes(build_id, MANIFEST_PATH)
    if data is None:
        return {"documents": [], "patch": []}
    try:
        m = json.loads(data.decode("utf-8"))
    except Exception:
        m = {}
    m.setdefault("documents", [])
    m.setdefault("patch", [])
    return m


def save_manifest(build_id: str, manifest: dict) -> None:
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    storage.put_bytes(build_id, MANIFEST_PATH, payload)


def _upsert_entry(manifest: dict, category: str, path: str, data: bytes, extra: Optional[dict] = None):
    entries = manifest[category]
    # Правка уже существующей записи (не создание) не должна молча сбросить
    # enabled/order в дефолт — та же причина, что documents_tab.py's
    # _upsert_entry() комментирует подробно.
    old = next((e for e in entries if e.get("path") == path), None)
    entries[:] = [e for e in entries if e.get("path") != path]
    entry = {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": old.get("enabled", True) if old else True,
    }
    if old and "order" in old:
        entry["order"] = old["order"]
    if extra:
        entry.update(extra)
    entries.append(entry)


def _remove_entry(manifest: dict, category: str, path: str):
    manifest[category] = [e for e in manifest[category] if e.get("path") != path]


def next_order(manifest: dict, category: str) -> int:
    orders = [e.get("order", i) for i, e in enumerate(manifest.get(category, []))]
    return (max(orders) + 1) if orders else 0


def set_entry_enabled(build_id: str, category: str, path: str, enabled: bool) -> None:
    manifest = load_manifest(build_id)
    for e in manifest.get(category, []):
        if e.get("path") == path:
            e["enabled"] = enabled
            break
    else:
        manifest.setdefault(category, []).append({"path": path, "enabled": enabled})
    save_manifest(build_id, manifest)


# ── Документы ────────────────────────────────────────────────────────────

def list_documents(build_id: str) -> List[dict]:
    files = storage.list_files(build_id)
    manifest = load_manifest(build_id)
    manifest_by_path = {e.get("path"): e for e in manifest.get("documents", [])}
    docs = sorted(
        (f for f in files if f["path"].startswith(DOCS_PREFIX) and f["path"] != DOCS_PREFIX),
        key=lambda f: f["path"],
    )
    out = []
    for f in docs:
        entry = manifest_by_path.get(f["path"], {})
        out.append({
            "path": f["path"],
            "name": f["path"][len(DOCS_PREFIX):],
            "size": f["size"],
            "enabled": entry.get("enabled", True),
        })
    return out


def add_document(build_id: str, filename: str, data: bytes) -> str:
    rel_path = DOCS_PREFIX + filename
    storage.put_bytes(build_id, rel_path, data)
    manifest = load_manifest(build_id)
    _upsert_entry(manifest, "documents", rel_path, data)
    save_manifest(build_id, manifest)
    return rel_path


def edit_document(build_id: str, rel_path: str, data: bytes) -> None:
    storage.put_bytes(build_id, rel_path, data)
    manifest = load_manifest(build_id)
    _upsert_entry(manifest, "documents", rel_path, data)
    save_manifest(build_id, manifest)


def delete_document(build_id: str, rel_path: str) -> bool:
    existed = storage.delete_file(build_id, rel_path)
    manifest = load_manifest(build_id)
    _remove_entry(manifest, "documents", rel_path)
    save_manifest(build_id, manifest)
    return existed


# ── Патчи ────────────────────────────────────────────────────────────────

def next_patch_seq(build_id: str) -> int:
    files = storage.list_files(build_id)
    max_seq = 0
    for f in files:
        if not f["path"].startswith(PATCH_PREFIX):
            continue
        m = _PATCH_SEQ_RE.match(f["path"][len(PATCH_PREFIX):])
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def list_patches(build_id: str) -> List[dict]:
    files = storage.list_files(build_id)
    manifest = load_manifest(build_id)
    manifest_by_path = {e.get("path"): e for e in manifest.get("patch", [])}
    raw = [f for f in files if f["path"].startswith(PATCH_PREFIX) and f["path"] != PATCH_PREFIX]
    patches = []
    for f in raw:
        entry = manifest_by_path.get(f["path"], {})
        patches.append({
            "path": f["path"],
            "name": f["path"][len(PATCH_PREFIX):],
            "size": f["size"],
            "enabled": entry.get("enabled", True),
            "order": entry.get("order"),
            "version": entry.get("version", ""),
            "date": entry.get("date", ""),
        })
    # Та же сортировка отображения, что и documents_tab.py::_load_patches() —
    # order-заданные первыми по возрастанию, легаси-без-order — после, по имени.
    with_order = sorted((p for p in patches if p["order"] is not None), key=lambda p: p["order"])
    without_order = sorted((p for p in patches if p["order"] is None), key=lambda p: p["path"])
    return with_order + without_order


def add_patch(build_id: str, filename: str, version: str, data: bytes) -> str:
    seq = next_patch_seq(build_id)
    version_s = sanitize(version) if version.strip() else "x"
    date_s = date.today().isoformat()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bat"
    out_name = f"{seq:04d}_v{version_s}_{date_s}.{ext}"
    rel_path = PATCH_PREFIX + out_name
    storage.put_bytes(build_id, rel_path, data)
    manifest = load_manifest(build_id)
    order = next_order(manifest, "patch")
    _upsert_entry(manifest, "patch", rel_path, data, extra={
        "seq": seq, "version": version.strip(), "date": date_s,
        "enabled": True, "order": order,
    })
    save_manifest(build_id, manifest)
    return rel_path


def delete_patch(build_id: str, rel_path: str) -> bool:
    existed = storage.delete_file(build_id, rel_path)
    manifest = load_manifest(build_id)
    _remove_entry(manifest, "patch", rel_path)
    save_manifest(build_id, manifest)
    return existed


def move_patch(build_id: str, rel_path: str, delta: int) -> bool:
    """delta=-1 — раньше остальных (выше), +1 — позже (ниже). Та же
    нормализация-в-плотную-последовательность-затем-своп, что
    documents_tab.py::_move_patch() — см. её комментарий за обоснование
    (иначе своп при дырках/дублях старых order даёт неоднозначный результат)."""
    manifest = load_manifest(build_id)
    entries = manifest.get("patch", [])
    entries.sort(key=lambda e: (e.get("order") is None, e.get("order", 0), e.get("path", "")))
    for i, e in enumerate(entries):
        e["order"] = i
    idx = next((i for i, e in enumerate(entries) if e.get("path") == rel_path), None)
    if idx is None:
        return False
    new_idx = idx + delta
    if not (0 <= new_idx < len(entries)):
        return False
    entries[idx]["order"], entries[new_idx]["order"] = entries[new_idx]["order"], entries[idx]["order"]
    manifest["patch"] = entries
    save_manifest(build_id, manifest)
    return True


# ── Файлы патчей (patchs/) ──────────────────────────────────────────────
# Простое хранилище, без записей в extras_manifest.json — см. модульный
# докстринг/documents_tab.py за то, почему.

def list_patchfiles(build_id: str) -> List[dict]:
    files = storage.list_files(build_id)
    raw = [f for f in files if f["path"].startswith(PATCHFILES_PREFIX) and f["path"] != PATCHFILES_PREFIX]
    out = [{"path": f["path"], "name": f["path"][len(PATCHFILES_PREFIX):], "size": f["size"]} for f in raw]
    out.sort(key=lambda f: f["name"])
    return out


def add_patchfile(build_id: str, filename: str, data: bytes) -> str:
    rel_path = PATCHFILES_PREFIX + filename
    storage.put_bytes(build_id, rel_path, data)
    return rel_path


def delete_patchfile(build_id: str, rel_path: str) -> bool:
    return storage.delete_file(build_id, rel_path)
