# ==================== panel/token_rotate.py ====================
"""
Перегенерация TESL_PANEL_UPLOAD_TOKEN из /admin/settings — прямой запрос
пользователя ("добавь кнопку в панели перегенерировать токен"). До этого
единственный способ сменить токен был SSH на сервер + `infra/deploy.sh
--upload-token <новый>` вручную.

Тот же токен — и Bearer для /api/*, и единственный "пароль" входа в
/admin (см. config.py::UPLOAD_TOKEN докстринг за обоснование, почему это
один секрет, а не два) — перегенерация касается обоих сразу, не только
API.

Запись panel.env НЕ требует sudo — файл принадлежит тому же сервисному
пользователю (tesl-panel), от имени которого и выполняется сам процесс
(см. infra/deploy.sh: `chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"`),
обычная запись Python-процесса в свой собственный файл. Только РЕСТАРТ
сервиса (чтобы новое значение реально подхватилось — env-переменные
читаются один раз при старте процесса, не перечитываются на лету)
требует sudo — тот же самый грант, что уже ставит deploy.sh для
self_update.py::apply_update() (`systemctl restart tesl-panel.service`),
новых sudoers-строк для этого не нужно.

Session-cookie (подписан TESL_PANEL_SECRET_KEY, отдельным от UPLOAD_TOKEN
секретом, который эта операция НЕ трогает) остаётся валидным через
рестарт — оператор не разлогинивается сам у себя, хотя сам токен, каким
он был до перегенерации, для НОВЫХ входов/API-вызовов сразу же
перестаёт работать.
"""
import re
import secrets
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_DIR / "panel.env"
SERVICE_NAME = "tesl-panel.service"

_TOKEN_LINE_RE = re.compile(r"^TESL_PANEL_UPLOAD_TOKEN=.*$", re.MULTILINE)


def rotate_upload_token() -> "tuple[bool, str, str]":
    """(ok, message, new_token). new_token — пустая строка при неудаче.
    Новый токен уже реально записан в panel.env к моменту возврата True —
    сервис ещё не перезапущен (это fire-and-forget, ответ должен успеть
    уйти раньше, тот же принцип, что и apply_update()), поэтому
    ТЕКУЩИЙ процесс (и значит "код настройки" на этой же странице, если
    не перезагрузить её) ещё покажет старый токен до рестарта."""
    if not ENV_FILE.is_file():
        return False, f"panel.env не найден по пути {ENV_FILE}", ""
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Не удалось прочитать panel.env: {e}", ""

    if not _TOKEN_LINE_RE.search(text):
        return False, "В panel.env нет строки TESL_PANEL_UPLOAD_TOKEN= — обновите деплой", ""

    new_token = secrets.token_hex(24)
    new_text = _TOKEN_LINE_RE.sub(f"TESL_PANEL_UPLOAD_TOKEN={new_token}", text, count=1)

    try:
        ENV_FILE.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return False, f"Не удалось записать panel.env: {e}", ""

    # Токен уже реально на диске к этому моменту — дальше только попытка
    # перезапуска, её неудача не отменяет сам факт смены токена (ok=True
    # в обоих случаях ниже, отличается только текст подсказки).
    try:
        # Fire-and-forget, как и apply_update() — этот процесс скоро
        # умрёт от собственного рестарта, ответ должен успеть уйти раньше.
        subprocess.Popen(["sudo", "-n", "systemctl", "restart", SERVICE_NAME])
    except FileNotFoundError:
        return True, (
            "Токен записан в panel.env, но не удалось перезапустить сервис "
            "(sudo/systemctl не найдены) — перезапустите tesl-panel.service вручную, "
            "иначе новый токен не вступит в силу."
        ), new_token

    return True, (
        "Новый токен записан, сервис перезапускается — обновите страницу "
        "через несколько секунд, чтобы увидеть свежий код настройки. "
        "Старый токен уже недействителен для новых входов и API-вызовов."
    ), new_token
