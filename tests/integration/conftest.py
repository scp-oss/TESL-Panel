# ==================== tests/integration/conftest.py ====================
"""
Инфраструктура сквозного теста "TESL-Manager -> TESL-Panel -> TESL
(лаунчер)" — см. test_full_pipeline.py за сам тест и README.md в этой же
папке за то, зачем это существует и когда его гонять.

Требует чекауты СОСЕДНИХ репозиториев на диске (см. TESL_MANAGER_DIR/
TESL_LAUNCHER_DIR ниже) — этот репозиторий (TESL-Panel) один не
описывает весь протокол, только свою половину, поэтому без соседей тест
просто пропускается (`pytest.skip`, не падает) с понятным сообщением, а
не тихо зелёной галочкой на несуществующей проверке.
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import requests

THIS_REPO = Path(__file__).resolve().parents[2]


def _find_sibling(marker_rel_path: str, *candidate_names: str) -> "Path | None":
    """Проверяет не только что папка существует, но что внутри неё
    реально лежит код (marker_rel_path) — в этой же песочнице
    исторически бывали ОБА варианта регистра одновременно
    (`TESL-Manager/` — пустой чекаут без единого коммита, `tesl-manager/`
    — настоящий, с кодом); голая проверка `is_dir()` иначе тихо выбрала
    бы пустую и упала на ModuleNotFoundError вместо понятного skip."""
    for name in candidate_names:
        p = THIS_REPO.parent / name
        if (p / marker_rel_path).is_file():
            return p
    return None


TESL_MANAGER_DIR = _find_sibling(
    "depot_sync_manager/chunk_manager.py", "TESL-Manager", "tesl-manager",
)
TESL_LAUNCHER_DIR = _find_sibling(
    "launcher/core/panel_client.py", "TESL", "tesl",
)


def _skip_if_no_siblings():
    missing = []
    if TESL_MANAGER_DIR is None:
        missing.append("TESL-Manager (ожидался ../TESL-Manager или ../tesl-manager)")
    if TESL_LAUNCHER_DIR is None:
        missing.append("TESL (ожидался ../TESL или ../tesl)")
    if missing:
        pytest.skip(
            "Сквозной тест требует соседние чекауты рядом с этим репозиторием: "
            + "; ".join(missing) + " — см. tests/integration/README.md"
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def panel_server(tmp_path_factory):
    """Настоящий (не мок) werkzeug-сервер TESL-Panel на localhost, со
    свежим временным STORAGE_ROOT — тот же самый код (`panel.app.
    create_app()`), что и в проде, просто временное хранилище и
    случайный порт. Возвращает (base_url, token)."""
    _skip_if_no_siblings()

    storage_root = tmp_path_factory.mktemp("panel_storage")
    token = "e2e-test-token"
    os.environ["TESL_PANEL_STORAGE_ROOT"] = str(storage_root)
    os.environ["TESL_PANEL_UPLOAD_TOKEN"] = token
    os.environ["TESL_PANEL_SECRET_KEY"] = "e2e-test-secret"

    from panel.app import create_app
    app = create_app()

    port = _free_port()
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if requests.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("панель не поднялась за 5с")

    yield base_url, token, str(storage_root)

    srv.shutdown()


@pytest.fixture(scope="module")
def sibling_dirs():
    """(tesl_manager_dir, tesl_launcher_dir) — избегаем относительного
    импорта `from .conftest import X` в test_full_pipeline.py: он требует
    настоящий пакет (`__init__.py`) и зависит от режима импорта pytest,
    который эта команда/CI могут не совпадать. Фикстура — единственный
    способ передать эти пути тесту, без вопроса про import-mode."""
    _skip_if_no_siblings()
    return TESL_MANAGER_DIR, TESL_LAUNCHER_DIR
