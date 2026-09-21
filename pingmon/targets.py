"""
targets.py - probe destination catalog (mode A: no game install needed)

Each game is hosted in one or more "regions". We probe a public endpoint in
that same region. The last hop (inside the datacenter itself) is usually
well under 1 ms, so what we measure is the internet path - which is exactly
the part a ping-reduction service can change.

Endpoint patterns verified 2026-09:
  Azure : {region}.monitoring.azure.com        (24/24 regions resolve)
          {region}.api.cognitive.microsoft.com (fallback)
  AWS   : dynamodb.{region}.amazonaws.com      (same trick cloudping uses)
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
    """A single probe destination."""

    game: str            # "r6", "cs2", ...
    region: str          # human label: "UAE North"
    host: str            # hostname or IP
    port: int = 443
    method: str = "tcp"  # tcp | icmp | a2s
    note: str = ""
    genre: str = "fps"   # fps | moba -> changes score weighting
    ip: str | None = field(default=None, compare=False)

    @property
    def key(self) -> str:
        return f"{self.game}/{self.region}"

    def resolve(self, timeout: float = 5.0) -> str | None:
        """Resolve the hostname once so DNS never enters a measurement."""
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
# Regions that make sense from the Middle East / Iran come first.

#: Rainbow Six Siege runs on Microsoft Azure, and its in-game region names
#: are literally Azure region names (uaenorth, westeurope, ...).
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

#: Apex Legends - Multiplay/i3D on top of AWS. We probe the matching
#: cloud region as a stand-in.
APEX_REGIONS = [
    ("Bahrain (me-south-1)",     aws("me-south-1")),
    ("UAE (me-central-1)",       aws("me-central-1")),
    ("Frankfurt (eu-central-1)", aws("eu-central-1")),
    ("Ireland (eu-west-1)",      aws("eu-west-1")),
    ("London (eu-west-2)",       aws("eu-west-2")),
    ("Milan (eu-south-1)",       aws("eu-south-1")),
    ("Stockholm (eu-north-1)",   aws("eu-north-1")),
]

#: CoD Warzone - mixed, undocumented hosting. These are rough stand-ins;
#: use `pingmon discover` for the real thing.
WARZONE_REGIONS = [
    ("EU (Frankfurt approx)", aws("eu-central-1")),
    ("EU (Amsterdam approx)", gcp("europe-west4")),
    ("EU (London approx)",    aws("eu-west-2")),
    ("ME (Bahrain approx)",   aws("me-south-1")),
]

#: EA FC 26 - dedicated-server modes only (FUT / Clubs / Rush / VOLTA).
#: Online Seasons and Friendlies are peer-to-peer and meaningless here.
FC_REGIONS = [
    ("EU West (approx)",    aws("eu-west-1")),
    ("EU Central (approx)", aws("eu-central-1")),
    ("ME (approx)",         aws("me-south-1")),
]

#: CS2 uses Steam Datagram Relay. `pingmon sdr-refresh` fetches the live
#: list from Valve; this is only an offline fallback.
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
    Fetch Valve's live Steam Datagram Relay config and return
    {pop_code: [relay_ip, ...]}.

    Works from a normal connection; if the network blocks it, fall back to
    the offline list or enter a relay IP by hand.
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
    """Build the built-in default target set."""
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
                              note="approximate - discover recommended",
                              genre="fps"))

    if "fc26" in games:
        for label, host in FC_REGIONS[:max_per_game]:
            out.append(Target("fc26", label, host, 443, "tcp",
                              note="dedicated-server modes only",
                              genre="moba"))

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
                Target("cs2", "SDR (run sdr-refresh)", "", 27015, "tcp",
                       note="run `pingmon sdr-refresh` first", genre="fps")
            )

    return out


def targets_from_config(cfg: dict) -> list[Target]:
    """Build the target list from a config file."""
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
