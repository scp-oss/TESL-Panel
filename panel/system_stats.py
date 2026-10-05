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
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

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


def _mount_table() -> List[Tuple[str, str]]:
    """[(mountpoint, device), ...] из /proc/mounts, в порядке файла —
    используется для "какой блочное устройство обслуживает этот путь"
    (тот же приём, что `df`/`findmnt`: самый длинный совпадающий по
    префиксу mountpoint — путь кластера может быть подпапкой
    смонтированной точки, не самой точкой монтирования)."""
    out = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                device, mountpoint = parts[0], parts[1].replace("\\040", " ")
                out.append((mountpoint, device))
    except FileNotFoundError:
        pass
    return out


def _device_for_path(path: str) -> Optional[str]:
    """Имя блочного устройства (как в /proc/diskstats, напр. "sda1"),
    реально обслуживающего `path` — для "нагрузки кластера хранения" на
    дашборде (см. member_io() ниже). `None`, если точку монтирования не
    нашли, или устройство не блочное (tmpfs/overlay/network-fs — нет
    осмысленного diskstats-счётчика, см. _PSEUDO_FSTYPES)."""
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = path
    best_mp, best_dev = "", None
    for mountpoint, device in _mount_table():
        if (resolved == mountpoint or resolved.startswith(mountpoint.rstrip("/") + "/")) \
                and len(mountpoint) > len(best_mp):
            best_mp, best_dev = mountpoint, device
    if not best_dev or not best_dev.startswith("/dev/"):
        return None
    try:
        # realpath — LVM/mapper-устройства (/dev/mapper/vg-data) обычно
        # симлинки на /dev/dm-N, а diskstats знает только dm-N, не имя
        # mapper'а.
        real = os.path.realpath(best_dev)
    except OSError:
        real = best_dev
    return os.path.basename(real)


def disk_io_counters() -> dict:
    """Сырые накопительные sectors_read/sectors_written по каждому
    блочному устройству из /proc/diskstats (сектор = 512 байт — это
    фиксированная единица учёта ядра для этого файла, не зависит от
    реального размера сектора устройства, см. Documentation/admin-guide/
    iostats.rst). Тот же принцип, что и network_counters() — СЫРЫЕ
    счётчики, скорость считает клиент разницей между опросами (без
    состояния на сервере, безопасно под несколькими gunicorn-воркерами)."""
    out = {}
    try:
        with open("/proc/diskstats", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 10:
                    continue
                name = fields[2]
                out[name] = {
                    "read_bytes":  int(fields[5]) * 512,
                    "write_bytes": int(fields[9]) * 512,
                }
    except FileNotFoundError:
        pass
    return out


def member_io(path: str) -> Optional[dict]:
    """read_bytes/write_bytes/ts для устройства, обслуживающего `path` —
    для графика "нагрузка" у каждого члена кластера хранения на
    дашборде. `None`, если устройство не резолвится (сетевая ФС,
    tmpfs/overlay, путь не существует) — клиент в этом случае просто не
    рисует график для этого члена, карточка статуса (места) не
    затрагивается."""
    device = _device_for_path(path)
    if device is None:
        return None
    counters = disk_io_counters().get(device)
    if counters is None:
        return None
    return {"device": device, "ts": time.time(), **counters}


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
