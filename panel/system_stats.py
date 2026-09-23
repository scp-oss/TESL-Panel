# ==================== panel/system_stats.py ====================
"""
Метрики сервера для страницы "Дашборд" (`/admin/dashboard`) — прямой
запрос пользователя: свободное место на выбранном диске, график нагрузки
сети (загрузка/выгрузка), загруженность ОЗУ и ЦП.

Читает `/proc/*` напрямую, без `psutil` — проект держит минимум
зависимостей (см. CLAUDE.md "Стиль": "Flask, минимум зависимостей"), а
`/proc` — единственный источник, который в любом случае нужен на этой
конкретной целевой платформе (systemd/apt/Debian, см. `infra/deploy.sh`) —
переносимость на не-Linux этому сервису не требуется, так что
`psutil`-совместимость ради портируемости не оправдывает лишнюю
зависимость.
"""
import shutil
import time
from typing import List, Optional

# Псевдо-ФС, которые не имеет смысла показывать как "диск" — не блочные
# устройства, размер либо 0, либо бессмысленен для мониторинга свободного
# места.
_PSEUDO_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2",
    "pstore", "bpf", "tracefs", "securityfs", "debugfs", "mqueue",
    "hugetlbfs", "configfs", "fusectl", "autofs", "binfmt_misc",
    "overlay", "squashfs", "ramfs", "efivarfs", "rpc_pipefs",
}


def list_disks() -> List[dict]:
    """Реальные примонтированные файловые системы — источник
    `/proc/mounts`. Дедуплицирует по mountpoint (bind-mounts того же
    устройства не показываем дважды)."""
    disks = []
    seen = set()
    try:
        with open("/proc/mounts", "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return disks

    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        mountpoint = mountpoint.replace("\\040", " ")
        if fstype in _PSEUDO_FSTYPES or mountpoint in seen:
            continue
        usage = disk_usage(mountpoint)
        if usage is None:
            continue
        seen.add(mountpoint)
        disks.append({"device": device, "fstype": fstype, **usage})

    disks.sort(key=lambda d: d["mountpoint"])
    return disks


def disk_usage(mountpoint: str) -> Optional[dict]:
    try:
        u = shutil.disk_usage(mountpoint)
    except OSError:
        return None
    return {
        "mountpoint": mountpoint,
        "total":      u.total,
        "used":       u.used,
        "free":       u.free,
        "percent":    round(u.used / u.total * 100, 1) if u.total else 0.0,
    }


def _read_proc_stat_cpu_line() -> List[int]:
    with open("/proc/stat", "r") as f:
        line = f.readline()
    return [int(x) for x in line.split()[1:]]


def cpu_percent(interval: float = 0.2) -> float:
    """Процент занятости CPU за `interval` секунд — два замера
    `/proc/stat`, разница между ними (тот же принцип, что и у `top`/
    `psutil.cpu_percent(interval=...)`, просто вручную поверх `/proc`).
    Блокирует запрос на `interval` секунд — приемлемо для редкого
    admin-опроса дашборда (не на горячем пути раздачи депо)."""
    a = _read_proc_stat_cpu_line()
    time.sleep(interval)
    b = _read_proc_stat_cpu_line()
    # Столбцы (man proc(5), /proc/stat): user nice system idle iowait
    # irq softirq steal guest guest_nice — idle+iowait считаем простоем.
    idle_a = a[3] + (a[4] if len(a) > 4 else 0)
    idle_b = b[3] + (b[4] if len(b) > 4 else 0)
    total_delta = sum(b) - sum(a)
    idle_delta  = idle_b - idle_a
    if total_delta <= 0:
        return 0.0
    return round((1 - idle_delta / total_delta) * 100, 1)


def memory_stats() -> dict:
    info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                fields = rest.strip().split()
                if fields:
                    info[key] = int(fields[0]) * 1024  # kB -> байты
    except FileNotFoundError:
        pass
    total = info.get("MemTotal", 0)
    # MemAvailable (учитывает переиспользуемый кэш/буферы) точнее, чем
    # MemFree, для "сколько реально свободно" — есть на любом ядре ≥3.14,
    # с 2026 года можно считать всегда доступным; MemFree — фолбэк.
    available = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(total - available, 0)
    return {
        "total":     total,
        "used":      used,
        "available": available,
        "percent":   round(used / total * 100, 1) if total else 0.0,
    }


def network_counters() -> dict:
    """Суммарные rx/tx байты по всем интерфейсам, кроме loopback —
    СЫРЫЕ накопительные счётчики. Скорость (байт/сек) считает JS на
    клиенте разницей между последовательными опросами (см.
    templates/dashboard.html) — без состояния на сервере: при нескольких
    gunicorn-воркерах "предыдущий замер" на сервере был бы ненадёжен
    (следующий запрос может попасть на другой воркер), а клиентский стейт
    один на открытую вкладку, всегда консистентен сам с собой."""
    rx = tx = 0
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()[2:]
        for line in lines:
            iface, _, rest = line.partition(":")
            if iface.strip() == "lo":
                continue
            fields = rest.split()
            if len(fields) >= 9:
                rx += int(fields[0])
                tx += int(fields[8])
    except FileNotFoundError:
        pass
    return {"bytes_recv": rx, "bytes_sent": tx, "ts": time.time()}
