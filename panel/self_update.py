# ==================== panel/self_update.py ====================
"""
Проверка и применение обновлений САМОЙ панели из её же /admin/settings —
прямой запрос пользователя. Разбито на два принципиально разных по
требуемым правам шага:

  check_for_updates() — ЧИТАЕТ. Работает без каких-либо новых привилегий:
    `git rev-parse HEAD` (локальное чтение) + `git ls-remote origin HEAD`
    (сетевой запрос, ничего не пишет в .git — не то же самое, что
    `git fetch`, у которого есть побочный эффект записи new refs/objects).
    Это специально выбрано так, потому что deploy.sh (см. инфру) оставляет
    сам чекаут репозитория в собственности того, кто его склонировал
    (обычно root), сервисному пользователю `tesl-panel` даёт только
    o+rX (чтение+исполнение) — `git fetch`/`git pull` от его имени упали
    бы с "Permission denied" при попытке создать lock-файл в `.git/`.

  apply_update() — ПИШЕТ (git pull + перезапуск systemd-юнита). Реально
    применить обновление процесс `tesl-panel` своими правами не может —
    для этого нужен sudo-грант на две конкретные команды (см. deploy.sh,
    "sudoers для self-update"), который deploy.sh ставит идемпотентно.
    NoNewPrivileges=true в systemd-юните (было раньше, для запрета
    именно такой эскалации) ради этого снято — прямой trade-off,
    описанный в CLAUDE.md, не тихая дыра: сервис и так уже мог писать
    ЛЮБОЙ файл под STORAGE_ROOT (это его основная работа), а sudo даёт
    ровно две ДОПОЛНИТЕЛЬНЫЕ, явно перечисленные в sudoers команды —
    не root целиком.
"""
import subprocess
from pathlib import Path
from typing import Optional

# panel/self_update.py -> panel/ -> repo root (тот же принцип, что и
# get_launcher_commit()/get_manager_commit() в TESL/TESL-Manager — эта
# репа склонирована как есть, специального env для своего же пути не нужно).
REPO_DIR = Path(__file__).resolve().parent.parent

TIMEOUT_CHECK = 15
TIMEOUT_PULL  = 30
SERVICE_NAME  = "tesl-panel.service"


def _run(args, timeout: int) -> subprocess.CompletedProcess:
    # Живой инцидент (2026-09-24): чекаут репозитория (deploy.sh) остаётся
    # во владении того, кто его склонировал (обычно root), а эти git-вызовы
    # выполняются от имени НЕпривилегированного сервисного пользователя
    # (tesl-panel) — несовпадение владельца упирается в защиту git от
    # dubious ownership (пост-CVE-2022-24765): "fatal: detected dubious
    # ownership in repository". Та же болезнь, что уже была найдена и
    # исправлена в z2r_autobench's z0r (`_check_git_updates()`/
    # `_git_short_commit()`, см. её CLAUDE.md) для checkout'ов, у которых
    # владелец расходится с euid читающего процесса — тот же фикс: `-c
    # safe.directory=<repo>`, ограниченный ЭТИМ конкретным вызовом, а не
    # `--global` (никакого расширения доверия к другим репозиториям на
    # сервере). git сам принимает `-c` до подкоманды, так что args[0]
    # ("git") остаётся первым, опция вставляется сразу после него.
    args = [args[0], "-c", f"safe.directory={REPO_DIR}", *args[1:]]
    return subprocess.run(
        args, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout,
    )


def _short(commit_hash: str) -> str:
    return commit_hash.strip()[:7]


def get_local_commit() -> str:
    """Короткий хэш текущего коммита панели — дёшево (только `git
    rev-parse`, без сети), в отличие от check_for_updates() (та ещё
    делает `git ls-remote`). Прямой запрос пользователя (2026-09-24):
    "добавь чтоб в менеджере информация о сервере отображалась версия
    коммита текущего на панели" — живой повод: путаница в этой же
    сессии, действительно ли сервер уже получил вчерашний фикс nginx,
    не было простого способа проверить это прямо из TESL-Manager, не
    заходя на сервер отдельно. "?" на любую ошибку — тот же принцип,
    что get_manager_commit()/get_launcher_commit() в TESL-Manager/TESL."""
    try:
        r = _run(["git", "rev-parse", "--short", "HEAD"], TIMEOUT_CHECK)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "?"


def check_for_updates() -> dict:
    """{"local": "<short>", "remote": "<short>", "up_to_date": bool}
    либо {"error": "..."} — никогда не бросает исключение наружу, вызывающий
    (app.py) просто показывает то, что вернулось."""
    try:
        local = _run(["git", "rev-parse", "HEAD"], TIMEOUT_CHECK)
        if local.returncode != 0:
            return {"error": f"Не удалось прочитать текущий коммит: {local.stderr.strip()[:200]}"}

        remote = _run(["git", "ls-remote", "origin", "HEAD"], TIMEOUT_CHECK)
        if remote.returncode != 0:
            return {"error": f"Не удалось связаться с GitHub: {remote.stderr.strip()[:200]}"}
        remote_hash = remote.stdout.split()[0] if remote.stdout.strip() else ""
        if not remote_hash:
            return {"error": "Пустой ответ от git ls-remote — сеть или доступ к репозиторию?"}

        local_hash = local.stdout.strip()
        return {
            "local":       _short(local_hash),
            "remote":      _short(remote_hash),
            "up_to_date":  local_hash == remote_hash,
        }
    except FileNotFoundError:
        return {"error": "git не найден в PATH на сервере"}
    except subprocess.TimeoutExpired:
        return {"error": "Таймаут при проверке обновлений"}
    except Exception as e:
        return {"error": str(e)}


def apply_update() -> "tuple[bool, str]":
    """git pull --ff-only через sudo (нужен грант, см. deploy.sh), затем
    fire-and-forget перезапуск сервиса тем же путём — процесс, отвечающий
    на этот HTTP-запрос, сам себя убьёт этим рестартом, поэтому ответ
    собирается и возвращается ДО запуска systemctl restart, а сам restart
    запускается без ожидания (subprocess.Popen, не .run())."""
    try:
        pull = subprocess.run(
            ["sudo", "-n", "git", "-C", str(REPO_DIR), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=TIMEOUT_PULL,
        )
        if pull.returncode != 0:
            err = (pull.stderr or pull.stdout).strip()[:400]
            if "a password is required" in err.lower() or "sudo:" in err.lower():
                return False, (
                    "Нет прав на sudo для git pull — на сервере не установлен sudoers-грант "
                    "(запусти infra/deploy.sh заново, он ставит его идемпотентно)."
                )
            return False, f"git pull не выполнен: {err}"

        new_head = _run(["git", "rev-parse", "HEAD"], TIMEOUT_CHECK)
        new_short = _short(new_head.stdout) if new_head.returncode == 0 else "?"

        # Fire-and-forget — этот же процесс сейчас умрёт от рестарта,
        # ответ пользователю должен успеть уйти раньше.
        subprocess.Popen(["sudo", "-n", "systemctl", "restart", SERVICE_NAME])

        return True, f"Обновлено до {new_short}, сервис перезапускается — обнови страницу через несколько секунд."
    except FileNotFoundError:
        return False, "sudo или git не найдены в PATH на сервере"
    except subprocess.TimeoutExpired:
        return False, "Таймаут при обновлении"
    except Exception as e:
        return False, str(e)
