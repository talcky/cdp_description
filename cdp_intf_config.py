#!/usr/bin/env python3
"""
cdp_intf_config.py - Generate interface configuration from CDP neighbours.

Collects `show cdp neighbors detail` from Cisco NX-OS, IOS-XE and IOS-XR
devices (via Netmiko) or from saved output files, and renders per-device
interface config snippets:

    interface port-channel10
     description INFRA-P1 nbr:CORE-SW02 po 10,sp:direct,ref:n/a
    interface Ethernet1/49
     description INFRA-P2 nbr:CORE-SW02 et 1/49,sp:direct,ref:n/a

Port-channel / bundle membership comes from the running-config
('channel-group N' or XR 'bundle id N').

Optionally applies extra per-interface config based on rules matched against
the neighbour's platform / capabilities / hostname (YAML profiles file).

The script NEVER pushes config - it writes files for review.

Usage examples
--------------
  # Live, single device, auto-detect OS
  python cdp_intf_config.py -d 10.1.1.1 -u admin

  # Live, inventory file (host[,device_type] per line)
  python cdp_intf_config.py -i hosts.txt -u admin --profiles profiles.yml

  # Offline, from saved CLI output (file stem = hostname)
  python cdp_intf_config.py --from-file captures/sw01.txt --os cisco_nxos \
      --run-dir running/        # running/sw01.txt = show running-config

Credentials: -u/--username, password from $NET_PASSWORD or prompted.
Requires: netmiko (live mode only), pyyaml (only with --profiles).
"""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("cdp_intf_config")

SUPPORTED_OS = ("cisco_ios", "cisco_xe", "cisco_nxos", "cisco_xr")

# Description length limits (characters). IOS/IOS-XE is the tightest at 240.
DESC_MAX = {"cisco_ios": 240, "cisco_xe": 240, "cisco_nxos": 254, "cisco_xr": 1024}

# Longest-match-first abbreviation table for interface names
INTF_ABBREV = [
    ("TwoHundredGigE", "TwoH"),
    ("FourHundredGigE", "FH"),
    ("HundredGigabitEthernet", "Hu"),
    ("HundredGigE", "Hu"),
    ("FiftyGigabitEthernet", "Fi"),
    ("FortyGigabitEthernet", "Fo"),
    ("FortyGigE", "Fo"),
    ("TwentyFiveGigabitEthernet", "Twe"),
    ("TwentyFiveGigE", "Twe"),
    ("TenGigabitEthernet", "Te"),
    ("TenGigE", "Te"),
    ("FiveGigabitEthernet", "Fi"),
    ("TwoGigabitEthernet", "Tw"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Ethernet", "Eth"),
    ("Port-channel", "Po"),
    ("Bundle-Ether", "BE"),
    ("MgmtEth", "Mg"),
]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class CdpNeighbor:
    local_intf: str
    remote_host: str
    remote_intf: str
    platform: str = ""
    capabilities: str = ""
    mgmt_ip: str = ""
    sw_version: str = ""
    native_vlan: str = ""
    duplex: str = ""


@dataclass
class DeviceResult:
    host: str
    hostname: str
    os: str
    neighbors: list[CdpNeighbor] = field(default_factory=list)
    members: dict[str, str] = field(default_factory=dict)  # phys intf -> channel id
    current_desc: dict[str, str] | None = None  # intf -> description (None = no config)
    error: str = ""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
ENTRY_SPLIT = re.compile(r"^-{5,}\s*$", re.MULTILINE)

RE_DEVICE_ID = re.compile(r"^\s*Device ID\s*:\s*(\S+)", re.MULTILINE)
RE_SYSNAME = re.compile(r"^\s*System Name\s*:\s*(\S+)", re.MULTILINE)
RE_LOCAL_INTF = re.compile(r"^\s*Interface\s*:\s*([^,\s]+)", re.MULTILINE)
RE_REMOTE_INTF = re.compile(r"Port ID \(outgoing port\)\s*:\s*(.+?)\s*$", re.MULTILINE)
RE_PLATFORM = re.compile(r"Platform\s*:\s*([^,\n]+?)\s*(?:,|$)", re.MULTILINE)
RE_CAPS = re.compile(r"Capabilities\s*:\s*(.+?)\s*$", re.MULTILINE)
RE_IPV4 = re.compile(r"(?:IPv4 [Aa]ddress|IP address)\s*:\s*(\d+\.\d+\.\d+\.\d+)")
RE_MGMT_BLOCK = re.compile(r"(?:Management|Mgmt) address\(es\)\s*:\s*\n(.*?)(?:\n\S|\Z)", re.DOTALL)
RE_VERSION = re.compile(r"Version\s*:\s*\n?\s*(.+?)\n", re.DOTALL)
RE_NATIVE_VLAN = re.compile(r"Native VLAN\s*:\s*(\d+)")
RE_DUPLEX = re.compile(r"Duplex\s*:\s*(\S+)")


def clean_hostname(raw: str, strip_domain: bool = True) -> str:
    """NX-OS appends '(SERIAL)'; most platforms append the domain name."""
    name = re.sub(r"\(.*?\)$", "", raw.strip())
    if strip_domain and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name):
        name = name.split(".")[0]
    return name


