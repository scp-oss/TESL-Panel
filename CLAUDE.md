# CLAUDE.md

Оперативные заметки для Claude-сессий, работающих с этим репозиторием —
плотно, для агента. Не класть сюда реальные серверные пути/домены/токены
(см. TESL-Manager/CLAUDE.md "Секреты и пути — не в git" за тот же
принцип) — реальные значения живут только в `panel.env` на сервере
(генерируется `infra/deploy.sh`, в git не попадает, см. `.gitignore`).

## Зачем этот репозиторий (создан 2026-09-23)

Прямой запрос пользователя: "давай начнём разварачивать наш сервис —
сборка на вебдав остаётся, лаунчер тоже пока в таком виде — наши
действия: поднять панель с возможностью скачать exe лаунчера и
менеджера (пока с GitHub), плюс переписанный менеджер для заливки новой
версии сборки уже мимо Nextcloud и сразу в наш сервис, путь хранилища
`/mnt/1tb-1/<название проекта>`".

Разбито на два independent, но связанных куска:
1. **Эта страница** — публичная точка входа для игроков (скачать
   TESL.exe/TESL-Manager.exe) + приёмник новых публикаций сборки.
2. **TESL-Manager получил новый транспорт** (`panel_client.py::PanelHTTP`,
   см. его собственный CLAUDE.md) — публикует напрямую сюда вместо
   Nextcloud/WebDAV, когда оператор выберет `backend: "panel"` в
   конфиге.

**Уже существующая сборка** (та, что реально стоит у игроков, чанки на
Nextcloud/WebDAV) **этим не тронута и не мигрирует** — TESL-лаунчер
по-прежнему читает её оттуда (`DAV_BASE_URL` в TESL не менялся). Эта
панель и её API — путь для БУДУЩИХ публикаций, начиная с той, что
оператор решит опубликовать через неё в первый раз. Явно НЕ входило в
этот заход (по прямому "лаунчер тоже пока в таком виде"): переключение
самого TESL-лаунчера на чтение отсюда — когда/если это понадобится,
это отдельная, более поздняя задача (симметрично уже
запланированному-но-не-включённому read-only nginx+Cloudflare плану в
TESL-Manager — та же дисциплина "построили, проверили, только потом
переключили reader").

## Архитектура

```
panel/
  app.py             — Flask routes (/, /health, /api/depot/<project>/...)
  config.py          — вся конфигурация через os.environ, ничего не хардкожено
  storage.py         — файловое хранилище на диске, content-addressed
                        (та же раскладка, что раньше писалась на WebDAV)
  github_releases.py — кэшированный lookup последнего GitHub Release
  templates/index.html
wsgi.py              — точка входа для gunicorn (см. infra/tesl-panel.service.template)
infra/
  deploy.sh                        — идемпотентный сетап (venv+systemd+nginx),
                                      запускать на сервере, НЕ отсюда
  tesl-panel.service.template      — systemd unit (плейсхолдеры __X__)
  nginx-tesl-panel.conf.template   — nginx reverse-proxy (плейсхолдеры __X__)
```

**У Claude-сессии нет exec-доступа к продакшену** — тот же принцип, что
во всех остальных репозиториях этого проекта (z2r_autobench,
z0r-panel, TESL, TESL-Manager). `infra/deploy.sh` только готовится
здесь и передаётся оператору для ручного запуска (root по SSH).

## API депо (`/api/depot/<project>/...`)

- `GET/HEAD /api/depot/<project>/<rel_path>` — публичное чтение, токен
  НЕ нужен (тот же принцип, что у read-only nginx-плана в
  TESL-Manager: чтение депо — не секрет).
- `PUT /api/depot/<project>/<rel_path>` — запись, требует
  `Authorization: Bearer <TESL_PANEL_UPLOAD_TOKEN>`. Тело запроса —
  сырые байты файла, пишется атомарно (temp-файл + `os.replace`) —
  читающий никогда не увидит частично записанный файл.
- `GET /api/depot/<project>/chunks` — список всех `chunk_id`, уже
  лежащих в `chunks/` этого проекта (для delta-сравнения на стороне
  клиента, аналог `NextcloudDAV.list_chunk_ids()`).
- `GET /api/depot/<project>/test` — проверка достижимости + (неявно,
  через отдельный `PUT`-запрос до этого) валидности токена.

`project` — allowlist (`config.ALLOWED_PROJECTS`, `TESL_PANEL_PROJECTS`
через запятую), не принимается как произвольная строка — двойная
защита от path traversal: (1) на уровне имени проекта здесь, (2) на
уровне пути внутри проекта в `storage.safe_path()` (`realpath` +
prefix-check, тот же паттерн, что z2r_autobench's
`domain_list_sync.sh --path` применяет к своим путям).

## Почему `mkcol()` — no-op в `PanelHTTP` (см. TESL-Manager/panel_client.py)

`NextcloudDAV` (WebDAV) требует явного `MKCOL` перед `PUT` в
несуществующую директорию — WebDAV-протокол так устроен. Файловое
хранилище на диске (`storage.put_bytes()`) создаёт родительские папки
сама (`path.parent.mkdir(parents=True, exist_ok=True)`) при каждом
`PUT` — отдельный вызов для "создать папку" просто не нужен. `PanelHTTP.
mkcol()` всё равно существует и возвращает `True` — чтобы
`DepotSyncManager.ensure_depot_structure()`/`_ensure_chunk_subdir()` не
знали и не заботились, какой транспорт сейчас активен (см. depot_sync_
manager.py `_get_dav()` — единственное место, которое вообще смотрит на
`backend`).

## Проверено (2026-09-23, до первого пуша)

Полный сквозной тест (синтетические файлы → `ChunkManager.scan_directory`
→ `DepotSyncManager` с `backend="panel"` → реальный Flask-сервер на
`127.0.0.1` → файлы физически на диске) — `chunks/`+`versions/<id>.json`+
`depot.json`+`depot_manifest.json` появились корректно, повторная
публикация того же содержимого корректно увидела 0 новых чанков
(`list_chunk_ids` с сервера отработал как надо). `DepotSyncManager`'s
`execute_sync()`/`ensure_depot_structure()` не менялись НИ НА СТРОЧКУ —
только новый транспорт снизу, ровно по плану "backend swap, не
переписывание логики публикации".

Отдельно проверено вручную (curl): неавторизованный `PUT` → 401,
попытка path traversal (`../../etc/passwd`) → блокируется на уровне
кода (`storage.safe_path`), неизвестный `project` → 404, `HEAD`/`GET`
без токена — работают.

**Не проверено вживую** (нет доступа к реальному серверу из песочницы
Claude): сам `infra/deploy.sh` на реальной Debian-машине, nginx+certbot
интеграция, поведение под реальной нагрузкой/большими (до 4MB, см.
`chunk_manager.py::DEFAULT_CHUNK_SIZE`) чанками по сети (не localhost).

## Стиль

Flask, минимум зависимостей (Flask + requests + gunicorn). Русскоязычные
строки в UI/логах/комментариях — тот же стиль, что в TESL/TESL-Manager.
