# TESL-Panel

Тонкий веб-сервис для проекта TESL (лаунчер + менеджер сборки Skyrim SE):

1. **Страница скачивания** (`/`) — кнопка "скачать TESL.exe", ссылка
   берётся из последнего GitHub Release (`scp-oss/TESL`). Скачивание
   TESL-Manager.exe — только из `/admin`, после входа (см. п.3).
2. **API публикации депо** (`/api/depot/<build_id>/...`) — приёмник для
   новых версий сборки, пишет чанки/манифесты напрямую на диск сервера,
   в обход Nextcloud/WebDAV. Формат на диске идентичен тому, что раньше
   публиковалось на WebDAV (`chunks/<xx>/<id>`, `versions/<key>.json`,
   `depot.json`) — меняется только транспорт. `build_id` — реальный ключ
   (UUID), не имя сборки — см. `panel/builds_db.py`.
3. **Страница `/admin`** — список сборок (создать/переименовать/удалить),
   скачивание TESL-Manager.exe, файловый браузер сборки (с группировкой
   по компонентам Skyrim/MO2p/MO2ext), документы/патчи/постер
   (`/admin/project/<name>/documents`), отчёты, дашборд, настройки.
   Реестр сборок — SQLite (`<STORAGE_ROOT>/_meta/builds.db`,
   `panel/builds_db.py`), подхватывается сразу, без рестарта сервиса.

Сама раздача уже опубликованной сборки (то, что скачивает игрок через
TESL) пока остаётся на Nextcloud/WebDAV — эта панель её не подменяет, см.
`CLAUDE.md`.

## Локальный запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
TESL_PANEL_STORAGE_ROOT=./data TESL_PANEL_UPLOAD_TOKEN=devtoken \
  .venv/bin/flask --app panel.app run --port 8080
```

Откроется на `http://127.0.0.1:8080/`.

Для разового/ручного запуска на уже склонированном чекауте (не вместо
`deploy.sh`+systemd — см. его докстринг) — `./run.sh`: синкает на
`origin/main` (`git fetch` + `git reset --hard`), ставит/обновляет venv,
подхватывает `panel.env`, если он уже есть, запускает `gunicorn` тем же
набором флагов, что и systemd-юнит (`PORT`/`WORKERS`/`THREADS` —
переопределяются переменными окружения перед вызовом). Тот же паттерн,
что `update_and_run.bat` у TESL/TESL-Manager.

## Деплой на сервер

TLS — Cloudflare Origin Certificate, НЕ certbot (порты 80/443 на сервере
уже заняты другими проектами под certbot). Перед запуском получи
сертификат в Cloudflare (SSL/TLS -> Origin Server -> Create Certificate)
и положи его на сервер как `<repo>/ssl/origin.pem`/`origin.key`.

```bash
git clone https://github.com/scp-oss/TESL-Panel.git
cd TESL-Panel
sudo ./infra/deploy.sh --domain panel.example.com \
    --storage-root /mnt/1tb-1 --projects TESVAE
```

В Cloudflare для этого домена: A/AAAA-запись на сервер, "Proxied"
(оранжевое облако), SSL/TLS mode = "Full (strict)". Подробности и
дефолтные пути сертификата — см. докстринг `infra/deploy.sh`.

Скрипт сам генерирует upload-токен при первом запуске и печатает его в
конце — этот токен нужен для настройки публикации в TESL-Manager
(`backend: "panel"` в конфиге, см. его собственный CLAUDE.md).

## Переменные окружения

| Переменная                      | Назначение                                   |
|----------------------------------|-----------------------------------------------|
| `TESL_PANEL_STORAGE_ROOT`        | Корень хранилища депо на диске                |
| `TESL_PANEL_UPLOAD_TOKEN`        | Bearer-токен для записи (`PUT`) и входа в `/admin` |
| `TESL_PANEL_GITHUB_CACHE_TTL`    | Кэш ответа GitHub API, секунды (по умолч. 300)|

Чтение депо (`GET`/`HEAD`) токена не требует — та же логика, что у
read-only nginx-плана в TESL-Manager: чтение не секрет, запись — да.
