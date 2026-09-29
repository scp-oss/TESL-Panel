# ==================== tests/integration/launcher_verify.py ====================
"""
"TESL-лаунчер сторона" сквозного теста — своим подпроцессом, см.
manager_publish.py за то, почему не импортируется напрямую в тот же
процесс (модуль `panel_client.py`/`config.py` называется одинаково в
обоих репозиториях — коллизия через `sys.modules`).

Скачивает реально опубликованную (manager_publish.py) сборку через
РЕАЛЬНЫЙ клиентский код лаунчера — `core.panel_client.PanelDepotClient` +
`core.chunk_installer.ChunkInstaller` (тот же путь, что и
`core/workers.py::DownloadWorker.run()`, просто без QThread/Qt-сигналов
вокруг — сама бизнес-логика не отличается ни строкой). Проверяет:

1. Байт-в-байт совпадение каждого установленного файла с исходником на
   диске (`src_dir`, из manager_publish.py) — в частности
   `untouched_path` (файл раунда 1, не тронутый раундом 2 — именно на
   нём проявлялся баг коллизии имён pack-файлов 2026-09-29, см.
   TESL-Manager/CLAUDE.md).
2. `client.chunk_error_summary()` пуст — НИ ОДНОГО отказа скачивания
   чанка. Это прямая регрессионная проверка на живой баг: если коллизия
   вернётся, здесь появятся `sha256_mismatch`/`http_416`, тест упадёт
   сразу, без необходимости ждать реальной установки у пользователя,
   чтобы это заметить (именно так эта регрессия и была найдена в первый
   раз — с этим тестом она была бы найдена раньше).
3. `extras_manifest.json` (документы/патчи/постер) читается корректно
   через `fetch_extras_manifest()`/`fetch_poster()`.
4. Отправка крэш-/debug-отчёта — реальный `core.crash_logger.
   upload_files()`, публичный путь (без токена, см. TESL-Panel/CLAUDE.md
   "crash/debug_log отчёты сделаны публичными").

Аргументы: <panel_base_url> <in_json> <out_result_json>
"""
import hashlib
import json
import os
import pathlib
import sys
import tempfile

PANEL_BASE_URL, IN_JSON, OUT_JSON = sys.argv[1:4]

LAUNCHER_DIR = os.environ["TESL_LAUNCHER_SRC_DIR"]
sys.path.insert(0, LAUNCHER_DIR)

info = json.loads(pathlib.Path(IN_JSON).read_text(encoding="utf-8"))
build_id = info["build_id"]
src_dir = pathlib.Path(info["src_dir"])
untouched_path = info["untouched_path"]
round1_version_key = info["round1_version_key"]
round2_only_path = info["round2_only_path"]

import config as launcher_config  # noqa: E402
launcher_config.PANEL_BASE_URL = PANEL_BASE_URL

from core.panel_client import PanelDepotClient  # noqa: E402
from core.chunk_installer import ChunkInstaller  # noqa: E402
from core.workers import _load_panel_manifest  # noqa: E402
from core.crash_logger import upload_files as report_upload  # noqa: E402

result = {"ok": False, "errors": []}


def fail(msg: str):
    result["errors"].append(msg)


