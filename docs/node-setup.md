# Node setup — preparing the Raspberry Pi 5 camera nodes

Two Pis, one camera each, one identical procedure. Budget about an hour for the
first one and fifteen minutes for the second.

```bash
sudo ./scripts/provision-node.sh --role 0 --ip 10.10.0.10/24 --ntp 10.10.0.1
sudo reboot
sudo ./scripts/check-node.sh
```

`provision-node.sh` is idempotent — re-running it is the supported way to
repair a node or pick up a change. `check-node.sh` shares no code with it on
purpose: a provisioning step that silently did nothing still reports success,
and only an independent observer catches that.

**Nothing here is on the critical path to first light.** The node runs fine on
the laptop with the camera plugged straight in, which is how
[`day-one.md`](day-one.md) does it. Moving to the Pis is a transport change and
should not alter a single measurement. If it does, something else is wrong.

---

## The instructions this replaces were broken

Raspberry Pi OS is now Debian 13 **Trixie**. The four-line node recipe that used
to live in [`hardware.md`](hardware.md) fails at three separate points on it, and
each failure looks like something else:

| Old instruction | What happens now |
|---|---|
| `pip install -r requirements.txt` | `error: externally-managed-environment` (PEP 668). Worse, `python3-pip` is not installed on Lite at all, so even `--break-system-packages` is unreachable. **A venv is the only path.** |
| `apt install hailo-dkms` | Package does not exist on Trixie. The driver was renamed **`hailort-pcie-driver`**, and it needs `dkms` installed first because it does not depend on it. |
| `ExecStart=/usr/bin/python3 -m uvicorn ...` | System Python cannot see the venv. The service starts, fails to import fastapi, and `Restart=always` hides it as a restart loop. |
| `pip install ... gphoto2` after `libgphoto2-dev` | Works, but the dev package is unnecessary — the aarch64 wheel bundles libgphoto2, libusb and `ptp2.so`. What you actually need from apt is `libgphoto2-6t64`, **for its udev rules**. |

Raspberry Pi do not support an in-place Bookworm → Trixie upgrade. Reflash.
`provision-node.sh` refuses to run on Bookworm rather than half-working.

---

## 1. Flash the card

