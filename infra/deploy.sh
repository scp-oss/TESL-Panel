#!/usr/bin/env bash
# ==================== deploy.sh ====================
# Разворачивает TESL-Panel на сервере: venv + gunicorn под systemd + nginx
# reverse-proxy перед ним. Запускать на самом сервере (root по SSH), не
# отсюда — у Claude-сессии нет exec-доступа к продакшену, тот же принцип,
# что и у setup_depot_nginx.sh в TESL-Manager.
#
# Предполагается, что скрипт запускается ИЗ КОРНЯ уже склонированного
# репозитория (PROJECT_DIR по умолчанию — реальный путь этого чекаута,
# определяется автоматически) — venv и systemd WorkingDirectory смотрят
# прямо в него, копировать файлы никуда не нужно; `git pull` в этой же
# папке + повторный запуск deploy.sh — штатный способ обновиться.
#
# Использование (первый запуск — генерирует upload-токен сам и печатает
# его в конце, СОХРАНИ его, он нужен для настройки TESL-Manager):
#   sudo ./infra/deploy.sh --domain panel.example.com \
#       --storage-root /mnt/1tb-1 --projects TESVAE
#
# Повторный запуск (обновление кода/конфига) — идемпотентен, тот же токен
# сохраняется (перечитывается из уже существующего panel.env, не
# перегенерируется):
#   git pull && sudo ./infra/deploy.sh --domain panel.example.com \
#       --storage-root /mnt/1tb-1 --projects TESVAE
#
# После первого запуска ОТДЕЛЬНО, вручную:
#   sudo certbot --nginx -d panel.example.com
# (сам deploy.sh не трогает TLS/certbot — тот же принцип, что и у
# setup_depot_nginx.sh: не мешать существующим сертификатам/сервисам на
# этой же машине; certbot --nginx сам допишет ssl_* в конфиг, ничего не
# ломая на соседних server-блоках).

set -euo pipefail

DOMAIN=""
STORAGE_ROOT=""
PROJECTS="TESVAE"
PORT="8090"
SERVICE_USER="tesl-panel"
UPLOAD_TOKEN=""
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)        DOMAIN="$2"; shift 2 ;;
        --storage-root)  STORAGE_ROOT="$2"; shift 2 ;;
        --projects)      PROJECTS="$2"; shift 2 ;;
        --port)          PORT="$2"; shift 2 ;;
        --service-user)  SERVICE_USER="$2"; shift 2 ;;
        --upload-token)  UPLOAD_TOKEN="$2"; shift 2 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Запускать через sudo/от root — нужно писать в /etc/systemd, /etc/nginx." >&2
    exit 1
fi
if [[ -z "$DOMAIN" || -z "$STORAGE_ROOT" ]]; then
    echo "Нужны минимум --domain и --storage-root. См. докстринг файла за примером." >&2
    exit 1
fi

echo "== TESL-Panel deploy =="
echo "  Каталог проекта: $PROJECT_DIR"
echo "  Домен:            $DOMAIN"
echo "  Storage root:     $STORAGE_ROOT"
echo "  Проекты:          $PROJECTS"
echo "  Порт (локальный): $PORT"

# ── Сервисный пользователь (идемпотентно, тот же принцип, что
#    ensure_wsrelay_user()/ensure_panel_runtime_grants() в z2r_autobench —
#    выполняется на каждом запуске, не только при первой установке) ──────
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "-> создаю системного пользователя $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ── Storage root — создаём, если ещё нет, владелец — сервисный юзер ──────
mkdir -p "$STORAGE_ROOT"
chown "$SERVICE_USER:$SERVICE_USER" "$STORAGE_ROOT"

# ── venv + зависимости ────────────────────────────────────────────────────
if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
    echo "-> создаю venv"
    python3 -m venv "$PROJECT_DIR/.venv"
fi
"$PROJECT_DIR/.venv/bin/pip" install -q --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

# ── panel.env — токен генерируется один раз, сохраняется между запусками ──
ENV_FILE="$PROJECT_DIR/panel.env"
if [[ -f "$ENV_FILE" && -z "$UPLOAD_TOKEN" ]]; then
    echo "-> panel.env уже существует, токен НЕ перегенерирую (передай --upload-token, если нужно сменить)"
    UPLOAD_TOKEN="$(grep -oP '(?<=^TESL_PANEL_UPLOAD_TOKEN=).*' "$ENV_FILE" || true)"
fi
if [[ -z "$UPLOAD_TOKEN" ]]; then
    UPLOAD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    echo "-> сгенерирован новый upload-токен"
fi

cat > "$ENV_FILE" <<EOF
TESL_PANEL_STORAGE_ROOT=$STORAGE_ROOT
TESL_PANEL_UPLOAD_TOKEN=$UPLOAD_TOKEN
TESL_PANEL_PROJECTS=$PROJECTS
EOF
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ── systemd unit из шаблона ────────────────────────────────────────────────
UNIT_PATH="/etc/systemd/system/tesl-panel.service"
sed \
    -e "s#__SERVICE_USER__#$SERVICE_USER#g" \
    -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
    -e "s#__PORT__#$PORT#g" \
    "$PROJECT_DIR/infra/tesl-panel.service.template" > "$UNIT_PATH"

# WorkingDirectory/venv должны быть читаемы сервисным пользователем —
# сам чекаут репозитория остаётся во владении того, кто его склонировал
# (обычно root), сервисному юзеру достаточно прав на чтение+исполнение.
chmod -R o+rX "$PROJECT_DIR"

systemctl daemon-reload
systemctl enable tesl-panel.service >/dev/null
systemctl restart tesl-panel.service
sleep 1
if ! systemctl is-active --quiet tesl-panel.service; then
    echo "❌ Сервис не запустился — journalctl -u tesl-panel.service за подробностями" >&2
    exit 1
fi
echo "-> tesl-panel.service активен (127.0.0.1:$PORT)"

# ── nginx — свой конфиг внутри PROJECT_DIR, симлинк в sites-enabled
#    (тот же принцип "всё в одном месте", что и у tesl-depot) ────────────
mkdir -p "$PROJECT_DIR/nginx"
NGINX_CONF="$PROJECT_DIR/nginx/$DOMAIN.conf"
sed \
    -e "s#__DOMAIN__#$DOMAIN#g" \
    -e "s#__PORT__#$PORT#g" \
    "$PROJECT_DIR/infra/nginx-tesl-panel.conf.template" > "$NGINX_CONF"

ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/$DOMAIN.conf"
nginx -t
systemctl reload nginx

echo
echo "== Готово =="
echo "HTTP уже работает: curl -I http://$DOMAIN/health"
echo
echo "Дальше вручную (это deploy.sh НЕ делает сам — TLS отдельный шаг):"
echo "  sudo certbot --nginx -d $DOMAIN"
echo
echo "Upload-токен для TESL-Manager (введи в его настройках публикации):"
echo "  $UPLOAD_TOKEN"
