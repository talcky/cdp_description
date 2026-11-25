# cdp_intf_config.py

Generates interface description config for Cisco NX-OS, IOS/IOS-XE and IOS-XR
from CDP neighbour data, using a standard description format. Also produces a
report comparing current and new descriptions.

**The script never pushes config.** It only writes files for review.

---

## Description format

```
<prefix> nbr:<neighbour> <type> <number>,sp:direct,ref:n/a
```

| Interface kind              | Prefix     |
|-----------------------------|------------|
| Port-channel / Bundle-Ether | `INFRA-P1` |
| Physical interface          | `INFRA-P2` |

`<type>` and `<number>` are the **neighbour's** port, as reported by CDP.

### Examples

```
interface port-channel10
 description INFRA-P1 nbr:N9K-CORE02 po 10,sp:direct,ref:n/a
interface Ethernet1/49
 description INFRA-P2 nbr:N9K-CORE02 et 1/49,sp:direct,ref:n/a
interface Ethernet1/5
 description INFRA-P2 nbr:SRV-SW01 tf 1/1/1,sp:direct,ref:n/a
interface GigabitEthernet1/0/12
 description INFRA-P2 nbr:AP-FLOOR2 gi 0,sp:direct,ref:n/a
```

IOS-XR output is wrapped in `configure` … `commit` / `end`.

### Interface type codes

| Interface                       | Code |
|---------------------------------|------|
| Ethernet (NX-OS)                | `et` |
| FastEthernet                    | `fa` |
| GigabitEthernet                 | `gi` |
| TwoGigabitEthernet (2.5G)       | `tw` |
| FiveGigabitEthernet             | `fi` |
| TenGigabitEthernet / TenGigE    | `te` |
| TwentyFiveGigabitEthernet / TwentyFiveGigE | `tf` |
| FortyGigabitEthernet / FortyGigE | `fo` |
| FiftyGigabitEthernet            | `ff` |
| HundredGigabitEthernet / HundredGigE | `hu` |
| TwoHundredGigE                  | `th` |
| FourHundredGigE                 | `fh` |
| Port-channel                    | `po` |
| Bundle-Ether (IOS-XR)           | `be` |
| mgmt / MgmtEth                  | `mg` |
| IP phone port (`Port 1`)        | `ph` |

Unlisted types fall back to their first two letters in lower case. The codes
are set in the `INTF_TYPE2` table near the top of the script.

---

## Requirements

- Python 3.9+
- `netmiko`: live collection only
- `pyyaml`: only when using `--profiles`

```
pip install netmiko pyyaml
```

---

## Usage

### Live: single device

```
python cdp_intf_config.py -d 10.1.1.1 -u admin
```

### Live: inventory file

```
python cdp_intf_config.py -i hosts.txt -u admin
```

`hosts.txt`: one device per line, with an optional Netmiko device type
(otherwise auto-detected):

```
# host[,device_type]
10.1.1.1
10.1.1.2,cisco_nxos
10.1.1.3,cisco_xr
```

Credentials: `-u` (or `$NET_USERNAME`). The password comes from `$NET_PASSWORD`,
or the script prompts for it.

On each device the script runs:

- `show cdp neighbors detail`: neighbours
- `show running-config`: port-channel membership and current descriptions

### Offline: saved output

```
python cdp_intf_config.py --from-file captures/*.txt --os cisco_nxos --run-dir running/
```

- `captures/sw01.txt`: `show cdp neighbors detail` output. The file name
  (without extension) is used as the hostname.
- `running/sw01.txt`: `show running-config` for the same device. It must have
  the same file name stem.
- `--os` must be set explicitly, so run each OS as a separate batch.

If no running-config is found for a device, port-channels can't be detected
(all ports are treated as physical) and the current description shows as
`UNKNOWN`.

---

## Output files

All files go to `cdp_configs/` (change with `-o`).

| File                      | Contents |
|---------------------------|----------|
| `<hostname>.cfg`          | Config to review and paste, one file per device |
| `description_diff.txt`    | Readable current vs new descriptions, per device |
| `description_diff.csv`    | Same comparison for Excel |
| `cdp_summary.csv`         | Full neighbour detail: remote host/port, platform, capabilities, mgmt IP, native VLAN, profile matched |

### Diff status values

| Status      | Meaning |
|-------------|---------|
| `CHANGED`   | Has a description; the new one differs |
| `NEW`       | No description today |
| `UNCHANGED` | Already matches |
| `UNKNOWN`   | No running-config available for that device |

Example `description_diff.txt`:

