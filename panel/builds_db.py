# ==================== panel/builds_db.py ====================
"""
Реестр "сборок" (то, что раньше называлось "проект", `projects.py`) —
прямой запрос пользователя (2026-09-23): имя сборки не должно быть
ключом связи между панелью и менеджером, потому что сборки создаются
независимо в обоих местах — нужен настоящий стабильный id, а не строка,
которую легко случайно рассинхронизировать (опечатка, регистр,
переименование). Полноценно: SQLite вместо JSON-списка (`projects.py`,
теперь удалён), `id` (UUID4 hex) — реальный ключ везде, где сборка
адресуется программно (`/api/*`), имя — только для чтения человеком (в
`/admin` URL-ах, в списках) и как имя папки на диске (см. ниже, почему
это НЕ то же самое, что использовать имя как identity).

**Остаётся SQLite, не JSON-файл** — прямое решение пользователя
2026-09-23 после живого падения прод-сервера (см. ниже): реальные
таблицы нужны не только для этого простого реестра имён, а как
фундамент под то, что уже запланировано — несколько РЕЛИЗОВ/версий на
сборку и откат на клиенте на конкретную прошлую версию (история строк,
внешние ключи на будущую таблицу релизов, выборки "последний успешный
релиз для сборки X" и т.п.) — то, что JSON-файл с перезаписью целиком
не выражает естественно, а SQLite выражает без второй самодельной
реализации того же самого поверх файла.

**Живой инцидент 2026-09-23 и почему это НЕ повод уходить от SQLite**:
на реальном сервере Python (`/usr/local/lib/python3.11`, собран из
исходников не Debian-пакетом) оказался собран БЕЗ модуля `_sqlite3` —
`import sqlite3` падал `ModuleNotFoundError`. Это НЕ значит, что sqlite3
как формат хранения не годится — это значит, что конкретно ЭТОТ
интерпретатор не может открыть stdlib-модуль. Первая попытка исправить
это (переписать модуль на JSON-файл) была откачена по прямому указанию
пользователя: "нет sqlite3 нужна мы же реализуем разные релизы разные
версии релизов и можем откатываться на клиенте". Правильное исправление
— не отказ от SQL, а установка `pysqlite3-binary` (PyPI-пакет, не
stdlib) как запасного варианта: это самодостаточное wheel-сборка sqlite3
со статически слинкованной библиотекой sqlite3 внутри самого пакета —
не требует, чтобы СИСТЕМНЫЙ Python был скомпилирован с `--enable-loadable-sqlite-extensions`/
sqlite3-dev в системе, работает как обычный `pip install` в venv, что
уже делает `deploy.sh`. Импорт ниже сначала пробует stdlib `sqlite3` (на
серверах, где он есть, — никакой лишней зависимости), и только если это
падает — берёт `pysqlite3` под тем же именем `sqlite3`, так что весь
остальной код этого файла ниже не знает и не должен знать, какой из
двух путей сработал (`sqlite3.Connection`/`sqlite3.connect` — тот же
API у обоих).

Важно отличать от `chunk_index.db` (`pack_writer.py`, TESL-Manager) —
ЭТОТ файл ("локальная бд для сборки" по формулировке пользователя) НЕ
трогается и не заменяется, он остаётся своим отдельным SQLite ВНУТРИ
папки каждой сборки (`<build_dir>/chunk_index.db`) и решает совсем
другую задачу (где физически лежит чанк внутри pack-файлов). Этот
модуль — реестр САМИХ сборок (что существует, какое у чего имя), один
файл на всю панель (`<STORAGE_ROOT>/_meta/builds.db`), не имеет
никакого отношения к содержимому конкретной сборки.

Имя сборки СОХРАНЯЕТСЯ как имя папки на диске (storage.py менять раскладку
уже опубликованных сборок было бы рискованно без доступа к реальному
серверу) — rename_build() физически переименовывает папку тем же
заходом, что меняет строку в БД, так что имя-как-путь и имя-в-реестре
никогда не расходятся.

**Поправка 2026-09-29, когда версии/откат реально понадобились**: план
выше ("фундамент под несколько релизов/версий... SQLite выражает без
второй самодельной реализации") на практике не потребовался ВООБЩЕ —
оказалось, что TESL-Manager уже безусловно пишет полный снапшот
манифеста в `versions/<build_id>.json` на КАЖДОЙ публикации, давно, с
самого начала протокола (см. `depot_sync_manager.py`) — история версий
физически уже лежит на диске, просто никто её не читал назад. Реальная
реализация — `storage.py::list_version_meta()`/`prune_versions()`,
читает и чистит эти файлы напрямую, без единой новой таблицы здесь.
Оставлено как урок: до того, как строить новую БД под "то, что
понадобится позже" — стоит сначала проверить, не пишет ли уже что-то
нужное существующий клиентский код, просто без читателя на другом
конце.
"""
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    import sqlite3
except ImportError:  # см. докстринг модуля — интерпретатор без _sqlite3
    import pysqlite3 as sqlite3  # noqa: F401  (pysqlite3-binary, requirements.txt)

