# ==================== panel/app.py ====================
"""
TESL-Panel — тонкий веб-сервис с двумя ролями:

  1. Публичная страница "/" — кнопки скачать TESL.exe / TESL-Manager.exe,
     ссылки берутся из последнего GitHub Release (см. github_releases.py).
     Сама сборка (депо/чанки) пока остаётся на WebDAV — см. TESL-Manager's
     CLAUDE.md, эта панель её не подменяет.

  2. /api/depot/<build_id>/... — приёмник для НОВЫХ публикаций, минуя
     Nextcloud/WebDAV, пишет напрямую на диск сервера (storage.py). Формат
     на диске идентичен тому, что уже на WebDAV (chunks/<xx>/<id>,
     versions/<key>.json, depot.json) — меняется только транспорт.
     Запись требует Bearer-токен (TESL_PANEL_UPLOAD_TOKEN), чтение — нет
     (тот же принцип, что и у read-only nginx-плана в TESL-Manager: чтение
     депо не секрет, запись — да).

  build_id — стабильный UUID из builds_db.py (2026-09-23, прямой запрос
  пользователя: сборки создаются независимо и в панели, и в менеджере,
  имя не должно быть ключом связи между ними). Человеко-читаемое ИМЯ
  сборки остаётся в /admin-URL-ах (/admin/project/<name>/...) для
  удобства просмотра глазами — эти маршруты сами резолвят имя в build_id
  через builds_db.get_build_by_name() внутри обработчика.
"""
import base64
import json
from functools import wraps

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)

