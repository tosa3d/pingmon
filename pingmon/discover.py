"""
discover.py - capture a game's real server IP (mode B)

Run this mid-match. It reads the game process's UDP connections straight
from the operating system and pulls out the public destinations.

Windows : netstat -ano  +  tasklist  (no extra tooling needed)
Linux   : ss -tunp      (netstat -tunp as a fallback)

Nothing is hooked or injected - this only reads the OS connection table,
which is entirely passive and safe with anti-cheat.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import subprocess
from collections import Counter
from dataclasses import dataclass

#: Process names per game
GAME_PROCESSES = {
    "r6":      ["RainbowSix.exe", "RainbowSix_BE.exe", "RainbowSix_Vulkan.exe"],
    "cs2":     ["cs2.exe", "cs2"],
    "apex":    ["r5apex.exe", "r5apex_dx12.exe"],
    "warzone": ["cod.exe", "ModernWarfare.exe", "BlackOpsColdWar.exe",
                "Warzone.exe"],
    "fc26":    ["FC26.exe", "FC26_ver.exe", "FIFA.exe"],
}


@dataclass
class Endpoint:
    ip: str
    port: int
    proto: str
    pid: str = ""
    process: str = ""
    count: int = 1

    @property
    def key(self) -> tuple[str, int]:
        return (self.ip, self.port)


def _is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_multicast
                or a.is_link_local or a.is_reserved or a.is_unspecified)


# ---------------------------------------------------------------- windows


def _windows_processes() -> dict[str, str]:
    """{pid: image name}"""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            mapping[parts[1].strip('" ')] = parts[0].strip('" ')
    return mapping


def _windows_endpoints() -> list[Endpoint]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = _windows_processes()
    eps: list[Endpoint] = []
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4 or f[0] not in ("UDP", "TCP"):
            continue
        proto, remote, pid = f[0], f[2], f[-1]
        if proto == "TCP" and len(f) >= 5:
            remote, pid = f[2], f[4]
        m = re.match(r"^(.*):(\d+)$", remote)
        if not m:
            continue
        ip, port = m.group(1).strip("[]"), int(m.group(2))
        if not _is_public(ip):
            continue
        eps.append(Endpoint(ip, port, proto, pid, procs.get(pid, "")))
    return eps


# ---------------------------------------------------------------- linux


def _linux_endpoints() -> list[Endpoint]:
    for cmd in (["ss", "-tunp"], ["netstat", "-tunp"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if not out.strip():
            continue
        eps: list[Endpoint] = []
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) < 5:
                continue
            proto = f[0].lower()
            if proto.startswith("udp"):
                proto = "UDP"
            elif proto.startswith("tcp"):
                proto = "TCP"
            else:
                continue
            peer = f[5] if cmd[0] == "ss" and len(f) > 5 else f[4]
            m = re.match(r"^\[?([0-9a-fA-F:.]+)\]?:(\d+)$", peer)
            if not m:
                continue
            ip, port = m.group(1), int(m.group(2))
            if not _is_public(ip):
                continue
            pm = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            name, pid = (pm.group(1), pm.group(2)) if pm else ("", "")
            eps.append(Endpoint(ip, port, proto, pid, name))
        if eps:
            return eps
    return []


# ---------------------------------------------------------------- api


def snapshot() -> list[Endpoint]:
    if platform.system() == "Windows":
        return _windows_endpoints()
    return _linux_endpoints()


def collect(seconds: int = 20, interval: float = 2.0,
            game: str | None = None, on_tick=None) -> list[Endpoint]:
    """
    Samples repeatedly and returns destinations that keep showing up -
    the game server is stable for the whole match, everything else is
    transient.
    """
    import time

    wanted = [p.lower() for p in GAME_PROCESSES.get(game or "", [])]
    seen: Counter[tuple[str, int]] = Counter()
    detail: dict[tuple[str, int], Endpoint] = {}
    ticks = max(1, int(seconds / interval))

    for i in range(ticks):
        for ep in snapshot():
            if wanted and ep.process.lower() not in wanted:
                continue
            seen[ep.key] += 1
            detail.setdefault(ep.key, ep)
        if on_tick:
            on_tick(i + 1, ticks, len(seen))
        if i < ticks - 1:
            time.sleep(interval)

    out: list[Endpoint] = []
    for key, n in seen.most_common():
        ep = detail[key]
        ep.count = n
        out.append(ep)
    return out


def rank(endpoints: list[Endpoint]) -> list[Endpoint]:
    """
    Rank by how likely each endpoint is to be the game server:
    a stable UDP flow on a non-web port beats everything else.
    """
    def score(e: Endpoint) -> tuple:
        udp = e.proto == "UDP"
        not_web = e.port not in (80, 443, 8080, 8443)
        return (udp, not_web, e.count)

    return sorted(endpoints, key=score, reverse=True)