def shorten_intf(name: str) -> str:
    for long, short in INTF_ABBREV:
        if name.lower().startswith(long.lower()):
            return short + name[len(long):]
    return name


# 2-character interface type codes used in descriptions (longest match first)
INTF_TYPE2 = [
    ("FourHundredGigE", "fh"),
    ("TwoHundredGigE", "th"),
    ("HundredGigabitEthernet", "hu"),
    ("HundredGigE", "hu"),
    ("FiftyGigabitEthernet", "ff"),
    ("FortyGigabitEthernet", "fo"),
    ("FortyGigE", "fo"),
    ("TwentyFiveGigabitEthernet", "tf"),
    ("TwentyFiveGigE", "tf"),
    ("TenGigabitEthernet", "te"),
    ("TenGigE", "te"),
    ("FiveGigabitEthernet", "fi"),
    ("TwoGigabitEthernet", "tw"),
    ("GigabitEthernet", "gi"),
    ("FastEthernet", "fa"),
    ("Ethernet", "et"),
    ("Port-channel", "po"),
    ("Port ", "ph"),          # IP phone CDP port ('Port 1') - keep 'po' for port-channel only
    ("Bundle-Ether", "be"),
    ("MgmtEth", "mg"),
    ("mgmt", "mg"),
]


def split_intf(name: str) -> tuple[str, str]:
    """'TenGigabitEthernet1/1/1' -> ('te', '1/1/1'); 'Ethernet1/49' -> ('et', '1/49').

    Unknown types fall back to their first two letters, lower-case.
    """
    name = name.strip()
    for long, code in INTF_TYPE2:
        if name.lower().startswith(long.lower()):
            return code, name[len(long):].strip()
    m = re.match(r"^([A-Za-z][A-Za-z\-]*?)\s*(\d.*)$", name)
    if m:
        return m.group(1)[:2].lower(), m.group(2)
    return name[:2].lower(), ""


def parse_cdp_detail(output: str, strip_domain: bool = True) -> list[CdpNeighbor]:
    """Parse `show cdp neighbors detail` from IOS/IOS-XE, NX-OS or IOS-XR."""
    neighbors: list[CdpNeighbor] = []
    for block in ENTRY_SPLIT.split(output):
        dev = RE_DEVICE_ID.search(block)
        local = RE_LOCAL_INTF.search(block)
        remote = RE_REMOTE_INTF.search(block)
        if not (dev and local and remote):
            continue

        sysname = RE_SYSNAME.search(block)
        raw_name = sysname.group(1) if sysname else dev.group(1)

        # Prefer management address if present, else first entry address
        mgmt_ip = ""
        mblock = RE_MGMT_BLOCK.search(block)
        if mblock:
            m = RE_IPV4.search(mblock.group(1))
            mgmt_ip = m.group(1) if m else ""
        if not mgmt_ip:
            m = RE_IPV4.search(block)
            mgmt_ip = m.group(1) if m else ""

        def grab(rx: re.Pattern) -> str:
            m = rx.search(block)
            return m.group(1).strip() if m else ""

        platform = grab(RE_PLATFORM)
        platform = re.sub(r"^cisco\s+", "", platform, flags=re.IGNORECASE)

        neighbors.append(
            CdpNeighbor(
                local_intf=local.group(1).strip(),
                remote_host=clean_hostname(raw_name, strip_domain),
                remote_intf=remote.group(1).strip(),
                platform=platform,
                capabilities=grab(RE_CAPS),
                mgmt_ip=mgmt_ip,
                sw_version=grab(RE_VERSION)[:120],
                native_vlan=grab(RE_NATIVE_VLAN),
                duplex=grab(RE_DUPLEX),
            )
        )
    return neighbors