from . import builds_db, config, github_releases, reports_storage, self_update, storage, system_stats
from .storage import UnsafePathError
from .reports_storage import UnsafePathError as ReportsUnsafePathError


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY

    # ── Публичная страница ──────────────────────────────────────────────────

    @app.get("/")
    def index():
        releases = {
            key: github_releases.get_latest_release(key)
            for key in config.GITHUB_REPOS
        }
        titles = {key: spec["title"] for key, spec in config.GITHUB_REPOS.items()}
        return render_template("index.html", releases=releases, titles=titles)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    # Публично, как и /health — версия/коммит панели не секрет (тот же
    # принцип, что у GET-чтения депо ниже), клиенту (TESL-Manager) нужно
    # знать это ДО того, как он вообще авторизован токеном записи —
    # прямой запрос пользователя, см. self_update.get_local_commit().
    @app.get("/api/server-info")
    def api_server_info():
        return jsonify({"commit": self_update.get_local_commit()})

    # ── Admin: логин по UPLOAD_TOKEN (тот же токен, что и у /api/depot/*
    #    записи — см. config.py, зачем не два разных секрета), дальше
    #    сессия по подписанной cookie (SECRET_KEY) — управление сборками
    #    ниже требует эту сессию на КАЖДЫЙ запрос, не только на страницу
    #    логина. ─────────────────────────────────────────────────────────────

    def _token_valid(token: str) -> bool:
        return bool(config.UPLOAD_TOKEN) and token == config.UPLOAD_TOKEN

    def _admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("admin"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    @app.get("/admin/login")
    def admin_login():
        # Вход одной ссылкой — /admin/login?token=<UPLOAD_TOKEN> —
        # тот же токен, что и у формы ниже/Bearer у /api/*, просто как
        # query-параметр вместо POST-формы, чтобы ссылку можно было
        # сохранить/переслать и войти одним кликом. Не более "открыто",
        # чем остальной проект уже обращается с этим токеном (тот же
        # токен явным текстом в коде настройки на /admin/settings) —
        # если токен когда-нибудь станет более чувствительным, эту
        # ссылку тоже нужно будет пересмотреть.
        token = request.args.get("token", "")
        if token:
            if not _token_valid(token):
                return render_template("login.html", error="Неверный токен"), 401
            session["admin"] = True
            session.permanent = True
            next_path = request.args.get("next") or url_for("admin_page")
            return redirect(next_path)
        return render_template("login.html", error=None)

    @app.post("/admin/login")
    def admin_login_post():
        token = request.form.get("token", "")
        if not _token_valid(token):
            return render_template("login.html", error="Неверный токен"), 401
        session["admin"] = True
        session.permanent = True
        next_path = request.form.get("next") or url_for("admin_page")
        return redirect(next_path)

    @app.get("/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("admin_login"))

    # ── Admin: список сборок, добавление/удаление/переименование — без
    #    передеплоя/рестарта, см. builds_db.py ──────────────────────────────

    @app.get("/admin")
    @_admin_required
    def admin_page():
        return render_template("admin.html", builds=builds_db.list_builds(), error=None)

    @app.post("/admin/add-project")
    @_admin_required
    def admin_add_project():
        name = request.form.get("project", "").strip()
        build, err = builds_db.create_build(name)
        if build is None:
            return render_template("admin.html", builds=builds_db.list_builds(), error=err), 400
        return redirect(url_for("admin_page"))

    # ── Admin: "код настройки" для TESL-Manager (Настройки → одна строка,
    #    вставляется в клиент вместо URL+токена по отдельности) + self-update
    #    самой панели (см. self_update.py) ───────────────────────────────────

    def _generate_setup_code() -> str:
        # base64(JSON) — не секретность ради самого кодирования (тот же
        # UPLOAD_TOKEN и так виден в открытом виде на этой же странице),
        # а чтобы в одну строку без переносов/пробелов помещалось сразу
        # несколько полей (url + token, при необходимости — другие в
        # будущем) и клиент мог надёжно распарсить её одним action'ом
        # "Подключить по коду" вместо трёх отдельных полей ввода.
        base_url = config.PUBLIC_BASE_URL or request.url_root.rstrip("/")
        payload = {"v": 1, "base_url": base_url, "token": config.UPLOAD_TOKEN}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    @app.get("/admin/settings")
    @_admin_required
    def admin_settings():
        return render_template(
            "settings.html",
            setup_code=_generate_setup_code(),
            has_token=bool(config.UPLOAD_TOKEN),
            update_info=None,
            update_result=None,
        )

    @app.post("/admin/settings/check-updates")
    @_admin_required
    def admin_settings_check_updates():
        return render_template(
            "settings.html",
            setup_code=_generate_setup_code(),
            has_token=bool(config.UPLOAD_TOKEN),
            update_info=self_update.check_for_updates(),
            update_result=None,
        )

    @app.post("/admin/settings/apply-update")
    @_admin_required
    def admin_settings_apply_update():
        ok, msg = self_update.apply_update()
        return render_template(
            "settings.html",
            setup_code=_generate_setup_code(),
            has_token=bool(config.UPLOAD_TOKEN),
            update_info=None,
            update_result={"ok": ok, "message": msg},
        )

    # ── Admin: страница одной сборки (по ИМЕНИ в URL — человеко-читаемо,
    #    резолвится в build_id внутри обработчика) ──────────────────────────

    @app.get("/admin/project/<name>")
    @_admin_required
    def admin_project_detail(name):
        build = builds_db.get_build_by_name(name)
        if build is None:
            abort(404)
        return render_template(
            "project_detail.html",
            name=name,
            build_id=build["id"],
            depot_meta=storage.get_depot_meta(build["id"]),
            versions=storage.list_versions(build["id"]),
            rename_error=None,
        )

    @app.post("/admin/project/<name>/rename")
    @_admin_required
    def admin_project_rename(name):
        build = builds_db.get_build_by_name(name)
        if build is None:
            abort(404)
        new_name = request.form.get("new_name", "").strip()
        ok, msg = builds_db.rename_build(build["id"], new_name)
        if not ok:
            return render_template(
                "project_detail.html",
                name=name,
                build_id=build["id"],
                depot_meta=storage.get_depot_meta(build["id"]),
                versions=storage.list_versions(build["id"]),
                rename_error=msg,
            ), 400
        # msg — старое имя при успехе (см. builds_db.rename_build())
        storage.rename_dir(msg, new_name)
        return redirect(url_for("admin_project_detail", name=new_name))

    @app.post("/admin/project/<name>/delete")
    @_admin_required
    def admin_project_delete(name):
        # Печатать имя сборки заново в форме — самая простая защита от
        # "случайно ткнул кнопку" для необратимого действия (удаляет ВСЕ
        # чанки/версии сборки с диска) — тот же принцип, что и у
        # confirmBulkDelete() в z0r-panel (JS там, тут — server-side).
        if request.form.get("confirm", "") != name:
            abort(400, description="имя для подтверждения не совпадает")
        build = builds_db.get_build_by_name(name)
        if build is not None:
            deleted = builds_db.delete_build(build["id"])
            if deleted is not None:
                storage.delete_dir_by_name(deleted["name"])
        return redirect(url_for("admin_page"))

    # ── Admin: файловый менеджер сборки (список/просмотр/правка/добавление/
    #    удаление отдельных файлов внутри STORAGE_ROOT/<name>/) — работает
    #    поверх той же storage.py, что и /api/depot, просто с UI и без
    #    Bearer-токена (сессия /admin уже подтверждает то же самое доверие).
    #    "Правка" — только для небольших (см. storage.MAX_INLINE_EDIT_BYTES)
    #    и декодируемых как UTF-8 файлов; для остального — скачать/заменить/
    #    удалить целиком, инлайн-редактор бинарных чанков не имеет смысла
    #    (они адресуются по хэшу своего же содержимого — руками поправленный
    #    чанк развалит все ссылающиеся на него файлы). ──────────────────────

    def _build_id_by_name_or_404(name: str) -> str:
        build = builds_db.get_build_by_name(name)
        if build is None:
            abort(404)
        return build["id"]

    @app.get("/admin/project/<name>/files")
    @_admin_required
    def admin_files(name):
        build_id = _build_id_by_name_or_404(name)
        return render_template(
            "files.html", name=name, files=storage.list_files(build_id), error=None,
        )

    @app.get("/admin/project/<name>/files/edit")
    @_admin_required
    def admin_file_edit(name):
        build_id = _build_id_by_name_or_404(name)
        rel_path = request.args.get("path", "").strip()
        content = ""
        too_big = False
        binary = False
        if rel_path:
            data = storage.get_bytes(build_id, rel_path)
            if data is not None:
                if len(data) > storage.MAX_INLINE_EDIT_BYTES:
                    too_big = True
                else:
                    try:
                        content = data.decode("utf-8")
                    except UnicodeDecodeError:
                        binary = True
        return render_template(
            "file_edit.html", name=name, rel_path=rel_path, content=content,
            too_big=too_big, binary=binary,
        )

    @app.post("/admin/project/<name>/files/edit")
    @_admin_required
    def admin_file_edit_post(name):
        build_id = _build_id_by_name_or_404(name)
        rel_path = request.form.get("path", "").strip()
        content  = request.form.get("content", "")
        if not rel_path:
            abort(400, description="путь не указан")
        try:
            storage.put_bytes(build_id, rel_path, content.encode("utf-8"))
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    @app.post("/admin/project/<name>/files/upload")
    @_admin_required
    def admin_file_upload(name):
        build_id = _build_id_by_name_or_404(name)
        f = request.files.get("file")
        rel_path = request.form.get("path", "").strip() or (f.filename if f else "")
        if not f or not rel_path:
            abort(400, description="нужны и файл, и путь назначения")
        try:
            storage.put_bytes(build_id, rel_path, f.read())
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    @app.post("/admin/project/<name>/files/delete")
    @_admin_required
    def admin_file_delete(name):
        build_id = _build_id_by_name_or_404(name)
        rel_path = request.form.get("path", "").strip()
        try:
            storage.delete_file(build_id, rel_path)
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    # ── Builds API (JSON, для десктоп-GUI TESL-Manager, см. его depot_tab.py:
    #    серверный список/создание/удаление/переименование сборок вместо
    #    ручного управления, тем же Bearer-токеном, что уже используется для
    #    публикации чанков). id — реальный ключ, не имя. ─────────────────────

    def _require_upload_token():
        if not config.UPLOAD_TOKEN:
            # Токен не сконфигурирован на сервере — запись выключена
            # целиком, а не тихо разрешена без проверки.
            abort(503, description="upload token не сконфигурирован на сервере")
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _token_valid(auth[len("Bearer "):]):
            abort(401, description="неверный или отсутствующий Bearer-токен")

    @app.get("/api/builds")
    def api_list_builds():
        # Публичное чтение — список сборок не секрет (тот же принцип,
        # что у GET/HEAD depot_object ниже).
        return jsonify({"builds": builds_db.list_builds()})

    @app.post("/api/builds")
    def api_create_build():
        _require_upload_token()
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or request.form.get("name") or "").strip()
        build, err = builds_db.create_build(name)
        if build is None:
            abort(400, description=err)
        return jsonify(build), 201

    @app.delete("/api/builds/<build_id>")
    def api_delete_build(build_id):
        _require_upload_token()
        deleted = builds_db.delete_build(build_id)
        if deleted is not None:
            storage.delete_dir_by_name(deleted["name"])
        return jsonify({"status": "ok", "existed": deleted is not None})

    @app.patch("/api/builds/<build_id>")
    def api_rename_build(build_id):
        _require_upload_token()
        data = request.get_json(silent=True) or {}
        new_name = (data.get("name") or request.form.get("name") or "").strip()
        ok, msg = builds_db.rename_build(build_id, new_name)
        if not ok:
            abort(400, description=msg)
        storage.rename_dir(msg, new_name)  # msg — старое имя при успехе
        return jsonify({"status": "ok", "id": build_id, "name": new_name})

    # ── Файлы сборки — JSON-листинг для десктоп-GUI (Bearer, тот же
    #    уровень доверия, что у /admin/project/<name>/files, только без
    #    cookie-сессии — сам просмотр/правка/загрузка байт файла уже
    #    покрыты обычным GET/PUT/DELETE на depot_object ниже). ────────────────

    @app.get("/api/depot/<build_id>/files")
    def api_depot_files(build_id):
        _check_build(build_id)
        _require_upload_token()
        try:
            return jsonify({"files": storage.list_files(build_id)})
        except UnsafePathError:
            abort(400)

    # ── Depot API ────────────────────────────────────────────────────────────

    def _check_build(build_id: str):
        if not builds_db.is_allowed(build_id):
            abort(404, description=f"неизвестная сборка: {build_id}")

    @app.get("/api/depot/<build_id>/test")
    def depot_test(build_id):
        _check_build(build_id)
        return jsonify({"status": "ok", "build_id": build_id})

    @app.get("/api/depot/<build_id>/chunks")
    def depot_list_chunks(build_id):
        _check_build(build_id)
        try:
            return jsonify({"chunk_ids": storage.list_chunk_ids(build_id)})
        except UnsafePathError:
            abort(400)

    @app.route("/api/depot/<build_id>/<path:rel_path>", methods=["GET", "HEAD", "PUT", "DELETE"])
    def depot_object(build_id, rel_path):
        _check_build(build_id)
        try:
            if request.method == "DELETE":
                # Тот же Bearer-токен, что и PUT — используется десктоп-GUI
                # (см. TESL-Manager/depot_sync_manager/depot_files_tab.py)
                # для удаления уже опубликованного файла из отдельной
                # вкладки "Файлы на сервере", тем же смыслом, что и
                # /admin/project/<name>/files/delete, только без cookie-сессии.
                _require_upload_token()
                existed = storage.delete_file(build_id, rel_path)
                return jsonify({"status": "ok", "existed": existed})

            if request.method == "PUT":
                _require_upload_token()
                data = request.get_data(cache=False)
                storage.put_bytes(build_id, rel_path, data)
                return jsonify({"status": "ok", "bytes": len(data)}), 201

            # GET/HEAD — публичное чтение, токен не нужен. send_file(...,
            # conditional=True) — а не Response(data, ...) как раньше —
            # даёт настоящую поддержку Range-запросов (206 Partial
            # Content), нужную для pack-файлов (см. pack_writer.py в
            # TESL-Manager, CLAUDE.md "Упаковка чанков в pack-файлы"):
            # pack может весить сотни МБ, а читателю (launcher/панели)
            # нужны байты ОДНОГО чанка внутри него — без Range пришлось
            # бы каждый раз скачивать весь pack целиком. Werkzeug сам
            # читает файл через seek()/частичное чтение, не грузит его в
            # память целиком ни на PUT-время (уже было потоково), ни
            # здесь на чтение.
            path = storage.safe_path(build_id, rel_path)
            if not path.is_file():
                abort(404)
            return send_file(
                path, mimetype="application/octet-stream",
                conditional=True, etag=False, last_modified=None,
            )
        except UnsafePathError:
            abort(400, description="некорректный путь")

    # ── Отчёты с клиентов (лаунчеров) ────────────────────────────────────────
    # Прямой запрос пользователя: раздел "Отчёты", два вида — крэш-репорты
    # и логи отладки (см. reports_storage.py докстринг — уже существующие
    # на стороне TESL-лаунчера кейсы, сейчас идут на WebDAV напрямую;
    # лаунчер НЕ трогается этим заходом, эта панель — только готовая
    # принять их инфраструктура на будущее, плюс сама админ-страница
    # просмотра уже накопленного).

    @app.get("/admin/reports")
    @_admin_required
    def admin_reports():
        summary = [
            {
                "type": t, "title": title,
                "source": reports_storage.REPORT_SOURCES.get(t, ""),
                "count": reports_storage.count_entries(t),
            }
            for t, title in reports_storage.REPORT_TYPES.items()
        ]
        return render_template("reports.html", summary=summary)

    @app.get("/admin/reports/<report_type>")
    @_admin_required
    def admin_reports_type(report_type):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        usernames = reports_storage.list_usernames(report_type)
        return render_template(
            "reports_type.html",
            report_type=report_type,
            title=reports_storage.REPORT_TYPES[report_type],
            usernames=[
                {"name": u, "count": len(reports_storage.list_timestamps(report_type, u))}
                for u in usernames
            ],
        )

    @app.get("/admin/reports/<report_type>/<username>")
    @_admin_required
    def admin_reports_user(report_type, username):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        return render_template(
            "reports_user.html",
            report_type=report_type,
            title=reports_storage.REPORT_TYPES[report_type],
            username=username,
            timestamps=reports_storage.list_timestamps(report_type, username),
        )

    @app.get("/admin/reports/<report_type>/<username>/<timestamp>")
    @_admin_required
    def admin_reports_entry(report_type, username, timestamp):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        try:
            files = reports_storage.list_files(report_type, username, timestamp)
        except ReportsUnsafePathError:
            abort(400)
        return render_template(
            "reports_entry.html",
            report_type=report_type,
            title=reports_storage.REPORT_TYPES[report_type],
            username=username,
            timestamp=timestamp,
            files=files,
        )

    @app.get("/admin/reports/<report_type>/<username>/<timestamp>/<filename>")
    @_admin_required
    def admin_reports_download(report_type, username, timestamp, filename):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        try:
            path = reports_storage.get_file_path(report_type, username, timestamp, filename)
        except ReportsUnsafePathError:
            abort(400)
        if not path.is_file():
            abort(404)
        # Живой запрос: "оставь возможность скачать их но добавь
        # просмоторщик" — этот маршрут остаётся ровно тем, чем был
        # (принудительное скачивание, application/octet-stream), новый
        # /view ниже — отдельный, читающий маршрут для просмотра прямо в
        # браузере, ничего здесь не меняется.
        return send_file(path, mimetype="application/octet-stream", conditional=True)

    @app.get("/admin/reports/<report_type>/<username>/<timestamp>/<filename>/view")
    @_admin_required
    def admin_reports_view(report_type, username, timestamp, filename):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        try:
            path = reports_storage.get_file_path(report_type, username, timestamp, filename)
        except ReportsUnsafePathError:
            abort(400)
        if not path.is_file():
            abort(404)
        size = path.stat().st_size
        text, error = None, None
        # Живой инцидент этого репозитория (см. CLAUDE.md, "проверено
        # storage.py::MAX_INLINE_EDIT_BYTES") — тот же принцип: крэш-репорт
        # может тащить за собой бинарный сейв (.ess) или разрастись до
        # МБ-ов текста, тянуть это целиком в HTML-страницу без ограничения
        # было бы и медленно, и незачем — показываем текст только до
        # разумного предела, иначе явно объясняем и отправляем к
        # обычному скачиванию (кнопка на этой же странице).
        if size > reports_storage.MAX_VIEW_BYTES:
            error = (
                f"Файл слишком большой для просмотра в браузере "
                f"({size / 1024 / 1024:.1f} МБ > "
                f"{reports_storage.MAX_VIEW_BYTES / 1024 / 1024:.0f} МБ) — скачайте и откройте локально."
            )
        else:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("cp1251")
                except UnicodeDecodeError:
                    error = "Бинарный файл — просмотр как текст не имеет смысла, скачайте его."
        return render_template(
            "report_view.html",
            report_type=report_type, title=reports_storage.REPORT_TYPES[report_type],
            username=username, timestamp=timestamp, filename=filename,
            size=size, text=text, error=error,
        )

    @app.post("/admin/reports/<report_type>/<username>/<timestamp>/delete")
    @_admin_required
    def admin_reports_delete(report_type, username, timestamp):
        if not reports_storage.is_valid_report_type(report_type):
            abort(404)
        try:
            reports_storage.delete_entry(report_type, username, timestamp)
        except ReportsUnsafePathError:
            abort(400)
        return redirect(url_for("admin_reports_user", report_type=report_type, username=username))

    # ── Приём отчётов (Bearer, тот же токен, что и /api/depot/* запись) —
    #    PUT по одному файлу за раз, та же форма, что и depot_object ниже,
    #    для единообразия. НИЧЕМ пока не вызывается (лаунчер не трогаем в
    #    этом заходе) — эндпоинт существует, чтобы включить это одной
    #    правкой на стороне лаунчера позже, без изменений здесь. ────────────

    @app.put("/api/reports/<report_type>/<username>/<timestamp>/<filename>")
    def api_reports_upload(report_type, username, timestamp, filename):
        _require_upload_token()
        if not reports_storage.is_valid_report_type(report_type):
            abort(404, description=f"неизвестный тип отчёта: {report_type}")
        try:
            data = request.get_data(cache=False)
            reports_storage.put_file(report_type, username, timestamp, filename, data)
            return jsonify({"status": "ok", "bytes": len(data)}), 201
        except ReportsUnsafePathError:
            abort(400, description="некорректный путь")

    # ── Дашборд: место на диске, сеть, CPU/RAM ───────────────────────────────
    # Прямой запрос пользователя. system_stats.py читает /proc напрямую
    # (без psutil, см. её докстринг) — cpu_percent() блокирует запрос на
    # ~0.2с (два замера с паузой), приемлемо для редкого admin-опроса, не
    # на горячем пути раздачи депо.

    @app.get("/admin/dashboard")
    @_admin_required
    def admin_dashboard():
        return render_template("dashboard.html", disks=system_stats.list_disks())

    @app.get("/admin/dashboard/stats")
    @_admin_required
    def admin_dashboard_stats():
        mountpoint = request.args.get("disk", "")
        disk = system_stats.disk_usage(mountpoint) if mountpoint else None
        if disk is None:
            disks = system_stats.list_disks()
            disk = disks[0] if disks else None
        return jsonify({
            "cpu_percent": system_stats.cpu_percent(),
            "memory":      system_stats.memory_stats(),
            "disk":        disk,
            "network":     system_stats.network_counters(),
        })

    return app


# gunicorn/точка входа для прямого запуска (только для локальной проверки —
# на сервере всегда через gunicorn, см. infra/tesl-panel.service)
app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
