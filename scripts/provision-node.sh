#!/usr/bin/env bash
#
# provision-node.sh -- turn a fresh Raspberry Pi 5 into a scanner camera node.
#
# Run once per Pi, as root, over SSH or from the console:
#
#     sudo ./scripts/provision-node.sh --role 0 --ip 10.10.0.10/24
#     sudo ./scripts/provision-node.sh --role 1 --ip 10.10.0.11/24
#
# It is idempotent: re-running it is the supported way to repair a node or
# pick up a change.  Every step is skipped if it is already in the desired
# state, and every step prints what it did.
#
# Target: Raspberry Pi 5, Raspberry Pi OS **Lite** 64-bit (Debian 13 Trixie).
# Bookworm is not supported -- the Hailo package names differ and Raspberry Pi
# do not support an in-place Bookworm -> Trixie upgrade.  Reflash.
#
# See docs/node-setup.md for the reasoning behind each step.

set -Eeuo pipefail

# ---------------------------------------------------------------- defaults --

ROLE=""
NODE_IP=""
IFACE="eth0"
CON_NAME="scanner-lan"
NODE_USER=""
REPO_URL="https://github.com/cmtunderbird/overhead-scanner"
REPO_DIR=""
PORT="8000"
NTP_SERVER=""
DO_UPGRADE=1
DO_HAILO=1
DO_NETWORK=1
DO_USB_MAX_CURRENT=0
ACTIVATE_NETWORK=0
JOURNAL_MAX="200M"

readonly SCRIPT_NAME="${0##*/}"

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/provision-node.sh --role <0|1> [options]

Required:
  --role N              0 or 1.  Sets the hostname to scanner-node-N, which is
                        where the node server gets its camera id from, so both
                        Pis run an identical image.

Network (default: configure a static profile, activate on next boot):
  --ip A.B.C.D/PREFIX   Static address for the camera LAN, e.g. 10.10.0.10/24.
  --iface NAME          Interface (default: eth0).
  --activate-network    Bring the profile up immediately.  THIS WILL DROP YOUR
                        SSH SESSION if you are connected over --iface.  Without
                        it the profile is written and takes effect on reboot.
  --no-network          Skip all network configuration.

Software:
  --user NAME           Account to own and run the node (default: the user who
                        invoked sudo, else the first non-system user).
  --repo-dir PATH       Checkout location (default: ~USER/overhead-scanner).
  --repo-url URL        Clone source (default: the upstream GitHub repo).
  --port N              Node HTTP port (default: 8000).
  --no-upgrade          Skip apt full-upgrade and the EEPROM update.  Faster,
                        but you are then responsible for firmware currency.
  --no-hailo            Skip the Hailo AI HAT+ stack.  Use if the HAT is not
                        fitted; the node captures fine without it.

Extras:
  --ntp HOST            Point systemd-timesyncd at HOST (the laptop) for an
                        isolated segment with no route to the internet.
  --usb-max-current     Set usb_max_current_enable=1 (1.6 A downstream instead
                        of 600 mA).  ONLY with the official 27 W PSU.
  --journal-max SIZE    Journal disk cap (default: 200M).
  -h, --help            This.
EOF
}

# ------------------------------------------------------------------- output --

if [[ -t 1 ]]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_STEP=$'\033[1;36m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""; C_DIM=""; C_OFF=""
fi

STEP_N=0
CHANGED=()
WARNINGS=()