RE_INTF_BLOCK = re.compile(r"^interface (\S+)\n((?:[ \t]+.*\n?)*)", re.MULTILINE)
RE_MEMBER = re.compile(r"^\s*(?:channel-group|bundle id)\s+(\d+)", re.MULTILINE)

PO_NAME = {"cisco_ios": "Port-channel{}", "cisco_xe": "Port-channel{}",
           "cisco_nxos": "port-channel{}", "cisco_xr": "Bundle-Ether{}"}


def parse_channel_members(running_config: str) -> dict[str, str]:
    """Map physical interface -> port-channel/bundle id from running-config.

    Matches 'channel-group N' (IOS/IOS-XE/NX-OS) and 'bundle id N' (IOS-XR).
    Running-config uses full interface names, same as CDP detail.
    """
    members = {}
    for m in RE_INTF_BLOCK.finditer(running_config.replace("\r", "")):
        grp = RE_MEMBER.search(m.group(2))
        if grp:
            members[m.group(1)] = grp.group(1)
    return members


RE_DESC = re.compile(r"^\s+description\s+(.*?)\s*$", re.MULTILINE)


def parse_interface_descriptions(running_config: str) -> dict[str, str]:
    """Map every interface in running-config -> current description ('' if none)."""
    descs = {}
    for m in RE_INTF_BLOCK.finditer(running_config.replace("\r", "")):
        d = RE_DESC.search(m.group(2))
        descs[m.group(1)] = d.group(1) if d else ""
    return descs


def lookup_desc(descs: dict[str, str] | None, intf: str) -> str | None:
    """Case-insensitive lookup (NX-OS 'port-channel10' vs 'Port-channel10')."""
    if descs is None:
        return None
    if intf in descs:
        return descs[intf]
    low = intf.lower()
    for k, v in descs.items():
        if k.lower() == low:
            return v
    return ""  # interface not in config / no description


# --------------------------------------------------------------------------
# Profiles (optional extra config per neighbour type)
# --------------------------------------------------------------------------
def load_profiles(path: Path | None) -> list[dict]:
    if not path:
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("pyyaml is required for --profiles (pip install pyyaml)")
    data = yaml.safe_load(path.read_text()) or {}
    rules = data.get("rules", [])
    for r in rules:
        for key in ("platform", "capabilities", "hostname", "local_intf"):
            if key in r:
                r[f"_{key}_rx"] = re.compile(r[key], re.IGNORECASE)
    return rules


def match_profile(nbr: CdpNeighbor, os_type: str, rules: list[dict]) -> dict | None:
    """First matching rule wins. All supplied match keys must match."""
    fields = {
        "platform": nbr.platform,
        "capabilities": nbr.capabilities,
        "hostname": nbr.remote_host,
        "local_intf": nbr.local_intf,
    }
    for rule in rules:
        if "os" in rule and os_type not in _as_list(rule["os"]):
            continue
        if all(
            rule[f"_{k}_rx"].search(v)
            for k, v in fields.items()
            if f"_{k}_rx" in rule
        ):
            return rule
    return None


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def build_description(nbr: CdpNeighbor, fmt: str, os_type: str) -> str:
    rtype, rnum = split_intf(nbr.remote_intf)
    desc = fmt.format(
        type=rtype,
        num=rnum,
        host=nbr.remote_host,
        port=shorten_intf(nbr.remote_intf),
        port_full=nbr.remote_intf,
        platform=nbr.platform,
        ip=nbr.mgmt_ip,
    )
    desc = re.sub(r"\s+", " ", desc).strip()
    desc = desc.replace(" []", "").replace("[]", "")  # empty platform
    return desc[: DESC_MAX.get(os_type, 240)]


