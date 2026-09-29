# ==================== tests/integration/test_full_pipeline.py ====================
"""
Сквозной тест всего протокола: TESL-Manager публикует (два раунда, на
один build_id) -> TESL-Panel хранит и отдаёт -> TESL (лаунчер) качает и
верифицирует. Три отдельных репозитория, которые физически не могут
существовать по отдельности (общий протокол — depot_manifest.json/
chunk_index.db/extras_manifest.json/отчёты), но исторически проверялись
только руками, по одному разу, при каждой правке — и оба реальных бага
2026-09-29 (коллизия имён pack-файлов при повторной публикации, 401 на
публичных отчётах) прошли бы этот тест сразу, если бы он существовал
раньше. См. README.md в этой же папке за то, когда его гонять.

Сам тест — тонкая обёртка (`pytest`, HTTP-проверки напрямую) вокруг двух
подпроцессов (`manager_publish.py`/`launcher_verify.py`) — см. их
докстринги за то, почему не импортируются напрямую сюда (коллизия
модулей `panel_client.py`/`config.py` между TESL-Manager и TESL при
попытке импортировать оба в один процесс).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

THIS_DIR = Path(__file__).resolve().parent


def _run(script: str, args, env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(THIS_DIR / script), *args],
        env=env, capture_output=True, text=True, timeout=120,
    )


def test_full_pipeline(panel_server, sibling_dirs, tmp_path):
    base_url, token, storage_root = panel_server
    tesl_manager_dir, tesl_launcher_dir = sibling_dirs
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    manager_out = tmp_path / "manager_result.json"

    # ── 1. TESL-Manager: два раунда публикации на один build_id ─────────
    r = _run(
        "manager_publish.py",
        [base_url, token, str(src_dir), str(manager_out)],
        {"TESL_MANAGER_SRC_DIR": str(tesl_manager_dir / "depot_sync_manager")},
    )
    assert r.returncode == 0, (
        f"manager_publish.py упал:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    info = json.loads(manager_out.read_text(encoding="utf-8"))
    build_id = info["build_id"]

    # ── 2. Прямые HTTP-проверки на стороне панели (в этом же процессе,
    #    у нас есть прямой доступ к её storage/app) ──────────────────────
    r_manifest = requests.get(f"{base_url}/api/depot/{build_id}/depot_manifest.json", timeout=10)
    assert r_manifest.status_code == 200
    manifest = r_manifest.json()
    assert info["untouched_path"] in manifest["files"], (
        "файл раунда 1 отсутствует в итоговом манифесте после раунда 2"
    )

    r_index = requests.get(f"{base_url}/api/depot/{build_id}/chunk_index.db", timeout=10)
    assert r_index.status_code == 200

    # manager_log/manager_crash по-прежнему требуют токен (не должны
    # стать публичными вслед за crash/debug_log) — прямая регрессия на
    # правку 2026-09-29.
    r_unauth = requests.put(
        f"{base_url}/api/reports/manager_log/op/09.29.2026-00.00.00/app.log",
        data=b"x", timeout=10,
    )
    assert r_unauth.status_code == 401, "manager_log должен требовать Bearer-токен"

    # ── 3. TESL (лаунчер): скачать реальным клиентским кодом, сверить
    #    байт-в-байт, проверить нулевые ошибки скачивания чанков ────────
    launcher_out = tmp_path / "launcher_result.json"
    r = _run(
        "launcher_verify.py",
        [base_url, str(manager_out), str(launcher_out)],
        {"TESL_LAUNCHER_SRC_DIR": str(tesl_launcher_dir / "launcher")},
    )
    result = json.loads(launcher_out.read_text(encoding="utf-8")) if launcher_out.exists() else {}
    assert r.returncode == 0 and result.get("ok"), (
        f"launcher_verify.py нашёл проблемы: {result.get('errors')}\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert result["files_checked"] >= 5

    # ── 4. Отчёты (crash/debug_log), отправленные launcher_verify.py,
    #    реально видны на панели ──────────────────────────────────────
    from panel import reports_storage
    os.environ["TESL_PANEL_STORAGE_ROOT"] = storage_root
    assert "e2e_test_user" in reports_storage.list_usernames("crash")
    assert "e2e_test_user" in reports_storage.list_usernames("debug_log")