```
Description changes - CHANGED 2, NEW 1, UNCHANGED 1

===== nxos-sw01 =====
port-channel10  [CHANGED]
  current: old-uplink-to-core
  new    : INFRA-P1 nbr:N9K-CORE02 po 10,sp:direct,ref:n/a
Ethernet1/49  [UNCHANGED]
  current: INFRA-P2 nbr:N9K-CORE02 et 1/49,sp:direct,ref:n/a
  new    : INFRA-P2 nbr:N9K-CORE02 et 1/49,sp:direct,ref:n/a
```

---

## Options

| Option | Default | Purpose |
|--------|---------|---------|
| `-d`, `--device` | | Device IP/hostname (repeatable) |
| `-i`, `--inventory` | | Inventory file |
| `--from-file` | | Saved CDP output file(s) |
| `--run-dir` | | Running-configs for `--from-file` |
| `-u`, `--username` | `$NET_USERNAME` | SSH username |
| `--os` | `autodetect` | `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_xr` |
| `-o`, `--outdir` | `cdp_configs` | Output folder |
| `--phys-prefix` | `INFRA-P2` | Physical interface prefix |
| `--po-prefix` | `INFRA-P1` | Port-channel / bundle prefix |
| `--desc-format` | `nbr:{host} {type} {num},sp:direct,ref:n/a` | Physical description (after prefix) |
| `--po-desc-format` | `nbr:{hosts} {type} {num},sp:direct,ref:n/a` | Port-channel description (after prefix) |
| `--profiles` | | YAML rules file (see below) |
| `--include-mgmt` | off | Include `mgmt0` / `MgmtEth` ports |
| `--keep-domain` | off | Keep the domain suffix on neighbour names |
| `-w`, `--workers` | `10` | Parallel SSH sessions |
| `--timeout` | `30` | SSH timeout (seconds) |
| `-v` | | Debug logging |

### Format fields

Physical (`--desc-format`):

- `{host}`: neighbour hostname
- `{type}` and `{num}`: neighbour port type code and number
- `{port}`: neighbour port in short form, e.g. `Eth1/49`
- `{port_full}`: neighbour port in full
- `{platform}`: neighbour platform
- `{ip}`: neighbour management IP

Port-channel (`--po-desc-format`):

- `{hosts}`: neighbour hostname(s)
- `{type}` and `{num}`: the **local** channel type and number
- `{ports}`: the neighbour's member ports
- `{platform}`: neighbour platform

---

## Profiles (optional)

`profiles.yml` adds extra config lines, or a different description, to ports
based on the neighbour. The first matching rule wins. All match keys are
case-insensitive regexes, and every key given must match:

```yaml
rules:
  - name: ip-phone
    capabilities: "Phone"
    config:
      - switchport mode access
      - switchport voice vlan 20

  - name: bt-circuit
    hostname: "^PE-BT"
    description: "nbr:{host} {type} {num},sp:BT,ref:ABC123"

  - name: router-p2p
    os: [cisco_ios, cisco_xe, cisco_nxos]
    capabilities: "Router"
    config:
      - no switchport
```

- **Match keys:** `platform`, `capabilities`, `hostname`, `local_intf`, plus an
  optional `os`.
- **Config line fields:** `{remote_host}`, `{remote_intf}`, `{platform}`,
  `{mgmt_ip}`, `{native_vlan}`. If a field is empty for a neighbour, that line
  is skipped and a warning is logged.
- **Prefix:** the INFRA-P1/P2 prefix is always added, including to profile
  descriptions.

The VLANs in the supplied `profiles.yml` are placeholders, so edit them before use.

---

## Behaviour and limitations

- **Port-channel membership** comes from `channel-group N` (IOS/IOS-XE/NX-OS) or
  `bundle id N` (IOS-XR) in the running-config.
- **Port-channel number** in the description is the **local** channel ID. CDP
  doesn't report the far end's channel number.
- **vPC / multi-chassis:** if members go to two different switches, both names
  are shown, e.g. `nbr:CORE01/CORE02`.
- **Multiple CDP neighbours on one port:** only the first is used, and a
  warning is logged.
- **Neighbour names:** the NX-OS serial suffix `(FDO…)` and the domain suffix
  are removed (use `--keep-domain` to keep the domain).
- **Description length:** truncated to the platform maximum (IOS-XE 240,
  NX-OS 254, IOS-XR 1024).
- **Excluded ports:** management ports are skipped unless `--include-mgmt` is set.
- **CDP disabled:** devices with CDP disabled are reported as failed, and the
  script exits with code 1.
- **`.cfg` contents:** the `.cfg` files contain every CDP-learned interface,
  including those marked `UNCHANGED` in the diff.