step()    { STEP_N=$((STEP_N + 1)); printf '\n%s[%d] %s%s\n' "$C_STEP" "$STEP_N" "$*" "$C_OFF"; }
ok()      { printf '    %s+%s %s\n' "$C_OK" "$C_OFF" "$*"; }
same()    { printf '    %s= %s%s\n' "$C_DIM" "$*" "$C_OFF"; }
changed() { CHANGED+=("$*"); ok "$*"; }
warn()    { WARNINGS+=("$*"); printf '    %s! %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
die()     { printf '\n%serror:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

on_err() {
    local rc=$? line=$1
    printf '\n%sfailed%s at line %s (exit %s).  The script is idempotent -- fix the\n' \
           "$C_ERR" "$C_OFF" "$line" "$rc" >&2
    printf 'cause and run it again; completed steps will be skipped.\n' >&2
}
trap 'on_err $LINENO' ERR

# ------------------------------------------------------------------- parsing --

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)             ROLE="${2:?}"; shift 2 ;;
        --ip)               NODE_IP="${2:?}"; shift 2 ;;
        --iface)            IFACE="${2:?}"; shift 2 ;;
        --user)             NODE_USER="${2:?}"; shift 2 ;;
        --repo-dir)         REPO_DIR="${2:?}"; shift 2 ;;
        --repo-url)         REPO_URL="${2:?}"; shift 2 ;;
        --port)             PORT="${2:?}"; shift 2 ;;
        --ntp)              NTP_SERVER="${2:?}"; shift 2 ;;
        --journal-max)      JOURNAL_MAX="${2:?}"; shift 2 ;;
        --activate-network) ACTIVATE_NETWORK=1; shift ;;
        --no-network)       DO_NETWORK=0; shift ;;
        --no-upgrade)       DO_UPGRADE=0; shift ;;
        --no-hailo)         DO_HAILO=0; shift ;;
        --usb-max-current)  DO_USB_MAX_CURRENT=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)                  usage >&2; die "unknown argument: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must run as root: sudo ./scripts/$SCRIPT_NAME ..."
[[ "$ROLE" == "0" || "$ROLE" == "1" ]] || { usage >&2; die "--role must be 0 or 1"; }

if [[ $DO_NETWORK -eq 1 && -z "$NODE_IP" ]]; then
    die "--ip is required unless you pass --no-network"
