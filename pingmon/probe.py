"""
probe.py — موتور اندازه‌گیری

سه روش پروب:
  tcp  — زمان دست‌دادن TCP (یک RTT کامل). بدون نیاز به دسترسی ادمین.
         پیش‌فرض، و روی ویندوز و لینوکس یکسان کار می‌کند.
  icmp — ping خام. نزدیک‌ترین چیز به ترافیک بازی، ولی نیاز به ادمین/root
         دارد و بعضی سرورها ICMP را drop یا rate-limit می‌کنند.
  a2s  — کوئری Source engine روی UDP. فقط برای سرورهای Source/GoldSrc.

نکته‌ی مهم: در روش tcp، پاسخ «connection refused» هم یک اندازه‌گیری
معتبر است — یعنی SYN رسید و RST برگشت، دقیقاً یک RTT. پس آن را موفق
حساب می‌کنیم نه گم‌شده.
"""

from __future__ import annotations

import os
import random
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

from .targets import Target


@dataclass(slots=True)
class Sample:
    ts: float          # unix timestamp
    seq: int
    rtt_ms: float | None
    ok: bool
    err: str = ""


# ---------------------------------------------------------------- TCP


def probe_tcp(ip: str, port: int, timeout: float = 2.0) -> Sample:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # RST به جای FIN هنگام بستن: جلوی انباشت TIME_WAIT و
        # تمام‌شدن پورت‌های ephemeral در اجرای چندساعته را می‌گیرد.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     struct.pack("ii", 1, 0))
        s.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            s.connect((ip, port))
        except ConnectionRefusedError:
            # RST برگشت ⇒ مسیر رفت‌وبرگشت کامل شد. اندازه‌گیری معتبر است.
            pass
        rtt = (time.perf_counter() - t0) * 1000.0
        return Sample(time.time(), 0, rtt, True)
    except socket.timeout:
        return Sample(time.time(), 0, None, False, "timeout")
    except OSError as e:
        return Sample(time.time(), 0, None, False, type(e).__name__)
    finally:
        try:
            s.close()
        except OSError:
            pass


# ---------------------------------------------------------------- ICMP


def _icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class IcmpProber:
    """پروب ICMP. نیاز به ادمین (ویندوز) یا root/CAP_NET_RAW (لینوکس)."""

    def __init__(self) -> None:
        self.ident = os.getpid() & 0xFFFF
        self._seq = 0
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                              socket.IPPROTO_ICMP)
            s.close()
            return True
        except (OSError, AttributeError):
            return False

    def probe(self, ip: str, timeout: float = 2.0,
              payload: int = 32) -> Sample:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFF
            seq = self._seq

        header = struct.pack("!BBHHH", 8, 0, 0, self.ident, seq)
        data = bytes(random.getrandbits(8) for _ in range(payload))
        chk = _icmp_checksum(header + data)
        packet = struct.pack("!BBHHH", 8, 0, chk, self.ident, seq) + data

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                              socket.IPPROTO_ICMP)
        except OSError as e:
            return Sample(time.time(), seq, None, False, f"raw socket: {e}")

        try:
            s.setblocking(False)
            t0 = time.perf_counter()
            s.sendto(packet, (ip, 0))
            deadline = t0 + timeout
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return Sample(time.time(), seq, None, False, "timeout")
                ready, _, _ = select.select([s], [], [], remaining)
                if not ready:
                    return Sample(time.time(), seq, None, False, "timeout")
                recv, addr = s.recvfrom(2048)
                t1 = time.perf_counter()
                if addr[0] != ip:
                    continue
                ihl = (recv[0] & 0x0F) * 4
                icmp = recv[ihl:ihl + 8]
                if len(icmp) < 8:
                    continue
                r_type, _, _, r_id, r_seq = struct.unpack("!BBHHH", icmp)
                if r_type == 0 and r_id == self.ident and r_seq == seq:
                    return Sample(time.time(), seq, (t1 - t0) * 1000.0, True)
        except OSError as e:
            return Sample(time.time(), seq, None, False, type(e).__name__)
        finally:
            s.close()


# ---------------------------------------------------------------- A2S (UDP)

A2S_INFO = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"


def probe_a2s(ip: str, port: int, timeout: float = 2.0) -> Sample:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(A2S_INFO, (ip, port))
        payload, _ = s.recvfrom(4096)
        rtt = (time.perf_counter() - t0) * 1000.0
        # پاسخ challenge (0x41) هم یک RTT کامل است.
        if payload[:4] == b"\xFF\xFF\xFF\xFF":
            return Sample(time.time(), 0, rtt, True)
        return Sample(time.time(), 0, rtt, True, "unexpected")
    except socket.timeout:
        return Sample(time.time(), 0, None, False, "timeout")
    except OSError as e:
        return Sample(time.time(), 0, None, False, type(e).__name__)
    finally:
        s.close()


# ---------------------------------------------------------------- runner


class TargetProbeThread(threading.Thread):
    """
    یک ترد به ازای هر مقصد. با نرخ ثابت پروب می‌زند و نمونه‌ها را
    در صف می‌ریزد. نرخ ثابت برای اندازه‌گیری jitter حیاتی است.
    """

    def __init__(self, target: Target, rate_hz: float, timeout: float,
                 on_sample, icmp: IcmpProber | None = None) -> None:
        super().__init__(daemon=True, name=f"probe-{target.key}")
        self.target = target
        self.interval = 1.0 / max(rate_hz, 0.1)
        self.timeout = timeout
        self.on_sample = on_sample
        self.icmp = icmp
        self._stopping = threading.Event()
        self._paused = threading.Event()
        self._paused.set()      # set = در حال اجرا
        self.seq = 0

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def stop(self) -> None:
        self._stopping.set()
        self._paused.set()

    def _one(self) -> Sample:
        ip = self.target.ip or self.target.resolve()
        if not ip:
            return Sample(time.time(), self.seq, None, False, "dns")
        m = self.target.method
        if m == "icmp" and self.icmp is not None:
            return self.icmp.probe(ip, self.timeout)
        if m == "a2s":
            return probe_a2s(ip, self.target.port, self.timeout)
        return probe_tcp(ip, self.target.port, self.timeout)

    def run(self) -> None:
        next_at = time.perf_counter()
        while not self._stopping.is_set():
            self._paused.wait()
            if self._stopping.is_set():
                break
            sample = self._one()
            self.seq += 1
            sample.seq = self.seq
            try:
                self.on_sample(self.target, sample)
            except Exception:       # noqa: BLE001 — یک نمونه نباید ترد را بکشد
                pass
            next_at += self.interval
            sleep_for = next_at - time.perf_counter()
            if sleep_for < -self.interval * 5:
                # خیلی عقب افتادیم (مثلاً بعد از یک سری timeout) — ریست
                next_at = time.perf_counter()
                sleep_for = 0.0
            if sleep_for > 0:
                self._stopping.wait(sleep_for)
