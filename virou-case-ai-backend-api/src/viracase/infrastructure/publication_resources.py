import os
import resource
from pathlib import Path, PurePosixPath

WORKLOAD_NPROC = 64
LIMIT_APPLICATION_STAGE = "inside_sandbox_before_node"


def workload_nproc():
    inherited = resource.getrlimit(resource.RLIMIT_NPROC)
    return min([WORKLOAD_NPROC, *(value for value in inherited if value != resource.RLIM_INFINITY)])


def _read(path):
    try:
        with path.open() as stream:
            return stream.read(8192).strip()
    except (OSError, UnicodeError):
        return None


def _value(path):
    value = _read(path)
    if value == "max":
        return "max"
    return int(value) if value and value.isdecimal() else None


def _events(path, allowed):
    result = {}
    for line in (_read(path) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in allowed and parts[1].isdecimal():
            result[parts[0]] = int(parts[1])
    return result


def _cgroup_paths(proc, root, controller):
    for line in (_read(proc / "self/cgroup") or "").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hierarchy, controllers, membership = fields
        unified = hierarchy == "0" and not controllers
        if not unified and controller not in controllers.split(","):
            continue
        relative = PurePosixPath(membership)
        if not relative.is_absolute() or ".." in relative.parts:
            continue
        base = root if unified else root / controller
        current = base.joinpath(*relative.parts[1:])
        metric = "memory.usage_in_bytes" if controller == "memory" and not unified else controller + ".current"
        if not (current / metric).exists():
            continue
        paths = []
        while current.is_relative_to(base) and len(paths) < 16:
            paths.append(current)
            if current == base:
                break
            current = current.parent
        return ("v2" if unified else "v1"), paths
    return None, []


def _cgroup_snapshot(proc, root, controller):
    version, paths = _cgroup_paths(proc, root, controller)
    levels = []
    for depth, path in enumerate(paths):
        if controller == "pids":
            current, maximum = "pids.current", "pids.max"
            events = _events(path / "pids.events", {"max"})
        elif version == "v2":
            current, maximum = "memory.current", "memory.max"
            events = _events(path / "memory.events", {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"})
        else:
            current, maximum = "memory.usage_in_bytes", "memory.limit_in_bytes"
            count = _value(path / "memory.failcnt")
            events = {"max": count} if isinstance(count, int) else {}
        levels.append({"depth": depth, "current": _value(path / current), "max": _value(path / maximum), "events": events})
    return {"version": version, "visibility": "mounted_hierarchy_only", "levels": levels}


def resource_snapshot(proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    limits = {}
    for name, kind in (("nproc", resource.RLIMIT_NPROC), ("address_space", resource.RLIMIT_AS), ("open_files", resource.RLIMIT_NOFILE)):
        try:
            soft, hard = resource.getrlimit(kind)
            limits[name] = {"soft": "unlimited" if soft == resource.RLIM_INFINITY else soft, "hard": "unlimited" if hard == resource.RLIM_INFINITY else hard}
        except (OSError, ValueError):
            limits[name] = {"soft": None, "hard": None}
    return {
        "uid": os.getuid(),
        "rlimits": limits,
        "pids_cgroup": _cgroup_snapshot(proc, cgroup, "pids"),
        "memory_cgroup": _cgroup_snapshot(proc, cgroup, "memory"),
        "kernel_threads_max": _value(proc / "sys/kernel/threads-max"),
        "kernel_pid_max": _value(proc / "sys/kernel/pid_max"),
        "workload_nproc_max": WORKLOAD_NPROC,
        "nproc_application_stage": LIMIT_APPLICATION_STAGE,
    }


def resource_evidence(before, after):
    evidence = []
    for group, event_names, code in (
        ("pids_cgroup", ("max",), "CGROUP_PIDS_LIMIT_REACHED"),
        ("memory_cgroup", ("oom", "oom_kill"), "CGROUP_MEMORY_PRESSURE"),
    ):
        previous = {level["depth"]: level for level in (before or {}).get(group, {}).get("levels", [])}
        for level in (after or {}).get(group, {}).get("levels", []):
            prior = previous.get(level["depth"], {})
            rising = any(
                event in level.get("events", {}) and event in prior.get("events", {})
                and level["events"][event] > prior["events"][event]
                for event in event_names
            )
            current, maximum = level.get("current"), level.get("max")
            saturated = group == "pids_cgroup" and isinstance(current, int) and isinstance(maximum, int) and current >= maximum
            if rising or saturated:
                evidence.append({"code": code, "depth": level["depth"], "event_increased": rising, "at_limit": saturated})
    return evidence