fi
if [[ -n "$NODE_IP" && "$NODE_IP" != */* ]]; then
    die "--ip must include a prefix length, e.g. 10.10.0.10/24"
fi

HOSTNAME_WANT="scanner-node-${ROLE}"
CAMERA_ID="cam${ROLE}"

# Resolve the account that will own the checkout and run the service.
if [[ -z "$NODE_USER" ]]; then
    NODE_USER="${SUDO_USER:-}"
fi
if [[ -z "$NODE_USER" || "$NODE_USER" == "root" ]]; then
    NODE_USER="$(awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}' /etc/passwd)"
fi
[[ -n "$NODE_USER" ]] || die "could not determine a non-root user; pass --user"
id "$NODE_USER" >/dev/null 2>&1 || die "user '$NODE_USER' does not exist"

NODE_HOME="$(getent passwd "$NODE_USER" 2>/dev/null | cut -d: -f6 || true)"
[[ -n "$NODE_HOME" && -d "$NODE_HOME" ]] || die "no home directory for '$NODE_USER'"
[[ -n "$REPO_DIR" ]] || REPO_DIR="${NODE_HOME}/overhead-scanner"
VENV_DIR="${REPO_DIR}/venv"

as_user() { runuser -u "$NODE_USER" -- "$@"; }

# Write $2 to path $1 only if the content differs.  Returns 0 if it wrote.
write_if_changed() {
    local path="$1" content="$2" mode="${3:-0644}"
    if [[ -f "$path" ]] && printf '%s' "$content" | cmp -s - "$path"; then
        return 1
    fi
    install -d -m 0755 "$(dirname "$path")"
    printf '%s' "$content" > "$path"
    chmod "$mode" "$path"
    return 0
}

apt_install() {
    local missing=()
    for pkg in "$@"; do
        dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null | grep -q '^installed$' \
            || missing+=("$pkg")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        same "already installed: $*"
        return 0
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    changed "installed: ${missing[*]}"
}

pkg_exists() { apt-cache show "$1" >/dev/null 2>&1; }

printf '%s%s%s  role=%s host=%s user=%s\n' \
       "$C_STEP" "overhead-scanner node provisioning" "$C_OFF" \
       "$ROLE" "$HOSTNAME_WANT" "$NODE_USER"

# ------------------------------------------------------------ 1. sanity ------

step "Check the machine is what this script expects"

# The `|| true` matters: under `set -e` a command substitution that exits
# non-zero aborts the script, so a missing file here would kill the run with a
# line number instead of the warning immediately below.
MODEL="$({ tr -d '\0' < /proc/device-tree/model; } 2>/dev/null || true)"
[[ -n "$MODEL" ]] || MODEL="unknown"
case "$MODEL" in
    *"Raspberry Pi 5"*) ok "model: $MODEL" ;;
    *) warn "model is '$MODEL', not a Raspberry Pi 5 -- continuing, but the Hailo and PCIe steps assume Pi 5" ;;
esac

ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
[[ "$ARCH" == "arm64" ]] || die "architecture is '$ARCH'; this needs the 64-bit OS (arm64)"
ok "architecture: arm64"

OS_CODENAME="unknown"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-unknown}")"
fi
case "$OS_CODENAME" in
    trixie) ok "OS: Debian 13 (trixie)" ;;
    bookworm)
        die "this is Bookworm.  The Hailo driver package was renamed (hailo-dkms ->
       hailort-pcie-driver) and Raspberry Pi do not support an in-place
       upgrade.  Reflash with Raspberry Pi OS Lite 64-bit (Trixie)." ;;
    *) warn "unrecognised OS codename '$OS_CODENAME'; package names may differ" ;;
esac

if command -v gvfsd >/dev/null 2>&1 || dpkg-query -W -f='${db:Status-Status}' gvfs 2>/dev/null | grep -q installed; then
    warn "gvfs is installed -- this is a desktop image, not Lite.  gvfs-gphoto2-volume-monitor
      claims the camera the instant it enumerates.  Step 7 masks it, but Lite is
      the supported base."
else
    ok "no gvfs (Lite image, as specified)"
fi

if [[ -e /boot/firmware/config.txt ]]; then
    ok "boot partition at /boot/firmware"
else
    warn "/boot/firmware/config.txt not found -- unusual for a current image"
fi

# ------------------------------------------------------------ 2. hostname ----

step "Set the hostname (this is where the camera id comes from)"

CURRENT_HOST="$(hostname)"
if [[ "$CURRENT_HOST" == "$HOSTNAME_WANT" ]]; then
    same "hostname already $HOSTNAME_WANT"
else
    hostnamectl set-hostname "$HOSTNAME_WANT"
    changed "hostname $CURRENT_HOST -> $HOSTNAME_WANT"
fi

# 127.0.1.1 must track the hostname or sudo stalls on every invocation.
if grep -qE "^127\.0\.1\.1[[:space:]]+${HOSTNAME_WANT}\b" /etc/hosts; then
    same "/etc/hosts 127.0.1.1 entry correct"
else
    if grep -qE '^127\.0\.1\.1' /etc/hosts; then
        sed -i -E "s/^127\.0\.1\.1.*/127.0.1.1\t${HOSTNAME_WANT}/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$HOSTNAME_WANT" >> /etc/hosts
    fi
    changed "/etc/hosts 127.0.1.1 -> $HOSTNAME_WANT"
fi

ok "node server will resolve camera id: $CAMERA_ID"

# ------------------------------------------------------- 3. firmware/apt -----

step "Update the package index, the OS and the bootloader EEPROM"

apt-get update -qq
ok "apt index updated"

if [[ $DO_UPGRADE -eq 1 ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y
    changed "apt full-upgrade"
    if command -v rpi-eeprom-update >/dev/null 2>&1; then
        # -a returns non-zero when there is nothing to do on some versions.
        if rpi-eeprom-update -a; then
            ok "EEPROM checked/updated (takes effect at next boot)"
        else
            same "EEPROM already current"
        fi
    else
        warn "rpi-eeprom-update not present; skipping firmware update"
    fi
else
    same "skipped (--no-upgrade)"
fi

# ---------------------------------------------------------- 4. base packages -

step "Install base packages"

apt_install git python3-venv ca-certificates curl avahi-daemon

# The runtime library is wanted for one reason above all: its udev rules.
# The PyPI gphoto2 wheel bundles the library itself but ships no rules, and
# without them the device node stays root:root.  Debian's 64-bit time_t
# transition renamed these, so try the new name first.
if pkg_exists libgphoto2-6t64; then
    apt_install libgphoto2-6t64
elif pkg_exists libgphoto2-6; then
    apt_install libgphoto2-6
else
    warn "no libgphoto2 runtime package found; camera udev rules will come only
      from the local rule written in step 7"
fi

# The CLI is not used at runtime (python-gphoto2 bindings are), but
# `gphoto2 --auto-detect` is the fastest way to prove the USB path works.
apt_install gphoto2

systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
if systemctl is-active --quiet avahi-daemon; then
    ok "avahi-daemon running -- ${HOSTNAME_WANT}.local will resolve"
else
    warn "avahi-daemon is not active; mDNS names will not resolve"
fi

# ------------------------------------------------------------- 5. network ----

step "Configure the camera LAN"

if [[ $DO_NETWORK -eq 0 ]]; then
    same "skipped (--no-network)"
elif ! command -v nmcli >/dev/null 2>&1; then
    warn "nmcli not found -- NetworkManager is expected on current Pi OS.  Skipping."
else
    if ! nmcli -t -f DEVICE device status | grep -qx "$IFACE"; then
        warn "interface '$IFACE' not present.  Profile will still be written and will
      bind when the interface appears."
    fi

    # Deleting and re-adding is the only reliable way to make this idempotent:
    # `nmcli con mod` leaves stale properties behind from a previous run.
    if nmcli -t -f NAME connection show | grep -qx "$CON_NAME"; then
        nmcli connection delete "$CON_NAME" >/dev/null
        same "removed previous '$CON_NAME' profile"
    fi

    # No gateway and no DNS on purpose.  A gateway address that never answers
    # ARP is the usual cause of "it worked, then it stopped"; a nameserver that
    # never answers puts a multi-second stall in front of every getaddrinfo().
    # ipv6 disabled so the interface does not sit waiting for a router
    # advertisement that will never come.
    nmcli connection add type ethernet ifname "$IFACE" con-name "$CON_NAME" \
        ipv4.method manual \
        ipv4.addresses "$NODE_IP" \
        ipv4.never-default yes \
        ipv4.ignore-auto-dns yes \
        ipv4.may-fail no \
        ipv6.method disabled \
        connection.autoconnect yes \
        connection.autoconnect-priority 100 >/dev/null
    changed "profile '$CON_NAME': $NODE_IP on $IFACE, no gateway, no default route"

    # Keep the stock DHCP profile as a manual rescue path rather than deleting
    # it: if the static segment is ever wrong you can still get on the house
    # LAN from the console with `nmcli con up "Wired connection 1"`.
    while IFS= read -r name; do
        [[ -n "$name" && "$name" != "$CON_NAME" ]] || continue
        nmcli connection modify "$name" connection.autoconnect no >/dev/null 2>&1 || true
        same "'$name' kept as a manual fallback (autoconnect off)"
    done < <(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1}')

    if [[ $ACTIVATE_NETWORK -eq 1 ]]; then
        warn "activating now -- if you are connected over $IFACE this session will drop"
        nmcli connection up "$CON_NAME" >/dev/null || warn "activation failed; it will retry at boot"
    else
        ok "not activated; takes effect at next boot (use --activate-network to force)"
    fi

    # NetworkManager-wait-online blocks boot for carrier-wait-timeout (6 s
    # default) and buys an appliance nothing.
    if systemctl is-enabled --quiet NetworkManager-wait-online.service 2>/dev/null; then
        systemctl disable NetworkManager-wait-online.service >/dev/null 2>&1 || true
        changed "disabled NetworkManager-wait-online (removes a boot stall)"
    else
        same "NetworkManager-wait-online already disabled"
    fi
fi

# ------------------------------------------------------------- 6. Hailo ------

step "Hailo AI HAT+ (26 TOPS, Hailo-8)"

if [[ $DO_HAILO -eq 0 ]]; then
    same "skipped (--no-hailo)"
elif ! pkg_exists hailort-pcie-driver; then
    warn "hailort-pcie-driver not in the archive.  Is this Raspberry Pi OS with the
      raspberrypi.com apt repository enabled?  Skipping the Hailo stack."
else
    # dkms is deliberately first and separate: hailort-pcie-driver builds the
    # kernel module in its postinst but does NOT depend on dkms, so installing
    # them together can order badly.
    apt_install dkms
    # Minimal set on purpose.  `hailo-all` additionally pulls hailo-tappas-core
    # and rpicam-apps-hailo-postprocess -- GStreamer, OpenCV-dev, VTK, LLVM,
    # ~840 extra packages and ~2.4 GB -- all of it for CSI-camera demos this
    # node will never run.  It installs no desktop and no gvfs either way.
    apt_install hailort hailort-pcie-driver python3-hailort

    if [[ -e /dev/hailo0 ]]; then
        ok "/dev/hailo0 present"
    else
        warn "/dev/hailo0 not present yet -- expected before the first reboot after
      installing the driver.  check-node.sh will verify it afterwards."
    fi

    # No config.txt edits: the AI HAT+ is a true HAT+, so the PCIe connector
    # auto-enables and Gen 3 is applied automatically.  dtparam=pciex1 and
    # dtparam=pciex1_gen=3 are for the M.2-based AI Kit, not this board.
    if grep -qE '^\s*dtparam=pciex1' /boot/firmware/config.txt 2>/dev/null; then
        warn "config.txt contains a dtparam=pciex1* line.  Harmless, but unnecessary
      for the AI HAT+ and not what Raspberry Pi document."
    else
        ok "no PCIe dtparam needed (HAT+ auto-enables the connector and Gen 3)"
    fi
fi

# --------------------------------------------------------- 7. camera access --

step "USB camera access"

# Why a rule at all, when systemd's 70-uaccess.rules already tags PTP devices:
# uaccess grants an ACL to a user with an *active local seat*.  Over SSH there
# is no seat, so the ACL is never applied.  That is the real mechanism behind
# "Could not claim the USB device" on a headless Pi, and the group is the fix.
UDEV_RULE_PATH="/etc/udev/rules.d/95-scanner-camera.rules"
read -r -d '' UDEV_RULE <<'EOF' || true
# overhead-scanner: headless access to a USB PTP camera.
#
# 060101 is the USB Still Image / PTP interface triple -- a Sony A6000 with
# "USB Connection -> PC Remote" matches it.  libgphoto2 ships an equivalent
# rule; this one is here so the node still works if that package is absent,
# and so the intent is visible in the repo.
#
# 95- so it runs after systemd's 70-uaccess.rules.
SUBSYSTEM=="usb", ENV{ID_USB_INTERFACES}=="*:060101:*", MODE="0660", GROUP="plugdev", TAG+="uaccess"

# Sony (054c), belt and braces.
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", MODE="0660", GROUP="plugdev"

# Stop libmtp's hwdb from tagging the body as a media player.  Nothing on Lite
# acts on the tag, but it costs nothing to keep the device unclaimed.
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", ENV{ID_MTP_DEVICE}="", ENV{ID_MEDIA_PLAYER}=""
EOF

if write_if_changed "$UDEV_RULE_PATH" "$UDEV_RULE"; then
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=usb
    changed "udev rule $UDEV_RULE_PATH (reloaded)"
else
    same "udev rule already current"
fi

if id -nG "$NODE_USER" | tr ' ' '\n' | grep -qx plugdev; then
    same "$NODE_USER already in plugdev"
else
    usermod -aG plugdev "$NODE_USER"
    changed "added $NODE_USER to plugdev (takes effect on next login / service restart)"
fi

# Defensive: nothing on Lite claims a PTP camera, but if someone later
# apt-installs a desktop fragment these are what would.
for unit in gvfs-gphoto2-volume-monitor.service gvfs-mtp-volume-monitor.service ModemManager.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 && \
       systemctl list-unit-files --no-legend "$unit" 2>/dev/null | grep -q .; then
        if systemctl mask "$unit" >/dev/null 2>&1; then
            changed "masked $unit"
        fi
    fi
done

if [[ $DO_USB_MAX_CURRENT -eq 1 ]]; then
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_usb_current 0 || warn "do_usb_current failed"
        changed "usb_max_current_enable=1 (1.6 A downstream) -- requires the 27 W PSU"
    else
        warn "raspi-config not found; cannot set usb_max_current_enable"
    fi
else
    same "usb_max_current left at default (600 mA downstream; --usb-max-current to raise)"
fi

# ------------------------------------------------------------ 8. checkout ----

step "Check out the repository"

if [[ -d "$REPO_DIR/.git" ]]; then
    as_user git -C "$REPO_DIR" fetch --quiet origin
    as_user git -C "$REPO_DIR" pull --quiet --ff-only || \
        warn "could not fast-forward $REPO_DIR (local changes?); leaving it alone"
    same "updated $REPO_DIR"
else
    install -d -o "$NODE_USER" -g "$NODE_USER" "$(dirname "$REPO_DIR")"
    as_user git clone --quiet "$REPO_URL" "$REPO_DIR"
    changed "cloned $REPO_URL -> $REPO_DIR"
fi

# ---------------------------------------------------------------- 9. venv ----

step "Python environment"

# --system-site-packages is not optional: python3-hailort installs
# hailo_platform into /usr/lib/python3/dist-packages and there is no wheel for
# it, so an isolated venv cannot see the NPU at all.
if [[ -f "$VENV_DIR/pyvenv.cfg" ]] && grep -q 'include-system-site-packages = true' "$VENV_DIR/pyvenv.cfg"; then
    same "venv exists with system site-packages"
else
    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        warn "existing venv lacked --system-site-packages; rebuilt"
    fi
    as_user python3 -m venv --system-site-packages "$VENV_DIR"
    changed "created $VENV_DIR (--system-site-packages)"
fi

as_user "$VENV_DIR/bin/pip" install --quiet --upgrade pip wheel
ok "pip up to date"

NODE_REQS="$REPO_DIR/requirements-node.txt"
if [[ -f "$NODE_REQS" ]]; then
    REQ_ARGS=(-r "$NODE_REQS")
else
    warn "requirements-node.txt missing; falling back to requirements.txt (installs
      the full pipeline stack, including desktop OpenCV)"
    REQ_ARGS=(-r "$REPO_DIR/requirements.txt")
fi

# --only-binary :all: makes a missing aarch64 wheel a loud failure instead of a
# silent 40-minute source build that then fails for want of python3-dev.
as_user "$VENV_DIR/bin/pip" install --only-binary :all: "${REQ_ARGS[@]}"
changed "installed node requirements"

as_user "$VENV_DIR/bin/pip" install --only-binary :all: 'gphoto2>=2.5'
changed "installed python-gphoto2 (aarch64 wheel bundles libgphoto2 and ptp2.so)"

if as_user "$VENV_DIR/bin/python" -c 'import cv2, fastapi, uvicorn, gphoto2' 2>/dev/null; then
    ok "imports clean: cv2, fastapi, uvicorn, gphoto2"
else
    as_user "$VENV_DIR/bin/python" -c 'import cv2, fastapi, uvicorn, gphoto2' || true
    die "the venv cannot import its own dependencies -- see the traceback above"
fi

# ------------------------------------------------------------ 10. service ----

step "Install the node service"

UNIT_PATH="/etc/systemd/system/scanner-node.service"
UNIT_CONTENT="$(cat <<EOF
[Unit]
Description=Overhead scanner camera node (${CAMERA_ID})
Documentation=https://github.com/cmtunderbird/overhead-scanner/blob/main/docs/node-setup.md
# Deliberately network.target, not network-online.target: on an isolated
# segment wait-online only adds a boot stall, and uvicorn binding 0.0.0.0
# does not need the address to be up first.
After=network.target
Wants=network.target

[Service]
Type=simple
User=${NODE_USER}
Group=${NODE_USER}
SupplementaryGroups=plugdev
WorkingDirectory=${REPO_DIR}
Environment=SCANNER_BACKEND=gphoto2
Environment=SCANNER_CAMERA_ID=${CAMERA_ID}
Environment=SCANNER_TILE=${ROLE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python -m uvicorn scanner.node.server:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
# The A6000 drops its PTP session when idle and the backend reconnects; do not
# let a burst of restarts trip the default start limit.
StartLimitIntervalSec=0

NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=no
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
)"

if write_if_changed "$UNIT_PATH" "$UNIT_CONTENT"; then
    systemctl daemon-reload
    changed "wrote $UNIT_PATH"
else
    same "unit already current"
fi

systemctl enable scanner-node.service >/dev/null 2>&1
systemctl restart scanner-node.service
sleep 2
if systemctl is-active --quiet scanner-node.service; then
    ok "scanner-node.service enabled and running on port $PORT"
else
    warn "scanner-node.service is not active -- journalctl -u scanner-node -n 50"
fi

# --------------------------------------------------------- 11. housekeeping --

step "Logging, time and capture staging"

# Pi OS ships Storage=volatile, so without this every log is lost at reboot --
# exactly the logs you want after an unattended overnight run.  A drop-in with
# a higher number than Raspberry Pi's 40- file is what wins.
JOURNAL_CONF="/etc/systemd/journald.conf.d/90-scanner.conf"
JOURNAL_CONTENT="$(cat <<EOF
# overhead-scanner: Pi OS defaults to Storage=volatile (see
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf), which loses
# the journal at every reboot.  Persist it, but cap it hard -- this is an SD
# card and write endurance is the constraint.
[Journal]
Storage=persistent
SystemMaxUse=${JOURNAL_MAX}
SystemMaxFileSize=25M
SystemKeepFree=500M
EOF
)"
if write_if_changed "$JOURNAL_CONF" "$JOURNAL_CONTENT"; then
    systemctl restart systemd-journald
    changed "persistent journal, capped at $JOURNAL_MAX"
else
    same "journald drop-in already current"
fi

if [[ -n "$NTP_SERVER" ]]; then
    TIMESYNC_CONF="/etc/systemd/timesyncd.conf.d/50-scanner.conf"
    TIMESYNC_CONTENT="$(cat <<EOF
# The camera segment has no route to the internet, and the Pi 5 ships without
# an RTC battery.  Take time from the orchestrator laptop instead.
[Time]
NTP=${NTP_SERVER}
FallbackNTP=
EOF
)"
    if write_if_changed "$TIMESYNC_CONF" "$TIMESYNC_CONTENT"; then
        systemctl restart systemd-timesyncd || true
        changed "timesyncd pointed at $NTP_SERVER"
    else
        same "timesyncd drop-in already current"
    fi
