"""
targets.py — پایگاه داده‌ی مقصدهای پروب (حالت الف: بدون نیاز به نصب بازی)

هر بازی روی یک یا چند «ریجن» هاست می‌شه. ما به endpoint عمومیِ همون ریجن
پروب می‌زنیم. هاپ آخر (داخل خود دیتاسنتر) معمولاً زیر ۱ms ـه، پس مسیر
اینترنتی‌ای که سرویس کاهش پینگ روش اثر می‌ذاره همینه.

الگوهای endpoint در ۲۰۲۶-۰۹ تست و تایید شدن:
  Azure : {region}.monitoring.azure.com        (پوشش ۲۴/۲۴ ریجن)
          {region}.api.cognitive.microsoft.com (فالبک)
  AWS   : dynamodb.{region}.amazonaws.com      (همون روشی که cloudping می‌زنه)
  GCP   : {region}-run.googleapis.com
"""

from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------- endpoints

AZURE_PRIMARY = "{region}.monitoring.azure.com"
AZURE_FALLBACK = "{region}.api.cognitive.microsoft.com"
AWS_PRIMARY = "dynamodb.{region}.amazonaws.com"
GCP_PRIMARY = "{region}-run.googleapis.com"


def azure(region: str) -> str:
    return AZURE_PRIMARY.format(region=region)


def aws(region: str) -> str:
    return AWS_PRIMARY.format(region=region)


def gcp(region: str) -> str:
    return GCP_PRIMARY.format(region=region)


# ---------------------------------------------------------------- model


@dataclass
class Target:
    """یک مقصد پروب."""

    game: str            # "r6"، "cs2"، ...
    region: str          # برچسب خوانا: "UAE North"
    host: str            # hostname یا IP
    port: int = 443
    method: str = "tcp"  # tcp | icmp | a2s
    note: str = ""
    genre: str = "fps"   # fps | moba  → روی وزن‌دهی امتیاز اثر داره
    ip: str | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        return f"{self.game}/{self.region}"

    def resolve(self, timeout: float = 5.0) -> str | None:
        """hostname را یک‌بار به IP تبدیل می‌کند تا DNS وارد اندازه‌گیری نشود."""
        if self.ip:
            return self.ip
        try:
            socket.setdefaulttimeout(timeout)
            infos = socket.getaddrinfo(
                self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
            )
            self.ip = infos[0][4][0]
        except OSError:
            self.ip = None
        finally:
            socket.setdefaulttimeout(None)
        return self.ip


# ---------------------------------------------------------------- catalog
# ریجن‌هایی که برای کاربر خاورمیانه/ایران منطقی‌ان، اول لیست.

#: Rainbow Six Siege — روی Microsoft Azure هاست می‌شه و اسم ریجن‌هاش
#: عیناً اسم ریجن‌های Azure ـه (uaenorth، westeurope، ...).
R6_REGIONS = [
    ("UAE North",      "uaenorth"),
    ("West Europe",    "westeurope"),
    ("North Europe",   "northeurope"),
    ("France Central", "francecentral"),
    ("Germany WC",     "germanywestcentral"),
    ("Poland Central", "polandcentral"),
    ("Qatar Central",  "qatarcentral"),
    ("UK South",       "uksouth"),
    ("Sweden Central", "swedencentral"),
    ("East US",        "eastus"),
]

#: Apex Legends — Multiplay/i3D + AWS. به ریجن ابری معادل پروب می‌زنیم.
APEX_REGIONS = [
    ("Bahrain (me-south-1)",   aws("me-south-1")),
    ("UAE (me-central-1)",     aws("me-central-1")),
    ("Frankfurt (eu-central-1)", aws("eu-central-1")),
    ("Ireland (eu-west-1)",    aws("eu-west-1")),
    ("London (eu-west-2)",     aws("eu-west-2")),
    ("Milan (eu-south-1)",     aws("eu-south-1")),
    ("Stockholm (eu-north-1)", aws("eu-north-1")),
]

#: CoD Warzone — هاست ترکیبی و مستند نشده. اینها تقریب خام‌ان؛
#: برای نتیجه‌ی دقیق از `pingmon discover` استفاده کن.
WARZONE_REGIONS = [
    ("EU (Frankfurt approx)", aws("eu-central-1")),
    ("EU (Amsterdam approx)", gcp("europe-west4")),
    ("EU (London approx)",    aws("eu-west-2")),
    ("ME (Bahrain approx)",   aws("me-south-1")),
]

