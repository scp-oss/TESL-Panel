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
# TLS — Cloudflare Origin Certificate, НЕ certbot/Let's Encrypt: порты
# 80/443 на этом сервере уже заняты другими проектами под certbot,
# трогать их конфиги нельзя (тот же принцип, что у setup_depot_nginx.sh
# в TESL-Manager). ПЕРЕД запуском:
#   1. Cloudflare -> SSL/TLS -> Origin Server -> Create Certificate
#      (15 лет, бесплатно) для домена панели.
#   2. Сохрани на сервере:
#        <PROJECT_DIR>/ssl/origin.pem   (сертификат)
#        <PROJECT_DIR>/ssl/origin.key   (приватный ключ)
#      (пути можно переопределить --cert/--key)
#   3. В Cloudflare: A/AAAA-запись на этот сервер, статус "Proxied"
#      (оранжевое облако), SSL/TLS mode = "Full (strict)".
#
# Использование (первый запуск — --domain и --storage-root ОБЯЗАТЕЛЬНЫ,
# panel.env ещё не существует, брать значения неоткуда; скрипт сам
# генерирует upload-токен и печатает его в конце, СОХРАНИ его, он нужен
# для настройки TESL-Manager):
#   sudo ./infra/deploy.sh --domain panel.example.com \
#       --storage-root /mnt/1tb-1 --projects TESVAE
#
# Повторный запуск (обновление кода/конфига) — идемпотентен, токен/секрет
# сессии сохраняются (перечитываются из уже существующего panel.env, не
# перегенерируются). --domain/--storage-root тоже можно не передавать —
# если panel.env уже существует, они читаются оттуда же:
#   git pull && sudo ./infra/deploy.sh
# Явно передавать их всё ещё можно (например, чтобы СМЕНИТЬ домен/путь
# хранилища) — флаг всегда имеет приоритет над тем, что уже в panel.env.

set -euo pipefail

DOMAIN=""
STORAGE_ROOT=""
PROJECTS="TESVAE"
PORT="8090"
SERVICE_USER="tesl-panel"
UPLOAD_TOKEN=""
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_PATH=""
KEY_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)        DOMAIN="$2"; shift 2 ;;
        --storage-root)  STORAGE_ROOT="$2"; shift 2 ;;
        --projects)      PROJECTS="$2"; shift 2 ;;
        --port)          PORT="$2"; shift 2 ;;
        --service-user)  SERVICE_USER="$2"; shift 2 ;;
        --upload-token)  UPLOAD_TOKEN="$2"; shift 2 ;;
        --cert)          CERT_PATH="$2"; shift 2 ;;
        --key)           KEY_PATH="$2"; shift 2 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
done

# Дефолты, зависящие от PROJECT_DIR — считаются ПОСЛЕ парсинга аргументов
# (тот же принцип, что и в setup_depot_nginx.sh), только если --cert/--key
# не заданы явно по отдельности.
: "${CERT_PATH:=$PROJECT_DIR/ssl/origin.pem}"
: "${KEY_PATH:=$PROJECT_DIR/ssl/origin.key}"

if [[ $EUID -ne 0 ]]; then
    echo "Запускать через sudo/от root — нужно писать в /etc/systemd, /etc/nginx." >&2
    exit 1
fi

# ── --domain/--storage-root необязательны на ПОВТОРНОМ запуске, если уже
#    есть panel.env от предыдущего деплоя — читаем оттуда как fallback
#    (тот же принцип, что уже был у UPLOAD_TOKEN/SECRET_KEY ниже: не
#    перегенерировать то, что уже настроено). На первом запуске файла
#    ещё нет — флаги остаются обязательными, ошибка ниже не изменилась.
_EARLY_ENV_FILE="$PROJECT_DIR/panel.env"
if [[ -f "$_EARLY_ENV_FILE" ]]; then
    if [[ -z "$DOMAIN" ]]; then
        DOMAIN="$(grep -oP '(?<=^TESL_PANEL_DOMAIN=).*' "$_EARLY_ENV_FILE" || true)"
        [[ -n "$DOMAIN" ]] && echo "-> --domain не передан, беру из panel.env: $DOMAIN"
    fi
    if [[ -z "$STORAGE_ROOT" ]]; then
        STORAGE_ROOT="$(grep -oP '(?<=^TESL_PANEL_STORAGE_ROOT=).*' "$_EARLY_ENV_FILE" || true)"
        [[ -n "$STORAGE_ROOT" ]] && echo "-> --storage-root не передан, беру из panel.env: $STORAGE_ROOT"
    fi