from . import config, storage_cluster

_lock = threading.Lock()

# ── Процесс-локальный кэш ────────────────────────────────────────────────────
# Живой инцидент 2026-09-30: `get_build()` вызывается на САМОМ горячем
# пути этого приложения — `_check_build()` в `app.py` дёргает его на
# КАЖДЫЙ GET/HEAD/PUT/DELETE чанка/пака (потенциально сотни тысяч раз за
# одну установку), и до этой правки каждый такой вызов открывал НОВОЕ
# SQLite-соединение (`_connect()` — плюс `CREATE TABLE IF NOT EXISTS` и
# пробный `ALTER TABLE ADD COLUMN`, каждый раз заново), гонял
# `_migrate_legacy_if_needed()`'s `SELECT COUNT(*)`, саму реальную
# SELECT-выборку — и ВСЁ это под одним общим `threading.Lock()`,
# сериализующим ВСЕ потоки одного gunicorn-воркера. Под 24+ параллельными
# запросами лаунчера (`CHUNK_MAX_WORKERS`) это создавало катастрофическую
# очередь: реальная передача байт файла (быстрая) тонула в ожидании
# лока на тривиальную проверку "существует ли эта сборка". Подтверждено
# живьём: один поток `curl` без этой перегрузки скачал файл за 16с на
# 1.75 МБ/с, а те же 24 потока через лаунчер еле ползли на единицы КБ/с.
#
# Кэш — простой словарь build_id -> запись, заполняется целиком при
# первом обращении в этом процессе, дальше `get_build()` на КЭШ-ХИТЕ
# (подавляющее большинство вызовов — один и тот же build_id тысячи раз
# подряд за установку) не трогает SQLite/lock вообще. Промах кэша (id
# ещё не видели В ЭТОМ процессе, либо сборка реально не существует) —
# один настоящий поход в БД, с записью результата в кэш.
#
# Мутирующие функции (create_build/delete_build/rename_build/
# set_storage_root) обновляют кэш ТОЧЕЧНО сами, в момент записи —
# единственный писатель в рамках одного процесса, кэш никогда не
# расходится с реальностью для мутаций ЭТОГО ЖЕ процесса.
#
# **Явная граница, не скрытая**: при `--workers 2` (см. `infra/
# tesl-panel.service.template`) это ДВА отдельных ОС-процесса, кэш
# каждого не расшарен с другим — если сборку создали/переименовали/
# удалили через ОДИН воркер, а запрос на чтение попал на ДРУГОЙ, который
# уже успел закэшировать старое состояние (create/rename — промах на
# новый id всё равно уйдёт в реальную БД и найдёт актуальные данные;
# delete/rename СТАРОГО имени — окно до перезапуска процесса, когда
# другой воркер ещё не знает об изменении). Для одного оператора с редкими
# мутациями (создание/переименование/удаление сборок — не поток
# чанков) это приемлемый компромисс — а не незамеченный риск.
_build_cache: Dict[str, dict] = {}
_migrated = False   # процесс уже прогонял _migrate_legacy_if_needed() хоть раз