#: EA FC 26 — فقط مودهای سرور اختصاصی (FUT / Clubs / Rush / VOLTA).
#: مودهای Seasons و Friendlies نقطه‌به‌نقطه‌ان و اینجا معنی ندارن.
FC_REGIONS = [
    ("EU West (approx)",   aws("eu-west-1")),
    ("EU Central (approx)", aws("eu-central-1")),
    ("ME (approx)",        aws("me-south-1")),
]

#: CS2 — Steam Datagram Relay. کدهای PoP والو.
#: لیست معتبر رو `pingmon sdr-refresh` زنده از والو می‌گیره؛
#: این فقط فالبک آفلاینه.
VALVE_POP_FALLBACK = {
    "fra": "Frankfurt",
    "vie": "Vienna",
    "waw": "Warsaw",
    "sto": "Stockholm",
    "lhr": "London",
    "ams": "Amsterdam",
    "par": "Paris",
    "mad": "Madrid",
    "dxb": "Dubai",
    "bom": "Mumbai",
    "maa": "Chennai",
    "jnb": "Johannesburg",
}

SDR_CONFIG_URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1/?appid=730"


def fetch_sdr_pops(timeout: float = 15.0) -> dict[str, list[str]]:
    """
    کانفیگ زنده‌ی Steam Datagram Relay را از والو می‌گیرد و
    {pop_code: [relay_ip, ...]} برمی‌گرداند.

    روی سیستم خودت کار می‌کند؛ اگر شبکه اجازه نداد، خطا می‌دهد و
    باید از لیست فالبک یا IP دستی استفاده کنی.
    """
    req = urllib.request.Request(
        SDR_CONFIG_URL, headers={"User-Agent": "pingmon/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    pops: dict[str, list[str]] = {}
    for code, entry in (data.get("pops") or {}).items():
        ips: list[str] = []
        for relay in entry.get("relays") or []:
            ip = relay.get("ipv4")
            if ip:
                ips.append(ip)
        if ips:
            pops[code] = ips
    return pops


# ---------------------------------------------------------------- builders


def build_default_targets(
    games: list[str] | None = None,
    sdr_pops: dict[str, list[str]] | None = None,
    max_per_game: int = 4,
) -> list[Target]:
    """مجموعه‌ی پیش‌فرض مقصدها را می‌سازد."""
    games = games or ["r6", "cs2", "apex", "warzone", "fc26"]
    out: list[Target] = []

    if "r6" in games:
        for label, region in R6_REGIONS[:max_per_game]:
            out.append(
                Target("r6", label, azure(region), 443, "tcp",
                       note=f"Azure {region}", genre="fps")
            )

    if "apex" in games:
        for label, host in APEX_REGIONS[:max_per_game]:
            out.append(Target("apex", label, host, 443, "tcp",
                              note="AWS region proxy", genre="fps"))

    if "warzone" in games:
        for label, host in WARZONE_REGIONS[:max_per_game]:
            out.append(Target("warzone", label, host, 443, "tcp",
                              note="تقریبی — discover توصیه می‌شود",
                              genre="fps"))

    if "fc26" in games:
        for label, host in FC_REGIONS[:max_per_game]:
            out.append(Target("fc26", label, host, 443, "tcp",
                              note="فقط مودهای سرور اختصاصی", genre="moba"))

    if "cs2" in games:
        if sdr_pops:
            picked = list(sdr_pops.items())[:max_per_game]
            for code, ips in picked:
                name = VALVE_POP_FALLBACK.get(code, code)
                out.append(
                    Target("cs2", f"{name} ({code})", ips[0], 27015, "tcp",
                           note="Valve SDR relay", genre="fps", ip=ips[0])
                )
        else:
            out.append(
                Target("cs2", "SDR (نیاز به sdr-refresh)", "", 27015, "tcp",
                       note="`pingmon sdr-refresh` را اجرا کن", genre="fps")
            )

    return out


def targets_from_config(cfg: dict) -> list[Target]:
    """مقصدها را از فایل کانفیگ می‌سازد."""
    out: list[Target] = []
    for game_cfg in cfg.get("games") or []:
        if not game_cfg.get("enabled", True):
            continue
        game = game_cfg["name"]
        genre = game_cfg.get("genre", "fps")
        default_method = game_cfg.get("method", "tcp")
        for t in game_cfg.get("targets") or []:
            out.append(
                Target(
                    game=game,
                    region=t.get("region") or t.get("host", "?"),
                    host=t.get("host", ""),
                    port=int(t.get("port", 443)),
                    method=t.get("method", default_method),
                    note=t.get("note", ""),
                    genre=genre,
                    ip=t.get("ip"),
                )
            )
    return out