else
    same "no --ntp given; leaving time sync at defaults"
fi

STAGING="${NODE_HOME}/captures"
if [[ -d "$STAGING" ]]; then
    same "staging directory $STAGING"
else
    install -d -o "$NODE_USER" -g "$NODE_USER" -m 0755 "$STAGING"
    changed "created staging directory $STAGING"
fi

# ------------------------------------------------------------- 12. summary ---

step "Summary"

if [[ ${#CHANGED[@]} -eq 0 ]]; then
    ok "nothing to change -- node was already provisioned"
else
    printf '    %d change(s) applied.\n' "${#CHANGED[@]}"
fi

if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    printf '\n%s%d warning(s):%s\n' "$C_WARN" "${#WARNINGS[@]}" "$C_OFF"
    for w in "${WARNINGS[@]}"; do printf '    - %s\n' "${w%%$'\n'*}"; done
fi

cat <<EOF

Next:
  1. sudo reboot
       Required: the Hailo driver, the EEPROM update, the plugdev membership
       and the static address all take effect at boot.
  2. sudo ./scripts/check-node.sh
       Verifies every invariant this script established, and fails loudly on
       any that did not survive the reboot.
  3. From the laptop:
       curl http://${NODE_IP%%/*}:${PORT}/status
       curl http://${HOSTNAME_WANT}.local:${PORT}/status

Camera body settings, before the node will capture:
  USB Connection -> PC Remote | Mode dial -> M | Auto Review -> Off
  Pre-AF -> Off | Focus -> DMF
EOF