# Тот же паттерн, что был у старого projects.py::_PROJECT_NAME_RE —
# буквы/цифры/подчёркивание/дефис, используется как сегмент файлового
# пути (storage.py), поэтому "/", ".." и т.п. в принципе не проходят.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _db_path() -> Path:
    p = Path(config.STORAGE_ROOT).resolve() / "_meta" / "builds.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    # Новое соединение на каждый вызов — эта БД живёт только на пути
    # управления сборками (list/create/delete/rename), не на горячем
    # пути записи/чтения чанков (тот остаётся чистой файловой операцией
    # в storage.py) — частота вызовов низкая, постоянное соединение
    # ради этого не оправдана, а per-call соединение проще и безопаснее
    # при нескольких gunicorn-воркерах (нет расшаренного состояния).
    conn = sqlite3.connect(str(_db_path()), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS builds ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL UNIQUE,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    # storage_root — какой член кластера хранения (см. storage_cluster.py)
    # физически держит эту сборку, прямой запрос пользователя 2026-09-29
    # ("кластер папок... одна папка на одном разделе, другая на другом").
    # ALTER TABLE ADD COLUMN — единственный способ добавить колонку к уже
    # существующей таблице в SQLite; идемпотентно через try/except (сам
    # SQLite не даёт "ADD COLUMN IF NOT EXISTS"). NULL у уже существующих
    # строк — это НЕ ошибка миграции, это осознанный сигнал "сборка
    # опубликована до появления кластера, физически лежит в исторически
    # единственном STORAGE_ROOT" — все читающие функции ниже трактуют
    # NULL именно так (config.STORAGE_ROOT), не как "неизвестно".
    try:
        conn.execute("ALTER TABLE builds ADD COLUMN storage_root TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже есть — обычный случай на каждом вызове после первого
    return conn


def _row_to_build(row) -> dict:
    return {
        "id": row[0], "name": row[1], "created_at": row[2], "updated_at": row[3],
        # см. комментарий у ALTER TABLE выше — NULL в БД -> исторический
        # единственный STORAGE_ROOT, не пустая строка (которая означала бы
        # "путь не задан" и сломала бы Path(...) резолвинг в storage.py).
        "storage_root": row[4] or config.STORAGE_ROOT,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_legacy_if_needed(conn: sqlite3.Connection) -> None:
    """Один раз переносит имена из старого <STORAGE_ROOT>/_meta/projects.json
    (до этого захода — единственный источник правды) в builds.db, выдавая
    каждому свежий id. Идемпотентно — если в builds уже есть хоть одна
    строка, ничего не делает; специально НЕ трогает и не удаляет старый
    projects.json (оставляем как есть на диске — не мешает, но и незачем
    трогать файл, который сам код больше не читает).

    Свежая установка без legacy-файла — builds просто остаётся пустым,
    первую сборку создаёт оператор сам через UI/API (кнопка "Новая
    сборка" в TESL-Manager, или /admin/add-project). Раньше здесь было
    жёстко зашитое имя-заглушка ("TESVAE", через env TESL_PANEL_PROJECTS)
    для сидирования пустой установки — убрано по прямому запросу
    пользователя (2026-09-24, "TESL_PANEL_PROJECTS=TESVAE надо удалить
    из евн") — реестр сборок больше не нуждается в стартовом значении
    по умолчанию, раз полноценное управление сборками уже есть."""
    count = conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
    if count > 0:
        return

    import json
    legacy_path = Path(config.STORAGE_ROOT).resolve() / "_meta" / "projects.json"
    names: List[str] = []
    if legacy_path.is_file():
        try:
            names = list(json.loads(legacy_path.read_text(encoding="utf-8")).get("projects", []))
        except Exception:
            names = []

    now = _now()
    for name in names:
        if not _NAME_RE.match(name):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO builds (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, name, now, now),
        )
    conn.commit()


def _ensure_migrated(conn: sqlite3.Connection) -> None:
    """`_migrate_legacy_if_needed()` — тот же самый идемпотентный
    результат при повторном вызове, но `SELECT COUNT(*)` внутри нет
    смысла гонять на каждое обращение — гейтим процесс-локальным флагом,
    один раз за жизнь процесса достаточно (см. докстринг `_build_cache`
    выше за полную причину этой правки)."""
    global _migrated
    if _migrated:
        return
    _migrate_legacy_if_needed(conn)
    _migrated = True


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def list_builds() -> List[dict]:
    # Живой инцидент 2026-10-05: "хочу почистить сборки через панель, но
    # они не удаляются". Корень — ЭТА функция. `if not _build_cache:`
    # прогревает кэш ОДИН РАЗ за жизнь процесса, потом ВСЕГДА возвращает
    # то, что лежит в словаре, сколько бы раз её ни звали — а под
    # `gunicorn --workers 2` (два отдельных ОС-процесса, см. `infra/
    # tesl-panel.service.template`) это означает: воркер, который сам
    # не участвовал в удалении/создании (запрос на мутацию ушёл ДРУГОМУ
    # процессу), после своего первого же обращения к /admin никогда
    # больше не узнает об изменении — не через 2с, не через 2 минуты,
    # а вообще никогда, пока процесс не перезапустится. С двумя
    # воркерами это ~50% запросов на каждую перезагрузку страницы —
    # не редкая гонка, а стабильно воспроизводимое "как будто не
    # удаляется". `list_builds()`/`get_build_by_name()` (ниже) —
    # НЕ горячий путь (админка, не поток чанков из `get_build()`), так
    # что цена честного похода в SQLite на каждый вызов здесь ничтожна
    # по сравнению с ценой показывать неверную картину. Кэш всё равно
    # обновляется (целиком пересобирается из СВЕЖИХ данных) — выигрыш
    # для `get_build()`'s кэш-хитов на чанках остаётся: тот как читал
    # словарь напрямую, так и продолжает.
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            rows = conn.execute(
                "SELECT id, name, created_at, updated_at, storage_root FROM builds"
            ).fetchall()
            fresh = {r[0]: _row_to_build(r) for r in rows}
        finally:
            conn.close()
    # Полная замена, не merge — запись, удалённая ДРУГИМ процессом,
    # должна пропасть из кэша ЭТОГО процесса тоже, не просто остаться
    # висеть рядом со свежими.
    _build_cache.clear()
    _build_cache.update(fresh)
    return sorted(_build_cache.values(), key=lambda b: b["name"])


def get_build(build_id: str) -> Optional[dict]:
    # САМЫЙ горячий вызов в этом модуле — см. докстринг `_build_cache`
    # выше за живой инцидент, из-за которого эта функция вообще
    # переписана. Кэш-хит — просто чтение словаря, без `_lock`/SQLite.
    cached = _build_cache.get(build_id)
    if cached is not None:
        return cached
    # Промах — либо сборки правда нет, либо она создана/переименована
    # ДРУГИМ gunicorn-воркером уже после того, как этот процесс в
    # последний раз видел её (см. докстринг `_build_cache` про границу
    # между процессами) — настоящий поход в SQLite только здесь, не на
    # каждый вызов.
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at, storage_root FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if row is None:
                return None
            b = _row_to_build(row)
            _build_cache[build_id] = b
            return b
        finally:
            conn.close()


def get_build_by_name(name: str) -> Optional[dict]:
    # НЕ кэш-первый, той же причине, что и у list_builds() выше (живой
    # инцидент 2026-10-05) — это функция, через которую проходит КАЖДОЕ
    # admin-удаление/переименование/открытие детальной страницы по
    # имени; доверять ей локальный кэш означало бы, что
    # `admin_project_delete()` может найти и попытаться удалить УЖЕ
    # удалённую (другим воркером) запись по стale id, либо, хуже,
    # решить, что сборки с таким именем больше нет, хотя она есть —
    # только что созданная другим воркером. Не горячий путь (один клик
    # администратора, не поток чанков) — лишний SQLite SELECT здесь
    # дешевле, чем недостоверный ответ.
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at, storage_root FROM builds WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            b = _row_to_build(row)
            _build_cache[b["id"]] = b
            return b
        finally:
            conn.close()


def is_allowed(build_id: str) -> bool:
    return get_build(build_id) is not None


def create_build(name: str) -> "tuple[Optional[dict], str]":
    """(build, "") при успехе, (None, причина) при отказе."""
    name = name.strip()
    if not is_valid_name(name):
        return None, f"недопустимое имя: {name!r} (только буквы/цифры/_/-, до 64 симв.)"
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            existing = conn.execute("SELECT id FROM builds WHERE name = ?", (name,)).fetchone()
            if existing:
                # Идемпотентно, тот же принцип, что был у projects.add_project() —
                # повторное создание уже существующего имени не ошибка.
                row = conn.execute(
                    "SELECT id, name, created_at, updated_at, storage_root FROM builds WHERE name = ?",
                    (name,),
                ).fetchone()
                b = _row_to_build(row)
                _build_cache[b["id"]] = b
                return b, ""
            build_id = uuid.uuid4().hex
            now = _now()
            # Член кластера хранения выбирается ОДИН РАЗ, здесь, и никогда
            # не меняется потом — см. storage_cluster.py за полную модель
            # (вся сборка целиком живёт на одном разделе).
            storage_root = storage_cluster.pick_member_for_new_build()
            conn.execute(
                "INSERT INTO builds (id, name, created_at, updated_at, storage_root) VALUES (?, ?, ?, ?, ?)",
                (build_id, name, now, now, storage_root),
            )
            conn.commit()
            b = {
                "id": build_id, "name": name, "created_at": now, "updated_at": now,
                "storage_root": storage_root,
            }
            _build_cache[build_id] = b
            return b, ""
        finally:
            conn.close()


def set_storage_root(build_id: str, new_root: str) -> None:
    """Переключает сборку на другой член кластера хранения — вызывается
    ТОЛЬКО panel/migration.py, и ТОЛЬКО после того, как содержимое уже
    физически скопировано на новое место и проверено (см. её докстринг)
    — эта функция сама ничего на диске не трогает, чистая запись в БД.
    Прямой запрос пользователя 2026-09-29 ("автоматически выбирает
    подходящий кластер... всегда, включая уже опубликованные сборки") —
    единственное место, которое меняет storage_root ПОСЛЕ создания
    сборки (create_build() — единственное другое место, что его пишет,
    и только один раз, при INSERT)."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE builds SET storage_root = ?, updated_at = ? WHERE id = ?",
                (new_root, _now(), build_id),
            )
            conn.commit()
        finally:
            conn.close()
    cached = _build_cache.get(build_id)
    if cached is not None:
        cached["storage_root"] = new_root


def delete_build(build_id: str) -> Optional[dict]:
    """Возвращает удалённую запись (чтобы вызывающий знал имя — для
    удаления папки на диске) или None, если такого id не было."""
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            row = conn.execute(
                "SELECT id, name, created_at, updated_at, storage_root FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
            if row is None:
                # Уже удалена — скорее всего ДРУГИМ воркером (см.
                # list_builds()/get_build_by_name() выше за полный
                # разбор 2026-10-05). Эта запись физически не может
                # больше прийти сюда через get_build_by_name() после
                # фикса выше, но get_build(build_id) (горячий путь на
                # чанках) остаётся кэш-первым нарочно — если id всё же
                # пришёл отсюда стale путём (прямой DELETE по id от
                # TESL-Manager на id, который этот процесс запомнил из
                # какого-то более раннего get_build()), выбросить
                # стale запись ЗДЕСЬ, а не оставлять её висеть до
                # перезапуска процесса.
                _build_cache.pop(build_id, None)
                return None
            conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
            conn.commit()
            b = _row_to_build(row)
            _build_cache.pop(build_id, None)
            return b
        finally:
            conn.close()


def rename_build(build_id: str, new_name: str) -> "tuple[bool, str]":
    """(True, старое_имя) при успехе — вызывающий (app.py) переименовывает
    папку на диске тем же old_name/new_name, (False, причина) при отказе.
    Переименование папки НЕ делается здесь — этот модуль ничего не знает
    про storage.py/файловую систему, разделение ответственности то же,
    что и у storage.py самого (никогда не трогает builds_db)."""
    new_name = new_name.strip()
    if not is_valid_name(new_name):
        return False, f"недопустимое имя: {new_name!r} (только буквы/цифры/_/-, до 64 симв.)"
    with _lock:
        conn = _connect()
        try:
            _ensure_migrated(conn)
            row = conn.execute("SELECT name FROM builds WHERE id = ?", (build_id,)).fetchone()
            if row is None:
                return False, "сборка не найдена"
            old_name = row[0]
            if old_name == new_name:
                return True, old_name
            clash = conn.execute("SELECT id FROM builds WHERE name = ?", (new_name,)).fetchone()
            if clash:
                return False, f"имя уже занято другой сборкой: {new_name!r}"
            conn.execute(
                "UPDATE builds SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, _now(), build_id),
            )
            conn.commit()
            cached = _build_cache.get(build_id)
            if cached is not None:
                cached["name"] = new_name
            return True, old_name
        finally:
            conn.close()
