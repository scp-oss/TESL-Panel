# ==================== panel/github_releases.py ====================
"""
Скачивает и кэширует ссылку на последний .exe-релиз лаунчера/менеджера с
GitHub — без токена (публичные репозитории), см. config.py::GITHUB_CACHE_TTL
за причину, почему это вообще кэшируется, а не запрашивается на каждый
показ страницы.
"""
import threading
import time
from typing import Optional

import requests

from . import config

_lock  = threading.Lock()
_cache: dict = {}   # key -> {"data": {...}|None, "fetched_at": float}


def _fetch_latest_release(owner: str, repo: str) -> Optional[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        r = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "tesl-panel"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        payload = r.json()
    except Exception:
        return None

    exe_asset = next(
        (a for a in payload.get("assets", []) if a.get("name", "").lower().endswith(".exe")),
        None,
    )
    if exe_asset is None:
        return None

    return {
        "tag":          payload.get("tag_name", "?"),
        "published_at": payload.get("published_at", ""),
        "download_url": exe_asset["browser_download_url"],
        "asset_name":   exe_asset["name"],
        "size_bytes":   exe_asset.get("size", 0),
        "html_url":     payload.get("html_url", f"https://github.com/{owner}/{repo}/releases"),
    }


def get_latest_release(key: str) -> Optional[dict]:
    """key — один из config.GITHUB_REPOS (напр. 'launcher'/'manager')."""
    spec = config.GITHUB_REPOS.get(key)
    if spec is None:
        return None

    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached["fetched_at"] < config.GITHUB_CACHE_TTL:
            return cached["data"]

    data = _fetch_latest_release(spec["owner"], spec["repo"])
    with _lock:
        # Живой релиз не найден (сеть/429/репозиторий без релизов) — держим
        # СТАРОЕ закэшированное значение, если оно есть, вместо того чтобы
        # затирать рабочую ссылку на None из-за временного сбоя GitHub.
        if data is not None or key not in _cache:
            _cache[key] = {"data": data, "fetched_at": now}
        else:
            _cache[key]["fetched_at"] = now
        return _cache[key]["data"]