def render_config(
    result: DeviceResult,
    desc_fmt: str,
    rules: list[dict],
    include_mgmt: bool,
    po_prefix: str = "INFRA-P1",
    phys_prefix: str = "INFRA-P2",
    po_desc_fmt: str = "nbr:{hosts} {type} {num},sp:direct,ref:n/a",
) -> tuple[str, list[dict]]:
    lines = [
        f"! Generated from CDP on {result.hostname} ({result.host}) - os={result.os}",
        f"! {len(result.neighbors)} CDP entries. REVIEW BEFORE APPLYING.",
        "!",
    ]
    if result.os == "cisco_xr":
        lines += ["configure", "!"]

    summary: list[dict] = []
    seen: set[str] = set()
    max_len = DESC_MAX.get(result.os, 240)

    # ---- Port-channels / bundles: INFRA-P1 ----------------------------------
    po_nbrs: dict[str, list[CdpNeighbor]] = {}
    for nbr in result.neighbors:
        ch = result.members.get(nbr.local_intf)
        if ch:
            po_nbrs.setdefault(ch, []).append(nbr)

    for ch in sorted(po_nbrs, key=int):
        nbrs = po_nbrs[ch]
        po_intf = PO_NAME.get(result.os, "Port-channel{}").format(ch)
        hosts = "/".join(dict.fromkeys(n.remote_host for n in nbrs))  # vPC: two peers
        ports = ",".join(shorten_intf(n.remote_intf) for n in nbrs)
        po_type, po_num = split_intf(po_intf)
        body = po_desc_fmt.format(hosts=hosts, host=hosts, ports=ports,
                                  type=po_type, num=po_num,
                                  platform=nbrs[0].platform).strip()
        desc = f"{po_prefix} {body}".strip()[:max_len]
        lines += [f"interface {po_intf}", f" description {desc}", "!"]
        summary.append({
            "device": result.hostname, "device_ip": result.host,
            "local_intf": po_intf, "remote_host": hosts, "remote_intf": ports,
            "platform": nbrs[0].platform, "capabilities": nbrs[0].capabilities,
            "remote_mgmt_ip": "", "native_vlan": "", "profile": "",
            "description": desc,
        })

    # ---- Physical interfaces: INFRA-P2 --------------------------------------
    for nbr in sorted(result.neighbors, key=lambda n: _intf_sort_key(n.local_intf)):
        if not include_mgmt and re.match(r"(mgmt|MgmtEth)", nbr.local_intf, re.I):
            continue
        # Multiple neighbours on one port (e.g. hub / phone+PC) - keep first, warn
        if nbr.local_intf in seen:
            log.warning("%s: multiple CDP neighbours on %s - only first used",
                        result.hostname, nbr.local_intf)
            continue
        seen.add(nbr.local_intf)

        rule = match_profile(nbr, result.os, rules)
        fmt = rule.get("description", desc_fmt) if rule else desc_fmt
        desc = f"{phys_prefix} {build_description(nbr, fmt, result.os)}".strip()[:max_len]

        lines.append(f"interface {nbr.local_intf}")
        lines.append(f" description {desc}")
        if rule:
            values = asdict(nbr)
            for extra in rule.get("config", []):
                used = re.findall(r"{(\w+)}", extra)
                if any(not values.get(k) for k in used):
                    log.warning("%s %s: skipped '%s' (neighbour field empty)",
                                result.hostname, nbr.local_intf, extra)
                    continue
                lines.append(f" {extra.format(**values)}")
        lines.append("!")

        summary.append({
            "device": result.hostname,
            "device_ip": result.host,
            "local_intf": nbr.local_intf,
            "remote_host": nbr.remote_host,
            "remote_intf": nbr.remote_intf,
            "platform": nbr.platform,
            "capabilities": nbr.capabilities,
            "remote_mgmt_ip": nbr.mgmt_ip,
            "native_vlan": nbr.native_vlan,
            "profile": rule.get("name", "") if rule else "",
            "description": desc,
        })

    if result.os == "cisco_xr":
        lines += ["commit", "end"]
    return "\n".join(lines) + "\n", summary