fi
if [[ -z "$DOMAIN" || -z "$STORAGE_ROOT" ]]; then
    echo "Нужны минимум --domain и --storage-root (в первый раз — явно;" >&2
    echo "при повторном деплое достаточно, если они уже есть в panel.env" >&2
    echo "от предыдущего запуска). См. докстринг файла за примером." >&2
    exit 1
fi
if [[ ! -f "$CERT_PATH" || ! -f "$KEY_PATH" ]]; then
    echo "❌ Не найден Cloudflare Origin Certificate:" >&2
    echo "     $CERT_PATH" >&2
    echo "     $KEY_PATH" >&2
    echo "   Получи его в Cloudflare (SSL/TLS -> Origin Server -> Create Certificate)" >&2
    echo "   и положи по этим путям (или передай --cert/--key), см. докстринг файла." >&2
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

# ── panel.env — токен/секрет сессии генерируются один раз, сохраняются
#    между запусками ──────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/panel.env"
SECRET_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    if [[ -z "$UPLOAD_TOKEN" ]]; then
        echo "-> panel.env уже существует, токен НЕ перегенерирую (передай --upload-token, если нужно сменить)"
        UPLOAD_TOKEN="$(grep -oP '(?<=^TESL_PANEL_UPLOAD_TOKEN=).*' "$ENV_FILE" || true)"
    fi
    SECRET_KEY="$(grep -oP '(?<=^TESL_PANEL_SECRET_KEY=).*' "$ENV_FILE" || true)"
fi
if [[ -z "$UPLOAD_TOKEN" ]]; then
    UPLOAD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    echo "-> сгенерирован новый upload-токен"
fi
if [[ -z "$SECRET_KEY" ]]; then
    # Отдельный от UPLOAD_TOKEN секрет — подписывает cookie сессии в
    # /admin, менять его отдельно от токена смысла нет, но раз уж Flask
    # ожидает именно app.secret_key, а не сам токен, — не переиспользуем
    # один параметр под две разные роли (подпись cookie vs пароль входа).
    SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    echo "-> сгенерирован новый секрет сессии"
fi

cat > "$ENV_FILE" <<EOF
TESL_PANEL_STORAGE_ROOT=$STORAGE_ROOT
TESL_PANEL_UPLOAD_TOKEN=$UPLOAD_TOKEN
TESL_PANEL_SECRET_KEY=$SECRET_KEY
TESL_PANEL_PROJECTS=$PROJECTS
TESL_PANEL_DOMAIN=$DOMAIN
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

# ── sudoers для self-update (идемпотентно, тот же принцип, что
#    ensure_panel_runtime_grants() в z2r_autobench — литеральные команды,
#    без wildcard-путей, visudo -cf проверяет ПЕРЕД тем, как заменить
#    рабочий файл) — panel/self_update.py::apply_update() дёргает эти
#    ровно две команды через `sudo -n`, ничего шире. ────────────────────────
SUDOERS_FILE="/etc/sudoers.d/tesl-panel-self-update"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/git -C $PROJECT_DIR pull --ff-only
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart tesl-panel.service
EOF
if visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
    install -m 440 "$SUDOERS_TMP" "$SUDOERS_FILE"
    echo "-> sudoers-грант self-update установлен ($SUDOERS_FILE)"
else
    echo "⚠️  Сгенерированный sudoers-файл не прошёл visudo -cf — self-update из /admin/settings работать не будет, остальной деплой продолжается" >&2
fi
rm -f "$SUDOERS_TMP"

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
    -e "s#__ORIGIN_CERT__#$CERT_PATH#g" \
    -e "s#__ORIGIN_KEY__#$KEY_PATH#g" \
    "$PROJECT_DIR/infra/nginx-tesl-panel.conf.template" > "$NGINX_CONF"

ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/$DOMAIN.conf"
nginx -t
systemctl reload nginx

echo
echo "== Готово =="
echo "Проверить: curl -I https://$DOMAIN/health"
echo "(убедись, что в Cloudflare для этого домена включено Proxied +"
echo " SSL/TLS mode = Full (strict) — иначе TLS будет невалиден на edge)"
echo
echo "Upload-токен для TESL-Manager (введи в его настройках публикации):"
echo "  $UPLOAD_TOKEN"
echo
echo "Вход в /admin одной ссылкой (см. /admin/login?token=... в CLAUDE.md):"
echo "  https://$DOMAIN/admin/login?token=$UPLOAD_TOKEN"
