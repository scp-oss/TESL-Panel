# ==================== tests/integration/manager_publish.py ====================
"""
"TESL-Manager сторона" сквозного теста — запускается СВОИМ подпроцессом,
не импортируется напрямую (см. модульный докстринг test_full_pipeline.py
за то, почему: и TESL-Manager, и TESL-лаунчер имеют модули с одинаковыми
именами — `panel_client.py`, `config.py` — импортировать оба в один
процесс `import panel_client` вернул бы уже закэшированный чужой модуль
через `sys.modules`; изолированные подпроцессы с непересекающимся
`sys.path` — надёжнее любого трюка с `importlib`).

Делает РЕАЛЬНУЮ, не однократную публикацию — ДВА раунда на один и тот же
build_id, специально чтобы поймать класс бага 2026-09-29 (коллизия имён
pack-файлов между публикациями, см. TESL-Manager/CLAUDE.md "Критический
баг..."): раунд 1 публикует набор файлов с маленьким pack_size (много
pack-файлов на скромном объёме данных), раунд 2 добавляет НОВЫЕ файлы, не
трогая часть файлов раунда 1 — именно эти нетронутые файлы раньше молча
портились, когда раунд 2 заново нумеровал pack-файлы с единицы.

Аргументы (позиционные): <panel_base_url> <token> <src_dir> <out_json>
Пишет в <out_json>: {"build_id", "src_dir", "round1_untouched_path"} —
`round1_untouched_path` — путь (логический, как в depot_manifest.json)
файла, который публикуется В РАУНДЕ 1 и ни разу не трогается в раунде 2 —
именно на нём launcher_verify.py проверяет регрессию бага целенаправленно
(не полагаясь только на "весь набор файлов совпал", а явно указывая,
какой файл — лакмусовая бумажка).
"""
import json
import os
import pathlib
import sys

PANEL_BASE_URL, TOKEN, SRC_DIR, OUT_JSON = sys.argv[1:5]

MANAGER_DIR = os.environ["TESL_MANAGER_SRC_DIR"]
sys.path.insert(0, MANAGER_DIR)

from chunk_manager import ChunkManager  # noqa: E402
import chunk_manager as cm_mod  # noqa: E402
from depot_sync_manager import DepotSyncManager  # noqa: E402
from panel_client import PanelHTTP  # noqa: E402
import documents_tab as dt  # noqa: E402

src = pathlib.Path(SRC_DIR)


def _write(rel_path: str, data: bytes):
    p = src / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _components(*names):
    out = {}
    for name in names:
        out[name] = {"local_dir": str(src / name), "included": True, "excludes": []}
    return out


def _scan_and_publish(component_names, prev_entries, sync, cm):
    entries, _stats, component_roots = cm.scan_components(_components(*component_names))
    manifest = cm_mod.DepotManifest(app_id="e2e-pipeline-test", channel="stable", build_number=1)
    manifest.files = entries
    manifest.description = "Сквозной тест: менеджер -> панель -> лаунчер"
    remote_chunk_ids = sync.fetch_remote_chunk_ids(None)
    delta = cm.compute_delta(entries, prev_entries, remote_chunk_ids)
    ok, msg = sync.execute_sync_packed(manifest, delta, component_roots)
    if not ok:
        raise RuntimeError(f"execute_sync_packed failed: {msg}")
    return entries


def main():
    # ── Раунд 1 — файл, который НИКОГДА не тронется в раунде 2 ─────────────
    untouched_path = "Skyrim/Data/Skyrim.esm"
    _write(untouched_path, os.urandom(180_000))
    _write("Skyrim/Data/Update.esm", os.urandom(90_000))
    _write("MO2p/ModOrganizer.exe", os.urandom(70_000))

    admin = PanelHTTP(base_url=PANEL_BASE_URL, build_id="", token=TOKEN)
    build, err = admin.create_build("e2e-pipeline")
    if not build:
        raise RuntimeError(f"create_build failed: {err}")
    build_id = build["id"]
    admin.close()

    cfg = {
        "backend": "panel",
        "panel": {"base_url": PANEL_BASE_URL, "token": TOKEN, "build_id": build_id, "verify_ssl": True},
        "use_packs": True,
        # Крошечный pack_size — форсирует НЕСКОЛЬКО pack-файлов даже на
        # этом скромном объёме синтетических данных, чтобы коллизия имён
        # между раундами была реально достижима, не только теоретически
        # возможна (см. DepotSyncManager.__init__ за то, откуда берётся
        # этот ключ).
        "depot": {"disk_mode": "ssd", "pack_size": 96 * 1024},
    }

    sync = DepotSyncManager(cfg)
    cm = ChunkManager(chunk_size=32 * 1024)
    ok = sync.ensure_depot_structure()
    if not ok:
        raise RuntimeError("ensure_depot_structure failed")

    round1_entries = _scan_and_publish(["Skyrim", "MO2p"], {}, sync, cm)

    # ── Раунд 2 — добавляем файлы, Skyrim.esm/Update.esm/MO2p НЕ трогаем ──
    _write("MO2p/mods/SomeMod/plugin.esp", os.urandom(150_000))
    _write("MO2p/mods/SomeMod/textures/tex1.dds", os.urandom(200_000))
    round2_entries = _scan_and_publish(["Skyrim", "MO2p"], round1_entries, sync, cm)

    if round2_entries[untouched_path].file_hash != round1_entries[untouched_path].file_hash:
        raise RuntimeError("тестовая ошибка: 'нетронутый' файл раунда 1 внезапно изменился")

    sync.close()

    # ── Документы/патчи/файлы патчей/постер — тот же generic API, что и
    #    documents_tab.py в реальном GUI. ────────────────────────────────
    doc_client = PanelHTTP(base_url=PANEL_BASE_URL, build_id=build_id, token=TOKEN)
    ini = b"[General]\r\nbEnableFileSelection=1\r\n"
    doc_client.put("documents/Skyrim.ini", ini)
    m = dt._load_manifest(doc_client)
    dt._upsert_entry(m, "documents", "documents/Skyrim.ini", ini, extra={"enabled": True})
    patch1 = b"echo patch one\r\n"
    doc_client.put("patch/0001_v1.0.0_x.bat", patch1)
    dt._upsert_entry(m, "patch", "patch/0001_v1.0.0_x.bat", patch1, extra={
        "enabled": True, "order": 0, "seq": 1, "version": "1.0.0",
    })
    doc_client.put("patchs/nvngx_dlss.dll", os.urandom(1000))
    dt._save_manifest(doc_client, m)
    poster_bytes = b"\x89PNG\r\n" + os.urandom(500)
    doc_client.put("poster.png", poster_bytes)
    doc_client.close()

    result = {
        "build_id": build_id,
        "src_dir": str(src),
        "untouched_path": untouched_path,
        "poster_sha256": __import__("hashlib").sha256(poster_bytes).hexdigest(),
    }
    pathlib.Path(OUT_JSON).write_text(json.dumps(result), encoding="utf-8")
    print("MANAGER_PUBLISH_OK", json.dumps(result))


if __name__ == "__main__":
    main()