def _intf_sort_key(name: str):
    m = re.match(r"([A-Za-z\-]+)(.*)", name)
    prefix, nums = (m.group(1), m.group(2)) if m else (name, "")
    return (prefix, [int(x) if x.isdigit() else x for x in re.split(r"[/.:]", nums) if x])


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------
def collect_live(host: str, device_type: str, username: str, password: str,
                 strip_domain: bool, timeout: int) -> DeviceResult:
    try:
        from netmiko import ConnectHandler, SSHDetect  # type: ignore
    except ImportError:
        sys.exit("netmiko is required for live collection (pip install netmiko)")

    params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "timeout": timeout,
        "fast_cli": False,
    }
    try:
        if device_type == "autodetect":
            guessed = SSHDetect(**params).autodetect()
            if guessed not in SUPPORTED_OS:
                log.warning("%s: autodetect returned %r, falling back to cisco_ios",
                            host, guessed)
                guessed = "cisco_ios"
            params["device_type"] = guessed

        with ConnectHandler(**params) as conn:
            hostname = conn.find_prompt().strip("#>").split(":")[-1]  # XR: RP/0/RSP0/CPU0:R1
            output = conn.send_command("show cdp neighbors detail", read_timeout=90)
            run_cfg = conn.send_command("show running-config", read_timeout=180)

        if re.search(r"CDP is not enabled|Invalid input|not enabled", output, re.I):
            return DeviceResult(host, hostname, params["device_type"],
                                error="CDP not enabled / command rejected")

        nbrs = parse_cdp_detail(output, strip_domain)
        members = parse_channel_members(run_cfg)
        descs = parse_interface_descriptions(run_cfg)
        log.info("%s (%s): %d neighbour(s), %d channel member(s)",
                 hostname, params["device_type"], len(nbrs), len(members))
        return DeviceResult(host, hostname, params["device_type"], nbrs, members, descs)
    except Exception as exc:  # noqa: BLE001 - report per-device, keep going
        log.error("%s: %s", host, exc)
        return DeviceResult(host, host, params["device_type"], error=str(exc))


def collect_file(path: Path, os_type: str, strip_domain: bool,
                 run_dir: Path | None = None) -> DeviceResult:
    nbrs = parse_cdp_detail(path.read_text(errors="replace"), strip_domain)
    members: dict[str, str] = {}
    descs: dict[str, str] | None = None
    run_file = None
    if run_dir:
        run_file = next((f for f in sorted(run_dir.glob(f"{path.stem}.*")) if f.is_file()), None)
    if run_file:
        run_cfg = run_file.read_text(errors="replace")
        members = parse_channel_members(run_cfg)
        descs = parse_interface_descriptions(run_cfg)
    else:
        log.warning("%s: no running-config found - port-channels not detected, "
                    "all ports treated as physical", path.stem)
    log.info("%s: %d neighbour(s), %d channel member(s)", path.name, len(nbrs), len(members))
    return DeviceResult(str(path), path.stem, os_type, nbrs, members, descs)


def read_inventory(path: Path, default_type: str) -> list[tuple[str, str]]:
    hosts = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        hosts.append((parts[0], parts[1] if len(parts) > 1 and parts[1] else default_type))
    return hosts


