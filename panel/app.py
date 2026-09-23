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
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from . import config, github_releases, projects, storage
from .storage import UnsafePathError


def create_app() -> Flask:
    app = Flask(__name__)

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

    # ── Admin: список сборок + добавление новой (без передеплоя, см.
    #    projects.py) ──────────────────────────────────────────────────────────

    def _token_valid(token: str) -> bool:
        return bool(config.UPLOAD_TOKEN) and token == config.UPLOAD_TOKEN

    @app.get("/admin")
    def admin_page():
        return render_template("admin.html", projects=projects.list_projects(), error=None)

    @app.post("/admin/add-project")
    def admin_add_project():
        token = request.form.get("token", "")
        name  = request.form.get("project", "").strip()
        if not _token_valid(token):
            return render_template(
                "admin.html", projects=projects.list_projects(),
                error="Неверный токен",
            ), 401
        if not projects.add_project(name):
            return render_template(
                "admin.html", projects=projects.list_projects(),
                error=f"Недопустимое имя: {name!r} (только буквы/цифры/_/-, до 64 симв.)",
            ), 400
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
