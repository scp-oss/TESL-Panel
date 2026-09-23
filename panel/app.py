# ==================== panel/app.py ====================
"""
TESL-Panel — тонкий веб-сервис с двумя ролями:

  1. Публичная страница "/" — кнопки скачать TESL.exe / TESL-Manager.exe,
     ссылки берутся из последнего GitHub Release (см. github_releases.py).
     Сама сборка (депо/чанки) пока остаётся на WebDAV — см. TESL-Manager's
     CLAUDE.md, эта панель её не подменяет.

  2. /api/depot/<project>/... — приёмник для НОВЫХ публикаций, минуя
     Nextcloud/WebDAV, пишет напрямую на диск сервера (storage.py). Формат
     на диске идентичен тому, что уже на WebDAV (chunks/<xx>/<id>,
     versions/<key>.json, depot.json) — меняется только транспорт.
     Запись требует Bearer-токен (TESL_PANEL_UPLOAD_TOKEN), чтение — нет
     (тот же принцип, что и у read-only nginx-плана в TESL-Manager: чтение
     депо не секрет, запись — да).
"""
import base64
import json
from functools import wraps

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)

from . import config, github_releases, projects, storage
from .storage import UnsafePathError


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

    # ── Admin: список сборок, добавление/удаление — без передеплоя/
    #    рестарта, см. projects.py ────────────────────────────────────────────

    @app.get("/admin")
    @_admin_required
    def admin_page():
        return render_template("admin.html", projects=projects.list_projects(), error=None)

    @app.post("/admin/add-project")
    @_admin_required
    def admin_add_project():
        name = request.form.get("project", "").strip()
        if not projects.add_project(name):
            return render_template(
                "admin.html", projects=projects.list_projects(),
                error=f"Недопустимое имя: {name!r} (только буквы/цифры/_/-, до 64 симв.)",
            ), 400
        return redirect(url_for("admin_page"))

    # ── Admin: "код настройки" для TESL-Manager (Настройки → одна строка,
    #    вставляется в клиент вместо URL+токена по отдельности) ────────────────

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
        )

    @app.get("/admin/project/<name>")
    @_admin_required
    def admin_project_detail(name):
        if not projects.is_allowed(name):
            abort(404)
        return render_template(
            "project_detail.html",
            name=name,
            depot_meta=storage.get_depot_meta(name),
            versions=storage.list_versions(name),
        )

    @app.post("/admin/project/<name>/delete")
    @_admin_required
    def admin_project_delete(name):
        # Печатать имя сборки заново в форме — самая простая защита от
        # "случайно ткнул кнопку" для необратимого действия (удаляет ВСЕ
        # чанки/версии проекта с диска) — тот же принцип, что и у
        # confirmBulkDelete() в z0r-panel (JS там, тут — server-side).
        if request.form.get("confirm", "") != name:
            abort(400, description="имя для подтверждения не совпадает")
        if projects.is_allowed(name):
            storage.delete_project_dir(name)
            projects.remove_project(name)
        return redirect(url_for("admin_page"))

    # ── Admin: файловый менеджер проекта (список/просмотр/правка/добавление/
    #    удаление отдельных файлов внутри STORAGE_ROOT/<project>/) — работает
    #    поверх той же storage.py, что и /api/depot, просто с UI и без
    #    Bearer-токена (сессия /admin уже подтверждает то же самое доверие).
    #    "Правка" — только для небольших (см. storage.MAX_INLINE_EDIT_BYTES)
    #    и декодируемых как UTF-8 файлов; для остального — скачать/заменить/
    #    удалить целиком, инлайн-редактор бинарных чанков не имеет смысла
    #    (они адресуются по хэшу своего же содержимого — руками поправленный
    #    чанк развалит все ссылающиеся на него файлы). ──────────────────────

    @app.get("/admin/project/<name>/files")
    @_admin_required
    def admin_files(name):
        if not projects.is_allowed(name):
            abort(404)
        return render_template(
            "files.html", name=name, files=storage.list_files(name), error=None,
        )

    @app.get("/admin/project/<name>/files/edit")
    @_admin_required
    def admin_file_edit(name):
        if not projects.is_allowed(name):
            abort(404)
        rel_path = request.args.get("path", "").strip()
        content = ""
        too_big = False
        binary = False
        if rel_path:
            data = storage.get_bytes(name, rel_path)
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
        if not projects.is_allowed(name):
            abort(404)
        rel_path = request.form.get("path", "").strip()
        content  = request.form.get("content", "")
        if not rel_path:
            abort(400, description="путь не указан")
        try:
            storage.put_bytes(name, rel_path, content.encode("utf-8"))
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    @app.post("/admin/project/<name>/files/upload")
    @_admin_required
    def admin_file_upload(name):
        if not projects.is_allowed(name):
            abort(404)
        f = request.files.get("file")
        rel_path = request.form.get("path", "").strip() or (f.filename if f else "")
        if not f or not rel_path:
            abort(400, description="нужны и файл, и путь назначения")
        try:
            storage.put_bytes(name, rel_path, f.read())
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    @app.post("/admin/project/<name>/files/delete")
    @_admin_required
    def admin_file_delete(name):
        if not projects.is_allowed(name):
            abort(404)
        rel_path = request.form.get("path", "").strip()
        try:
            storage.delete_file(name, rel_path)
        except UnsafePathError:
            abort(400, description="некорректный путь")
        return redirect(url_for("admin_files", name=name))

    # ── Projects API (JSON, Bearer — для десктоп-GUI TESL-Manager, см. его
    #    depot_tab.py: серверный список проектов вместо ручного ввода
    #    имени + возможность создать новый прямо из GUI, тем же токеном,
    #    что уже используется для публикации чанков) ──────────────────────────

    @app.get("/api/projects")
    def api_list_projects():
        # Публичное чтение — список имён проектов не секрет (тот же
        # принцип, что у GET/HEAD depot_object ниже).
        return jsonify({"projects": projects.list_projects()})

    @app.post("/api/projects")
    def api_add_project():
        _require_upload_token()
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or request.form.get("name") or "").strip()
        if not projects.add_project(name):
            abort(400, description=f"недопустимое имя проекта: {name!r} (только буквы/цифры/_/-, до 64 симв.)")
        return jsonify({"status": "ok", "project": name}), 201

    # ── Файлы проекта — JSON-листинг для десктоп-GUI (Bearer, тот же
    #    уровень доверия, что у /admin/project/<name>/files, только без
    #    cookie-сессии — сам просмотр/правка/загрузка байт файла уже
    #    покрыты обычным GET/PUT/DELETE на depot_object ниже). ────────────────

    @app.get("/api/depot/<project>/files")
    def api_depot_files(project):
        _check_project(project)
        _require_upload_token()
        try:
            return jsonify({"files": storage.list_files(project)})
        except UnsafePathError:
            abort(400)

    # ── Depot API ────────────────────────────────────────────────────────────

    def _check_project(project: str):
        if not projects.is_allowed(project):
            abort(404, description=f"неизвестный project: {project}")

    def _require_upload_token():
        if not config.UPLOAD_TOKEN:
            # Токен не сконфигурирован на сервере — запись выключена
            # целиком, а не тихо разрешена без проверки.
            abort(503, description="upload token не сконфигурирован на сервере")
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _token_valid(auth[len("Bearer "):]):
            abort(401, description="неверный или отсутствующий Bearer-токен")

    @app.get("/api/depot/<project>/test")
    def depot_test(project):
        _check_project(project)
        return jsonify({"status": "ok", "project": project})

    @app.get("/api/depot/<project>/chunks")
    def depot_list_chunks(project):
        _check_project(project)
        try:
            return jsonify({"chunk_ids": storage.list_chunk_ids(project)})
        except UnsafePathError:
            abort(400)

    @app.route("/api/depot/<project>/<path:rel_path>", methods=["GET", "HEAD", "PUT", "DELETE"])
    def depot_object(project, rel_path):
        _check_project(project)
        try:
            if request.method == "DELETE":
                # Тот же Bearer-токен, что и PUT — используется десктоп-GUI
                # (см. TESL-Manager/depot_sync_manager/server_files_panel_tab.py)
                # для удаления уже опубликованного файла из отдельной
                # вкладки "Файлы на сервере", тем же смыслом, что и
                # /admin/project/<name>/files/delete, только без cookie-сессии.
                _require_upload_token()
                existed = storage.delete_file(project, rel_path)
                return jsonify({"status": "ok", "existed": existed})

            if request.method == "PUT":
                _require_upload_token()
                data = request.get_data(cache=False)
                storage.put_bytes(project, rel_path, data)
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
            path = storage.safe_path(project, rel_path)
            if not path.is_file():
                abort(404)
            return send_file(
                path, mimetype="application/octet-stream",
                conditional=True, etag=False, last_modified=None,
            )
        except UnsafePathError:
            abort(400, description="некорректный путь")

    return app


# gunicorn/точка входа для прямого запуска (только для локальной проверки —
# на сервере всегда через gunicorn, см. infra/tesl-panel.service)
app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
