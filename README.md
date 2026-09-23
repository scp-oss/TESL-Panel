# TESL-Panel

Тонкий веб-сервис для проекта TESL (лаунчер + менеджер сборки Skyrim SE):

1. **Страница скачивания** (`/`) — кнопки "скачать TESL.exe" / "скачать
   TESL-Manager.exe", ссылки берутся из последнего GitHub Release каждого
   репозитория (`scp-oss/TESL`, `scp-oss/TESL-Manager`).
2. **API публикации депо** (`/api/depot/<project>/...`) — приёмник для
   новых версий сборки, пишет чанки/манифесты напрямую на диск сервера,
   в обход Nextcloud/WebDAV. Формат на диске идентичен тому, что раньше
   публиковалось на WebDAV (`chunks/<xx>/<id>`, `versions/<key>.json`,
   `depot.json`) — меняется только транспорт.

Сама раздача уже опубликованной сборки (то, что скачивает игрок через
TESL) пока остаётся на Nextcloud/WebDAV — эта панель её не подменяет, см.
`CLAUDE.md`.

## Локальный запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
TESL_PANEL_STORAGE_ROOT=./data TESL_PANEL_UPLOAD_TOKEN=devtoken TESL_PANEL_PROJECTS=TESVAE \
  .venv/bin/flask --app panel.app run --port 8080
```

Откроется на `http://127.0.0.1:8080/`.

## Деплой на сервер

См. `infra/deploy.sh` (запускать на самом сервере, root по SSH, скрипт
идемпотентен):

```bash
git clone https://github.com/scp-oss/TESL-Panel.git
cd TESL-Panel
sudo ./infra/deploy.sh --domain panel.example.com \
    --storage-root /mnt/1tb-1 --projects TESVAE
sudo certbot --nginx -d panel.example.com
```

Скрипт сам генерирует upload-токен при первом запуске и печатает его в
конце — этот токен нужен для настройки публикации в TESL-Manager
(`backend: "panel"` в конфиге, см. его собственный CLAUDE.md).

## Переменные окружения

| Переменная                      | Назначение                                   |
|----------------------------------|-----------------------------------------------|
| `TESL_PANEL_STORAGE_ROOT`        | Корень хранилища депо на диске                |
| `TESL_PANEL_UPLOAD_TOKEN`        | Bearer-токен для записи (`PUT`)               |
| `TESL_PANEL_PROJECTS`            | Через запятую — допустимые имена проектов     |
| `TESL_PANEL_GITHUB_CACHE_TTL`    | Кэш ответа GitHub API, секунды (по умолч. 300)|

Чтение депо (`GET`/`HEAD`) токена не требует — та же логика, что у
read-only nginx-плана в TESL-Manager: чтение не секрет, запись — да.
