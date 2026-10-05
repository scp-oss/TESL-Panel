#!/usr/bin/env bash
# === TESL-PANEL: update from git + launch ===
#
# Тот же паттерн, что update_and_run.bat в TESL/TESL-Manager (десктоп-
# приложения): git-синк на origin/main + запуск, одним скриптом, без
# ручного git pull перед каждым рестартом. Для production-деплоя через
# systemd (infra/deploy.sh) это НЕ замена — тот путь обновляется через
# самообновление из /admin/settings (panel/self_update.py, git pull +
# systemctl restart, см. CLAUDE.md "Self-update панели") и управляется
# как systemd-юнит, не руками. Этот скрипт — для ручного/разового
# запуска (тест свежего чекаута, сервер без систему systemd, запуск не
# из-под root) — если на сервере уже настроен tesl-panel.service,
# использовать его, а не параллельно гонять этот скрипт тем же портом.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== TESL-PANEL: update from git + launch ==="
echo

# Hard-sync to origin/main, не git pull — гарантия, что это ровно то,
# что на GitHub, без двусмысленности fast-forward/errorlevel. Не трогает
# untracked файлы (panel.env, .venv/, data/ — все в .gitignore), только
# отслеживаемые — тот же принцип, что у update_and_run.bat.
git fetch origin main
git reset --hard origin/main

echo
echo "Текущий коммит:"
git log -1 --oneline
echo

# Венв создаётся один раз, дальше переиспользуется — установка пакетов
# идемпотентна (pip install по уже спутствующим версиям — быстрый no-op).
if [[ ! -d .venv ]]; then
    echo "Создаю venv..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

# panel.env — то же, что EnvironmentFile= у systemd-юнита (см.
# infra/tesl-panel.service.template) — если файла нет, приложение
# падает на недостающих обязательных переменных (TESL_PANEL_UPLOAD_TOKEN
# и т.д., см. panel/config.py) с понятной ошибкой, а не тихо.
if [[ -f panel.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source panel.env
    set +a
else
    echo "⚠️  panel.env не найден — приложение возьмёт настройки только из"
    echo "    уже экспортированных переменных окружения (TESL_PANEL_*)."
fi

# Параметры запуска — редактируй/переопределяй здесь или через env перед
# вызовом скрипта (PORT=9000 ./run.sh, и т.п.). Дефолты зеркалят
# тот же gunicorn-вызов, что реально использует systemd-юнит.
PORT="${PORT:-8080}"
WORKERS="${WORKERS:-2}"
THREADS="${THREADS:-16}"

echo
echo "Запускаю на 127.0.0.1:${PORT} (${WORKERS} воркеров x ${THREADS} потоков)..."
echo

# Любые доп. аргументы, переданные этому скрипту, идут напрямую в
# gunicorn (например ./run.sh --reload для разработки).
exec .venv/bin/gunicorn \
    --workers "$WORKERS" --threads "$THREADS" --worker-class gthread \
    --bind "127.0.0.1:${PORT}" --timeout 300 \
    "$@" \
    wsgi:app