**Raspberry Pi OS Lite, 64-bit.** Lite is not a preference. On a Desktop image
the file manager auto-mounts the camera and `gvfsd-gphoto2` then holds the USB
interface, so every command fails with *Could not claim the USB device*; Lite has
no desktop and therefore no gvfs. See
[`decisions.md` D13](decisions.md#d13-pi-os-lite-specifically).

### Can the Desktop image be used instead?

Yes, but you have to disarm three things, and the reason it fails is not quite
the one usually given.

**It is not "gvfs grabs the camera on enumerate".** The gvfs camera *monitor*
never opens the device — it only decides the body is a camera, by a single test:
does the udev device carry the property `ID_GPHOTO2`. What actually claims the
interface is `gvfsd-gphoto2`, and it runs because **pcmanfm auto-mounts the
volume**: Pi OS ships `/etc/xdg/pcmanfm/default/pcmanfm.conf` with
`mount_on_startup=1`, `mount_removable=1`, `autorun=1`. So the trigger is the
desktop session existing at all, not the camera appearing.

`provision-node.sh` applies three independent mitigations. Any one of them is
sufficient; all three are cheap, and a node is not a place to rely on one.

| Mitigation | What it does |
|---|---|
| `systemctl --global mask gvfs-gphoto2-volume-monitor.service` (and `-mtp-`) | The camera never becomes a volume, so nothing can mount it. |
| udev `ENV{ID_GPHOTO2}=""` for Sony (054c) | gvfs stops recognising the body as a camera at all. libgphoto2 is unaffected — it enumerates through libusb and never reads udev. |
| Boot to console: `sudo raspi-config nonint do_boot_behaviour B2` | No graphical session, so no gvfs at all. This is the clean one; it turns a Desktop install into a Lite-equivalent *runtime* while keeping the tools on disk. |

⚠️ **The scope of that mask is the whole trick, and it is easy to get wrong.**
`sudo systemctl mask gvfs-gphoto2-volume-monitor` — system scope, the incantation
in most forum answers — **does nothing at all.** The unit ships only as
`/usr/lib/systemd/user/...`, has no `[Install]` section, and is started by D-Bus
activation delegated to `systemd --user`. A system-scope mask writes a dangling
symlink into `/etc/systemd/system` that the user manager never reads: the command
succeeds, prints nothing, and the camera still gets claimed. It has to be
`--global`, which writes to `/etc/systemd/user`. `check-node.sh` verifies the
symlink is actually there, and separately checks that `gvfsd-gphoto2` is not
running — because the only claim worth trusting is the one you can observe.

**Should you?** Probably not. The node has no user-facing interface — the
operator GUI is a browser page served from the laptop — so a desktop on the Pi
buys nothing and costs a permanently-running compositor, panel plugins,
PackageKit, CUPS, and roughly a thousand extra packages (632 on Lite against 1640
on Desktop and 1867 on Full). For a measurement instrument, "both nodes run an
identical minimal image" is worth more than any of that. The case for Desktop is
if you want a monitor on the node for on-device debugging; if so, take the
Desktop image *and* set it to boot to console, which gives you the tools without
the session.

**Use Raspberry Pi Imager 2.x.** Current Trixie images are customised through
**cloud-init**, and Imager 1.x does not know that format — it assumes the older
one, so customisation *appears* to apply and then silently does nothing. That is
an hour of wondering why SSH is refused.

Two traps worth naming:

- **`custom.toml` is dead on Trixie.** The `firstboot` script that consumed it
  was removed. If you have a `custom.toml` from an older build, it does nothing.
- The boot partition is **`/boot/firmware/`**, not `/boot/`.

In Imager, set: hostname `scanner-node-0` (and `-1` on the other card), your
username, your SSH **public key** — not a password — and locale. Leave Wi-Fi
blank; the nodes are wired.

If you would rather not trust the GUI, the older mechanisms still work on
Trixie and are independent of cloud-init: drop an empty file named `ssh` and a
`userconf.txt` containing `username:$(openssl passwd -6)` into `/boot/firmware/`.

> The hostname is not cosmetic. `scanner/node/server.py` derives the camera id
> from it, which is what lets both Pis run an identical image. `scanner-node-0`
> → `cam0`, `scanner-node-1` → `cam1`. `check-node.sh` fails if `/status`
> disagrees with the hostname, because a mislabelled node silently swaps left
> and right for the whole book.

---

## 2. First boot

Put the Pi on the segment, power it with the **official 27 W supply**, and find
it — on first boot it is still on DHCP:

```bash
ssh <user>@scanner-node-0.local
```

Then clone the repo and run the script. It clones the repo itself, so the
fastest path is to fetch just the two files:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/cmtunderbird/overhead-scanner
cd overhead-scanner
chmod +x scripts/*.sh
sudo ./scripts/provision-node.sh --role 0 --ip 10.10.0.10/24 --ntp 10.10.0.1
```

`chmod +x` because a file that arrives through the GitHub web API loses its
executable bit. Harmless if it is already set.

`--ntp` is the laptop. The camera segment has no route to the internet and the
**Pi 5 ships with no RTC battery fitted**, so without it every capture gets a
wrong mtime — which matters the first time you try to reconstruct what happened
during an overnight run.

The address is not activated immediately, because doing so would drop the SSH
session you are running the script over. It takes effect at the reboot. Pass
`--activate-network` if you are on the console and want it now.

| Flag | When |
|---|---|
| `--role 0` / `--role 1` | Always. Sets the hostname, so the camera id follows. |
| `--ip A.B.C.D/24` | Always, unless `--no-network`. |
| `--ntp HOST` | Isolated segment. Point it at the laptop. |
| `--no-hailo` | HAT not fitted yet. The node captures fine without it. |
| `--usb-max-current` | Only with the 27 W PSU. Raises downstream USB from 600 mA to 1.6 A. |
| `--no-upgrade` | Skips `apt full-upgrade` and the EEPROM update. Faster; you own the consequences. |

---

## 3. Reboot, then verify

```bash
sudo reboot
# then, once it is back:
sudo ./scripts/check-node.sh
```

The reboot is not optional: the Hailo driver, the EEPROM update, the `plugdev`
membership and the static address all take effect at boot.

`check-node.sh` exits non-zero if any invariant failed, so it can gate a script.
Warnings do not fail the run — they are the things that are usually fine and
occasionally the answer.

The two checks worth understanding before you see them fail:

**`/status backend=` must say `gphoto2`.** If `import gphoto2` fails inside the
venv, `build_camera()`'s `auto` mode falls back to the **mock** backend, and the
node serves synthetic pages that look entirely plausible. Every measurement
taken through it is fiction. This is why the unit sets `SCANNER_BACKEND=gphoto2`
explicitly rather than relying on `auto`, and why the check is a FAIL and not a
warning.

**The service user must be in `plugdev`.** systemd's `70-uaccess.rules` already
tags PTP devices, but `uaccess` grants an ACL only to a user with an *active
local seat*. Over SSH there is no seat, so the ACL is never applied. The group
is the only thing standing between you and *Could not claim the USB device* —
and because the symptom is identical to the gvfs one, it is easy to spend an
evening on the wrong cause.

---

## 4. The camera

On the body, before the node will capture anything — these are not optional,
capture hangs without them:

| Setting | Value | Why |
|---|---|---|
| USB Connection | **PC Remote** | Otherwise it enumerates as mass storage and gphoto2 sees nothing |
| Mode dial | **M** | Sony refuses aperture/shutter over PTP in any other mode |
| Auto Review | **Off** | Blocks the next command while it shows you the shot |
| Pre-AF | **Off** | Hunts between frames and moves your focus |
| Focus | **DMF** | Manual with magnification, which is what you want |

Use the camera's **DATA** port, not the charge-only one. Then:

```bash
gphoto2 --auto-detect          # on the node
curl http://10.10.0.10:8000/status | python3 -m json.tool
```

`"connected": true` with a real model string means the transport is done.

---

## 5. From the laptop

```bash
python -m scanner gui --node cam0=http://10.10.0.10:8000 \
                      --node cam1=http://10.10.0.11:8000
```

Both addressing routes work by design: the static IP is deterministic, and
`scanner-node-0.local` is the fallback for when you have moved the rig and
cannot remember the subnet.

⚠️ **Do not depend on mDNS from Windows.** Windows 11 does usually resolve
`.local` without Bonjour, but Microsoft publish no guarantee of it. Put the two
static addresses in `C:\Windows\System32\drivers\etc\hosts` and the question
never comes up.

---

## What the script does, and why

Roughly in order. The reasoning matters more than the commands, because the
commands will drift.

**Hostname before anything else.** It is where the camera id comes from. The
script also fixes the `127.0.1.1` line in `/etc/hosts`, without which every
`sudo` stalls for a few seconds.

**EEPROM and full-upgrade first.** Raspberry Pi document the firmware update as
a prerequisite for the AI HAT+, and a kernel that moves *after* the Hailo DKMS
module is built leaves you with no `/dev/hailo0`.

**Static IP with no gateway and no DNS.** Both are deliberate. A gateway address
that never answers ARP is the usual cause of *it worked, then it stopped*; a
nameserver that never answers puts a multi-second stall in front of every
`getaddrinfo()`, which then reads as a slow node. IPv6 is disabled so the
interface does not sit waiting for a router advertisement that will never come.
`NetworkManager-wait-online` is disabled because on this segment it only adds a
six-second boot stall.

The stock DHCP profile is kept, with autoconnect off, as a rescue path: from the
console, `nmcli con up "Wired connection 1"` puts the Pi back on the house LAN.

**Hailo: the minimal package set, not `hailo-all`.**

```bash
sudo apt install -y dkms hailort hailort-pcie-driver python3-hailort
```

`hailo-all` additionally pulls `hailo-tappas-core` and
`rpicam-apps-hailo-postprocess` — GStreamer, OpenCV-dev, VTK, LLVM, ~840 extra
packages and ~2.4 GB — all of it to support CSI-camera demos this node will
never run. Neither route installs a desktop or gvfs, so the minimal one costs
nothing but disk you get to keep.

**No `config.txt` edits.** The AI HAT+ is a true HAT+, so the PCIe connector
enables itself and Gen 3 is applied automatically. `dtparam=pciex1` and
`dtparam=pciex1_gen=3` are for the M.2-based AI Kit. Raspberry Pi's own docs say
to skip them for the HAT+; adding them is harmless but signals a
misunderstanding.

**A venv with `--system-site-packages`.** Not a style choice.
`python3-hailort` installs `hailo_platform` into `/usr/lib/python3/dist-packages`
and publishes no wheel, so an isolated venv cannot see the NPU at all. The venv
must also be built from the system Python 3.13 — the binding is ABI-tagged
`cpython-313` and will not load under a pyenv 3.12 or 3.14.

**`--only-binary :all:`** on every pip install, so a missing aarch64 wheel is an
immediate error rather than a source build that fails twenty minutes later for
want of `python3-dev`.

**[`requirements-node.txt`](../requirements-node.txt), not `requirements.txt`.**
Two differences that both bite on Lite: `opencv-contrib-python` links
`libGL.so.1`, which a Lite image does not have, so `import cv2` dies — the
`-headless` build is what you want, and the node only encodes JPEGs, resizes
previews and runs a Laplacian. And plain `uvicorn` instead of `uvicorn[standard]`,
which drops three compiled wheels in exchange for a reload watcher an appliance
must never use. The bottleneck is USB 2.0 PTP at 10–15 MB/s, not the event loop.

**Persistent, capped journal.** Pi OS ships `Storage=volatile`, so by default
every log is discarded at reboot — precisely the logs you want after an
unattended overnight run. Capped at 200 MB because this is an SD card and write
endurance is the real constraint.

**`After=network.target`, not `network-online.target`.** uvicorn binding
`0.0.0.0` does not need an address to exist first, and waiting for one only adds
a boot stall on an isolated segment.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Could not claim the USB device` | Service user not in `plugdev` — `uaccess` does nothing over SSH. Or, on a Desktop image, `gvfsd-gphoto2` holding the interface after pcmanfm auto-mounted the body. `pgrep -a gvfsd-gphoto2` tells you which. |
| Masked gvfs, still claimed | The mask was at system scope, which is a no-op for a user unit. It must be `systemctl --global mask`. |
| `/status` shows `backend: mock` | `import gphoto2` is failing in the venv. **The node is serving synthetic pages.** |
| `ImportError: libGL.so.1` | Desktop OpenCV on a Lite image. Install `opencv-contrib-python-headless`. |
| `error: externally-managed-environment` | PEP 668. Use the venv; do not reach for `--break-system-packages`. |
| `/dev/hailo0` missing after a kernel upgrade | DKMS rebuild failed. `dkms status \| grep hailo`, then `/var/log/hailort-pcie-driver.deb.log`. |
| `Driver version … is different from library version` | `hailort`, `hailort-pcie-driver` and `python3-hailort` drifted apart. `apt full-upgrade` so all three move together; never install or hold one alone. |
| Node unreachable after reboot | Static profile did not come up. Console in and `nmcli con up "Wired connection 1"`. |
| Service restart-looping | `journalctl -u scanner-node -n 50`. Usually a venv import. |
| Under-voltage / throttling flags | PSU. Two USB-tethered bodies on a non-27 W supply caps downstream USB at 600 mA. |
| `hailortcli` shows `N/A` for serial/part number | Expected on the AI HAT+. Not a fault. |

`check-node.sh -v` prints the command output behind each check.

---

## Assembly order

From [`hardware.md`](hardware.md), and worth repeating because it is the part
people skip: **move to the Pi nodes last.** Mount one camera, set the lens, tape
the rings, calibrate, shoot a test page and measure DPI and MTF50 with the camera
plugged into the laptop. Only when that measures correctly does the node become
a transport question rather than a confounding variable.

## The one thing not to skip

**Run `check-node.sh` after every reboot and every kernel upgrade.** The two
failures that cost you a whole book are silent by construction: a DKMS module
that stopped building, and a node that quietly fell back to the mock backend and
served you 200 spreads of synthetic paper. Neither announces itself, and both are
one command away from being caught.