try:
    client = PanelDepotClient(build_id, base_url=PANEL_BASE_URL)
    loaded = _load_panel_manifest(client)
    if loaded is None:
        fail("_load_panel_manifest() вернул None — манифест не получен")
    else:
        entries, meta = loaded
        install_dir = pathlib.Path(tempfile.mkdtemp(prefix="tesl_e2e_install_"))
        installer = ChunkInstaller(client=client, local_dir=install_dir, max_workers=8)
        ok = installer.install(entries)
        error_summary = client.chunk_error_summary()

        if error_summary:
            fail(f"chunk_error_summary НЕ пуст (регрессия): {error_summary}")
        if not ok:
            fail("ChunkInstaller.install() вернул False")

        checked = 0
        for entry in entries:
            path = entry.path
            installed = install_dir / path
            original = src_dir / path
            if not installed.is_file():
                fail(f"файл не установлен: {path}")
                continue
            if not original.is_file():
                fail(f"исходный файл отсутствует для сверки: {path}")
                continue
            a = hashlib.sha256(installed.read_bytes()).hexdigest()
            b = hashlib.sha256(original.read_bytes()).hexdigest()
            if a != b:
                fail(f"содержимое не совпадает с оригиналом: {path}")
            checked += 1
        if untouched_path not in {e.path for e in entries}:
            fail(f"untouched_path {untouched_path!r} не найден в манифесте вообще")
        result["files_checked"] = checked

        # ── Откат на историческую версию (2026-09-29) ──────────────────
        # Прямая регрессия на "версионность + откат из лаунчера": список
        # версий реально содержит ≥2 записи (см. manager_publish.py — два
        # раунда), а установка ПО version_key раунда 1 реально ставит
        # набор файлов раунда 1 — без round2_only_path, с тем же
        # untouched_path. Тот же ChunkInstaller/PanelDepotClient, что и
        # выше — единственная разница — version_key передан явно.
        versions = client.list_versions()
        if len(versions) < 2:
            fail(f"откат: ожидалось ≥2 версии в истории, получено {len(versions)}")
        version_keys = {v["version_key"] for v in versions}
        if round1_version_key not in version_keys:
            fail(f"откат: version_key раунда 1 ({round1_version_key}) не найден в списке версий панели: {version_keys}")
        else:
            rollback_loaded = _load_panel_manifest(client, round1_version_key)
            if rollback_loaded is None:
                fail(f"откат: _load_panel_manifest(version_key={round1_version_key!r}) вернул None")
            else:
                rollback_entries, _rollback_meta = rollback_loaded
                rollback_paths = {e.path for e in rollback_entries}
                if round2_only_path in rollback_paths:
                    fail(f"откат: манифест версии раунда 1 содержит файл раунда 2 ({round2_only_path}) — версии не различаются")
                if untouched_path not in rollback_paths:
                    fail(f"откат: манифест версии раунда 1 не содержит собственный файл раунда 1 ({untouched_path})")
                rollback_dir = pathlib.Path(tempfile.mkdtemp(prefix="tesl_e2e_rollback_"))
                rollback_installer = ChunkInstaller(client=client, local_dir=rollback_dir, max_workers=8)
                rollback_ok = rollback_installer.install(rollback_entries)
                if not rollback_ok:
                    fail("откат: ChunkInstaller.install() для версии раунда 1 вернул False")
                rollback_error_summary = client.chunk_error_summary()
                if rollback_error_summary:
                    fail(f"откат: chunk_error_summary НЕ пуст после установки версии раунда 1: {rollback_error_summary}")
                if (rollback_dir / round2_only_path).exists():
                    fail(f"откат: файл раунда 2 физически появился на диске после установки версии раунда 1 ({round2_only_path})")
                if not (rollback_dir / untouched_path).is_file():
                    fail(f"откат: собственный файл раунда 1 не установился ({untouched_path})")

    extras = client.fetch_extras_manifest()
    if not any(d["path"] == "documents/Skyrim.ini" and d["enabled"] for d in extras.get("documents", [])):
        fail("extras_manifest: ожидаемый документ не найден или enabled=false")
    if not any(p["path"] == "patch/0001_v1.0.0_x.bat" for p in extras.get("patch", [])):
        fail("extras_manifest: ожидаемый патч не найден")

    poster = client.fetch_poster()
    if poster is None or hashlib.sha256(poster).hexdigest() != info.get("poster_sha256"):
        fail("постер не скачался или не совпадает по содержимому")

    client.close()

    # ── Публичная отправка отчётов (без токена) ─────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="wb") as f:
        f.write(b"synthetic crash trace for integration test\n")
        crash_path = pathlib.Path(f.name)
    ok_crash, msg_crash = report_upload("e2e_test_user", [crash_path], log=lambda s: None, report_type="crash")
    if not ok_crash:
        fail(f"публичная отправка crash-отчёта не удалась: {msg_crash}")
    crash_path.unlink(missing_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="wb") as f:
        f.write(b"synthetic debug log for integration test\n")
        debug_path = pathlib.Path(f.name)
    ok_debug, msg_debug = report_upload("e2e_test_user", [debug_path], log=lambda s: None, report_type="debug_log")
    if not ok_debug:
        fail(f"публичная отправка debug_log-отчёта не удалась: {msg_debug}")
    debug_path.unlink(missing_ok=True)

except Exception as e:
    import traceback
    fail(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

result["ok"] = not result["errors"]
pathlib.Path(OUT_JSON).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
print("LAUNCHER_VERIFY_RESULT", json.dumps(result, ensure_ascii=False))
sys.exit(0 if result["ok"] else 1)