def write_diff(outdir: Path, rows: list[dict]) -> None:
    """description_diff.csv (for Excel) + description_diff.txt (readable)."""
    cols = ["device", "local_intf", "status", "current_description", "description"]
    csv_path = outdir / "description_diff.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["device", "interface", "status", "current_description", "new_description"])
        for r in rows:
            w.writerow([r[c] for c in cols])

    counts: dict[str, int] = {}
    txt = []
    for dev in dict.fromkeys(r["device"] for r in rows):
        txt.append(f"===== {dev} =====")
        for r in (x for x in rows if x["device"] == dev):
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            txt.append(f"{r['local_intf']}  [{r['status']}]")
            cur = ("(no running-config)" if r["status"] == "UNKNOWN"
                   else r["current_description"] or "(none)")
            txt.append(f"  current: {cur}")
            txt.append(f"  new    : {r['description']}")
        txt.append("")
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    txt.insert(0, f"Description changes - {summary}\n")
    (outdir / "description_diff.txt").write_text("\n".join(txt))
    log.info("Wrote %s and description_diff.txt (%s)", csv_path, summary)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate interface config from CDP neighbours (NX-OS / IOS-XE / IOS-XR).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("-d", "--device", action="append", help="Device IP/hostname (repeatable)")
    src.add_argument("-i", "--inventory", type=Path, help="File: host[,device_type] per line")
    src.add_argument("--from-file", type=Path, nargs="+",
                     help="Saved 'show cdp neighbors detail' output(s); file stem = hostname")

    ap.add_argument("-u", "--username", default=os.environ.get("NET_USERNAME"))
    ap.add_argument("--os", default="autodetect",
                    choices=("autodetect",) + SUPPORTED_OS,
                    help="Device type (default: autodetect; required as non-auto with --from-file)")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("cdp_configs"))
    ap.add_argument("--desc-format", default="nbr:{host} {type} {num},sp:direct,ref:n/a",
                    help="Physical description after prefix. Fields: {host} {type} {num} "
                         "{port} {port_full} {platform} {ip}")
    ap.add_argument("--run-dir", type=Path,
                    help="With --from-file: dir of running-configs named <cdp-file-stem>.*")
    ap.add_argument("--po-prefix", default="INFRA-P1", help="Port-channel/bundle prefix")
    ap.add_argument("--phys-prefix", default="INFRA-P2", help="Physical interface prefix")
    ap.add_argument("--po-desc-format", default="nbr:{hosts} {type} {num},sp:direct,ref:n/a",
                    help="Port-channel description after prefix. Fields: {hosts} {type} {num} "
                         "{ports} {platform}. {num} is the LOCAL channel number")
    ap.add_argument("--profiles", type=Path, help="YAML rules for extra per-neighbour config")
    ap.add_argument("--include-mgmt", action="store_true", help="Include mgmt0/MgmtEth ports")
    ap.add_argument("--keep-domain", action="store_true", help="Keep domain in neighbour names")
    ap.add_argument("-w", "--workers", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    strip_domain = not args.keep_domain
    rules = load_profiles(args.profiles)
    results: list[DeviceResult] = []

    if args.from_file:
        if args.os == "autodetect":
            ap.error("--os must be set explicitly with --from-file")
        results = [collect_file(p, args.os, strip_domain, args.run_dir)
                   for p in args.from_file]
    else:
        if not args.username:
            ap.error("--username (or $NET_USERNAME) required for live collection")
        password = os.environ.get("NET_PASSWORD") or getpass.getpass("Password: ")
        targets = (read_inventory(args.inventory, args.os) if args.inventory
                   else [(h, args.os) for h in args.device])
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(collect_live, h, t, args.username, password,
                                strip_domain, args.timeout) for h, t in targets]
            results = [f.result() for f in as_completed(futs)]

    args.outdir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    failed = 0

    for res in sorted(results, key=lambda r: r.hostname):
        if res.error:
            failed += 1
            continue
        if not res.neighbors:
            log.warning("%s: no CDP neighbours found", res.hostname)
            continue
        cfg, rows = render_config(res, args.desc_format, rules, args.include_mgmt,
                                  args.po_prefix, args.phys_prefix, args.po_desc_format)
        for row in rows:
            cur = lookup_desc(res.current_desc, row["local_intf"])
            row["current_description"] = "" if cur is None else cur
            row["status"] = ("UNKNOWN" if cur is None else
                             "UNCHANGED" if cur == row["description"] else
                             "NEW" if cur == "" else "CHANGED")
        out = args.outdir / f"{res.hostname}.cfg"
        out.write_text(cfg)
        all_rows.extend(rows)
        log.info("Wrote %s (%d interfaces)", out, len(rows))

    if all_rows:
        csv_path = args.outdir / "cdp_summary.csv"
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        log.info("Wrote %s (%d rows)", csv_path, len(all_rows))
        write_diff(args.outdir, all_rows)

    if failed:
        log.error("%d device(s) failed:", failed)
        for r in results:
            if r.error:
                log.error("  %s: %s", r.host, r.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
