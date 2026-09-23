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
from functools import wraps

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
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

    @app.route("/api/depot/<project>/<path:rel_path>", methods=["GET", "HEAD", "PUT"])
    def depot_object(project, rel_path):
        _check_project(project)
        try:
            if request.method == "PUT":
                _require_upload_token()
                data = request.get_data(cache=False)
                storage.put_bytes(project, rel_path, data)
                return jsonify({"status": "ok", "bytes": len(data)}), 201

            # GET/HEAD — публичное чтение, токен не нужен.
            if not storage.exists(project, rel_path):
                abort(404)
            if request.method == "HEAD":
                return Response(status=200)
            data = storage.get_bytes(project, rel_path)
            return Response(data, mimetype="application/octet-stream")
        except UnsafePathError:
            abort(400, description="некорректный путь")

    return app


# gunicorn/точка входа для прямого запуска (только для локальной проверки —
# на сервере всегда через gunicorn, см. infra/tesl-panel.service)
app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
