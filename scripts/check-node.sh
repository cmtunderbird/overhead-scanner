#!/usr/bin/env bash
#
# check-node.sh -- prove a Raspberry Pi 5 node is ready, or say exactly why not.
#
#     sudo ./scripts/check-node.sh
#
# Every check is an invariant provision-node.sh established.  The point of a
# separate script is that provisioning and verification must not share code:
# a provisioning step that silently did nothing still reports success, and only
# an independent observer catches it.  Run this after every reboot, after every
# kernel upgrade, and before believing any measurement taken through a node.
#
# Exit status: 0 if every check passed, 1 if any FAILed.  WARNs do not fail the
# run -- they are things that are usually fine and occasionally the answer.

set -uo pipefail   # deliberately NOT -e: a failing check must not stop the run

PORT="${SCANNER_PORT:-8000}"
IFACE="${SCANNER_IFACE:-eth0}"
NODE_USER=""
VERBOSE=0

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/check-node.sh [options]

  --port N     Node HTTP port (default: 8000, or $SCANNER_PORT)
  --iface NAME Camera LAN interface (default: eth0, or $SCANNER_IFACE)
  --user NAME  Account the node runs as (default: inferred from the service)
  -v           Print the command output behind each check
  -h, --help   This
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)  PORT="${2:?}"; shift 2 ;;
        --iface) IFACE="${2:?}"; shift 2 ;;
        --user)  NODE_USER="${2:?}"; shift 2 ;;
        -v)      VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [[ -t 1 ]]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_HEAD=$'\033[1;36m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_HEAD=""; C_DIM=""; C_OFF=""
fi

N_PASS=0; N_FAIL=0; N_WARN=0; N_SKIP=0
FAILED=()

head_()  { printf '\n%s-- %s%s\n' "$C_HEAD" "$*" "$C_OFF"; }
pass()   { N_PASS=$((N_PASS+1)); printf '  %sPASS%s  %s\n' "$C_OK" "$C_OFF" "$*"; }
fail()   { N_FAIL=$((N_FAIL+1)); FAILED+=("$1"); printf '  %sFAIL%s  %s\n' "$C_ERR" "$C_OFF" "$*"; }
warn()   { N_WARN=$((N_WARN+1)); printf '  %sWARN%s  %s\n' "$C_WARN" "$C_OFF" "$*"; }
skip()   { N_SKIP=$((N_SKIP+1)); printf '  %sSKIP  %s%s\n' "$C_DIM" "$*" "$C_OFF"; }
detail() { [[ $VERBOSE -eq 1 ]] && printf '        %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; return 0; }
note()   { printf '        %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }

have() { command -v "$1" >/dev/null 2>&1; }

printf '%soverhead-scanner node check%s  %s\n' "$C_HEAD" "$C_OFF" "$(date -Is 2>/dev/null || true)"
[[ $EUID -eq 0 ]] || warn "not running as root; some checks will be limited"

# ------------------------------------------------------------- platform ------

head_ "Platform"

# The redirect must be inside the subshell that can fail, or a missing file
# leaks "No such file or directory" to stderr before `|| echo` ever runs.
MODEL="$({ tr -d '\0' < /proc/device-tree/model; } 2>/dev/null)"
[[ -n "$MODEL" ]] || MODEL="unknown"
case "$MODEL" in
    *"Raspberry Pi 5"*) pass "Raspberry Pi 5 ($MODEL)" ;;
    *) warn "model is '$MODEL', not a Pi 5" ;;
esac

ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
if [[ "$ARCH" == "arm64" || "$ARCH" == "aarch64" ]]; then
    pass "64-bit userland ($ARCH)"
else
    fail "architecture is '$ARCH'; the node needs arm64"
fi

# shellcheck disable=SC1091
OS_CODENAME="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-unknown}")"
if [[ "$OS_CODENAME" == "trixie" ]]; then
    pass "Raspberry Pi OS / Debian 13 (trixie)"
elif [[ "$OS_CODENAME" == "bookworm" ]]; then
    fail "Bookworm: Hailo package names differ and no in-place upgrade is supported -- reflash"
else
    warn "unrecognised OS codename '$OS_CODENAME'"
fi

GVFS_PRESENT=0
if have gvfsd || dpkg-query -W -f='${db:Status-Status}' gvfs 2>/dev/null | grep -q installed; then
    GVFS_PRESENT=1
    warn "gvfs present -- this is a Desktop image, not Lite. Workable, but only with
        the mitigations checked below; Lite is the supported base."
else
    pass "no gvfs (Lite image)"
fi

DEFTGT="$(systemctl get-default 2>/dev/null)"
if [[ "$DEFTGT" == "graphical.target" ]]; then
    warn "default target is graphical.target -- a desktop session starts at every boot,
        and with it gvfs. 'raspi-config nonint do_boot_behaviour B2' boots to console."
elif [[ -n "$DEFTGT" ]]; then
    pass "default target is $DEFTGT (no graphical session)"
fi

PY3="$(python3 -V 2>&1 || echo unknown)"
pass "system python: $PY3"

# ------------------------------------------------------------- identity ------

head_ "Identity"

HOST="$(hostname)"
if [[ "$HOST" =~ ^scanner-node-([01])$ ]]; then
    ROLE="${BASH_REMATCH[1]}"
    EXPECT_CAM="cam${ROLE}"
    pass "hostname '$HOST' -> role $ROLE, camera id $EXPECT_CAM"
else
    ROLE=""
    EXPECT_CAM=""
    fail "hostname is '$HOST', expected scanner-node-0 or scanner-node-1 -- the node
        server derives its camera id from the hostname, so an unexpected name
        silently mislabels every frame this node captures"
fi

if grep -qE "^127\.0\.1\.1[[:space:]]+${HOST}\b" /etc/hosts 2>/dev/null; then
    pass "/etc/hosts 127.0.1.1 matches the hostname"
else
    warn "no 127.0.1.1 entry for '$HOST' in /etc/hosts (sudo may stall a few seconds)"
fi

# Two nodes with the same hostname is a silent disaster: avahi renames the
# second to <host>-2.local and the orchestrator pairs frames by node URL.
if have avahi-resolve && [[ -n "$HOST" ]]; then
    RESOLVED="$(timeout 5 avahi-resolve -n "${HOST}.local" 2>/dev/null || true)"
    if [[ -n "$RESOLVED" ]]; then
        pass "mDNS: ${HOST}.local -> $(echo "$RESOLVED" | awk '{print $2}')"
    else
        warn "${HOST}.local did not resolve locally"
    fi
fi

# ------------------------------------------------------------- network -------

head_ "Network"

if ! have ip; then
    skip "iproute2 not available"
else
    ADDRS="$(ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4}')"
    if [[ -n "$ADDRS" ]]; then
        pass "$IFACE has address(es): $(echo "$ADDRS" | tr '\n' ' ')"
    else
        fail "$IFACE has no IPv4 address -- the orchestrator cannot reach this node"
    fi

    if ip route show default dev "$IFACE" 2>/dev/null | grep -q .; then
        warn "a default route points out $IFACE. On an isolated camera segment this
        usually means a gateway was configured that will never answer; it is
        the classic cause of 'it worked, then it stopped'."
    else
        pass "no default route via $IFACE (as designed for an isolated segment)"
    fi

    CARRIER="$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)"
    if [[ "$CARRIER" == "1" ]]; then
        pass "$IFACE link is up"
    else
        fail "$IFACE has no carrier -- cable or switch"
    fi
fi

if have nmcli; then
    NM_STATE="$(nmcli -t -f GENERAL.STATE connection show scanner-lan 2>/dev/null | cut -d: -f2)"
    if [[ -n "$NM_STATE" ]]; then
        pass "NetworkManager profile 'scanner-lan' is $NM_STATE"
    elif nmcli -t -f NAME connection show 2>/dev/null | grep -qx scanner-lan; then
        warn "profile 'scanner-lan' exists but is not active"
    else
        warn "no 'scanner-lan' profile -- addressing is not under provisioning control"
    fi
fi

if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    pass "avahi-daemon running (mDNS fallback available)"
else
    warn "avahi-daemon not running -- only the static IP will work"
fi

# ------------------------------------------------------------- camera --------

head_ "Camera access"

# Infer the service user if not given, so the group check tests the right account.
if [[ -z "$NODE_USER" ]]; then
    NODE_USER="$(systemctl show -p User --value scanner-node.service 2>/dev/null)"
fi
[[ -n "$NODE_USER" ]] || NODE_USER="$(awk -F: '$3>=1000 && $3<65534{print $1; exit}' /etc/passwd)"

if [[ -n "$NODE_USER" ]] && id -nG "$NODE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx plugdev; then
    pass "service user '$NODE_USER' is in plugdev"
else
    fail "user '$NODE_USER' is NOT in plugdev. systemd's uaccess tag only grants an
        ACL to a user with an active local seat -- over SSH there is none, so
        the group is the only thing standing between you and
        'Could not claim the USB device'."
fi

if [[ -f /etc/udev/rules.d/95-scanner-camera.rules ]]; then
    pass "udev rule 95-scanner-camera.rules installed"
else
    warn "95-scanner-camera.rules missing (libgphoto2's own rule may still cover it)"
fi

if ls /usr/lib/udev/rules.d/*libgphoto2* /lib/udev/rules.d/*libgphoto2* >/dev/null 2>&1; then
    pass "libgphoto2 vendor udev rules present"
else
    warn "no libgphoto2 udev rules -- apt install libgphoto2-6t64"
fi

# The mask must be at --global (i.e. /etc/systemd/user) scope. A system-scope
# mask of these units is a silent no-op: they are systemd *user* units, D-Bus
# activated, and the user manager never reads /etc/systemd/system.
for gunit in gvfs-gphoto2-volume-monitor.service gvfs-mtp-volume-monitor.service; do
    if [[ "$(readlink -f "/etc/systemd/user/$gunit" 2>/dev/null)" == "/dev/null" ]]; then
        pass "$gunit masked for all users"
    elif [[ $GVFS_PRESENT -eq 1 ]]; then
        fail "$gunit is NOT masked at --global scope, and gvfs is installed. A
        system-scope mask does not count -- these are user units. Run:
        sudo systemctl --global mask $gunit"
    else
        warn "$gunit not masked (harmless while gvfs is absent)"
    fi
    if [[ -L "/etc/systemd/system/$gunit" ]]; then
        warn "a system-scope mask exists for $gunit. It does nothing -- the unit is a
        user unit. Remove it so it stops giving false confidence."
    fi
done

# The definitive test: is anything holding the camera right now?
if pgrep -a gvfsd-gphoto2 >/dev/null 2>&1; then
    fail "gvfsd-gphoto2 is RUNNING -- it has the USB interface and every capture will
        fail with 'Could not claim the USB device'. This is what the masks prevent."
elif [[ $GVFS_PRESENT -eq 1 ]]; then
    pass "gvfsd-gphoto2 not running"
fi

if have gphoto2; then
    DETECT="$(timeout 20 gphoto2 --auto-detect 2>/dev/null | tail -n +3 | grep -v '^\s*$' || true)"
    if [[ -n "$DETECT" ]]; then
        pass "gphoto2 sees a camera: $(echo "$DETECT" | head -1 | sed 's/  */ /g')"
    else
        warn "no camera detected. Check: body powered on, USB Connection -> PC Remote,
        mode dial -> M, cable in the camera's DATA port."
    fi
else
    skip "gphoto2 CLI not installed"
fi

# ------------------------------------------------------------- Hailo ---------

head_ "Hailo AI HAT+"

if [[ -e /dev/hailo0 ]]; then
    pass "/dev/hailo0 present ($(stat -c '%A %U:%G' /dev/hailo0 2>/dev/null))"

    if have hailortcli; then
        HAILO_ID="$(timeout 20 hailortcli fw-control identify 2>&1 || true)"
        if grep -qi 'firmware version' <<<"$HAILO_ID"; then
            FW="$(grep -i 'Firmware Version' <<<"$HAILO_ID" | head -1 | sed 's/.*: *//')"
            ARCHV="$(grep -i 'Device Architecture' <<<"$HAILO_ID" | head -1 | sed 's/.*: *//')"
            pass "hailortcli identify: FW $FW, arch $ARCHV"
            [[ "$ARCHV" == *"HAILO8"* && "$ARCHV" != *"HAILO8L"* ]] \
                || note "expected HAILO8 for the 26 TOPS AI HAT+; HAILO8L is the 13 TOPS part"
            note "N/A for serial/part number is expected on the AI HAT+ and is not a fault"
        elif grep -qi 'different from library version' <<<"$HAILO_ID"; then
            fail "driver/library version mismatch -- apt full-upgrade so hailort,
        hailort-pcie-driver and python3-hailort all move together"
            detail "$HAILO_ID"
        else
            fail "hailortcli fw-control identify failed"
            detail "$HAILO_ID"
        fi
    else
        warn "hailortcli not installed"
    fi
else
    if dpkg-query -W -f='${db:Status-Status}' hailort-pcie-driver 2>/dev/null | grep -q installed; then
        fail "hailort-pcie-driver is installed but /dev/hailo0 is absent. Usually a DKMS
        build that failed after a kernel upgrade -- check 'dkms status | grep hailo'
        and /var/log/hailort-pcie-driver.deb.log, then reboot."
    else
        skip "Hailo stack not installed (fine if the HAT is not fitted)"
    fi
fi

if have dkms; then
    DK="$(dkms status 2>/dev/null | grep -i hailo || true)"
    if [[ -n "$DK" ]]; then
        if grep -qi 'installed' <<<"$DK"; then
            pass "DKMS module built for the running kernel"
        else
            fail "DKMS module not installed for the running kernel: $DK"
        fi
        detail "$DK"
    fi
fi

# ------------------------------------------------------------- python --------

head_ "Python environment"

VENV=""
EXEC_START="$(systemctl show -p ExecStart --value scanner-node.service 2>/dev/null || true)"
if [[ "$EXEC_START" =~ (/[^[:space:]\"]*)/bin/python ]]; then
    VENV="${BASH_REMATCH[1]}"
fi
[[ -n "$VENV" ]] || for c in /home/*/overhead-scanner/venv /opt/scanner/venv; do
    [[ -d "$c" ]] && VENV="$c" && break
done

if [[ -z "$VENV" || ! -x "$VENV/bin/python" ]]; then
    fail "no venv found -- run provision-node.sh"
else
    pass "venv: $VENV ($("$VENV/bin/python" -V 2>&1))"

    if grep -q 'include-system-site-packages = true' "$VENV/pyvenv.cfg" 2>/dev/null; then
        pass "venv has --system-site-packages (required to import hailo_platform)"
    else
        fail "venv was built WITHOUT --system-site-packages. python3-hailort installs
        hailo_platform into /usr/lib/python3/dist-packages and publishes no wheel,
        so the NPU is invisible from this venv. Rebuild it."
    fi

    for mod in numpy cv2 fastapi uvicorn pydantic gphoto2; do
        if "$VENV/bin/python" -c "import $mod" 2>/dev/null; then
            pass "import $mod"
        else
            ERRTXT="$("$VENV/bin/python" -c "import $mod" 2>&1 | tail -1)"
            fail "import $mod -- $ERRTXT"
            [[ "$ERRTXT" == *libGL* ]] && note "install opencv-contrib-python-headless, not the desktop build (see requirements-node.txt)"
        fi
    done

    if [[ -e /dev/hailo0 ]]; then
        if "$VENV/bin/python" -c "import hailo_platform" 2>/dev/null; then
            pass "import hailo_platform (NPU reachable from the venv)"
        else
            warn "hailo_platform not importable from the venv -- live QA on the HAT will
        not run. Check python3-hailort is installed and the venv python is the
        system 3.13 (the .so is ABI-tagged cpython-313)."
        fi
    fi
fi

# ------------------------------------------------------------- service -------

head_ "Node service"

SERVICE_INSTALLED=0
if systemctl list-unit-files scanner-node.service >/dev/null 2>&1 && \
   systemctl cat scanner-node.service >/dev/null 2>&1; then
    SERVICE_INSTALLED=1

    if systemctl is-enabled --quiet scanner-node.service; then
        pass "scanner-node.service enabled at boot"
    else
        fail "scanner-node.service is not enabled -- it will not come back after a reboot"
    fi

    if systemctl is-active --quiet scanner-node.service; then
        SINCE="$(systemctl show -p ActiveEnterTimestamp --value scanner-node.service 2>/dev/null)"
        NRESTART="$(systemctl show -p NRestarts --value scanner-node.service 2>/dev/null)"
        pass "scanner-node.service active since ${SINCE:-?}"
        if [[ -n "$NRESTART" && "$NRESTART" -gt 3 ]]; then
            warn "$NRESTART restarts since boot -- something is crashing; journalctl -u scanner-node"
        fi
    else
        fail "scanner-node.service is not running -- journalctl -u scanner-node -n 50"
    fi

    # Assert the *effect*, not the spelling.  StartLimitIntervalSec belongs in
    # [Unit]; in [Service] systemd calls it an unknown key, drops it, and
    # applies the default limit -- while the unit file still reads as though
    # the limiter were disabled.  Observed on scanner-node-0: `10s` with the
    # directive in the wrong section, `0` with it in the right one.  Reading
    # the unit cannot tell those apart; asking systemd can.
    START_LIMIT="$(systemctl show -p StartLimitIntervalUSec --value scanner-node.service 2>/dev/null)"
    if [[ "$START_LIMIT" == "0" ]]; then
        pass "start rate limiter disabled (StartLimitIntervalUSec=0)"
    else
        fail "start rate limiter is ACTIVE (StartLimitIntervalUSec=${START_LIMIT:-?}).  The
        A6000 drops its PTP session when idle; with Restart=always a burst of
        reconnect failures will trip the limit and systemd will stop this node
        permanently.  StartLimitIntervalSec=0 must be in [Unit], not [Service]
        -- check 'journalctl -u scanner-node -b | grep \"Unknown key\"'."
    fi
else
    fail "scanner-node.service not installed -- run provision-node.sh"
fi

if [[ $SERVICE_INSTALLED -eq 0 ]]; then
    skip "HTTP probes (no service to probe)"
elif ! have curl; then
    skip "curl not installed; cannot probe the HTTP API"
else
    if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
        pass "GET /healthz on port $PORT"
    else
        fail "GET /healthz on port $PORT failed -- the unit is loaded but nothing is
        answering. journalctl -u scanner-node -n 50"
    fi

    # Generous timeout: /status may reconnect a dropped PTP session, which the
    # A6000 does routinely when idle.
    STATUS_JSON="$(curl -fsS --max-time 25 "http://127.0.0.1:${PORT}/status" 2>/dev/null || true)"
    if [[ -n "$STATUS_JSON" ]]; then
        get_field() { sed -n "s/.*\"$1\": *\"\{0,1\}\([^,\"}]*\)\"\{0,1\}.*/\1/p" <<<"$STATUS_JSON" | head -1; }
        S_CAM="$(get_field camera_id)"; S_BACK="$(get_field backend)"
        S_CONN="$(get_field connected)"; S_MODEL="$(get_field model)"
        S_ERR="$(get_field last_error)"

        if [[ -n "$EXPECT_CAM" && "$S_CAM" == "$EXPECT_CAM" ]]; then
            pass "/status camera_id=$S_CAM (matches the hostname)"
        elif [[ -n "$EXPECT_CAM" ]]; then
            fail "/status reports camera_id=$S_CAM but the hostname implies $EXPECT_CAM --
        frames from this node would be labelled as the wrong camera"
        fi

        if [[ "$S_BACK" == "gphoto2" ]]; then
            pass "/status backend=gphoto2"
        else
            fail "/status backend=$S_BACK -- this node is serving SIMULATED frames.
        Every measurement taken through it is fiction. Set SCANNER_BACKEND=gphoto2
        in the unit and make sure 'import gphoto2' works in the venv."
        fi

        if [[ "$S_CONN" == "true" ]]; then
            pass "/status connected=true${S_MODEL:+ (}${S_MODEL}${S_MODEL:+)}"
        else
            warn "/status connected=false${S_ERR:+ -- last_error: $S_ERR}"
            note "normal if no body is plugged in yet; POST /connect once it is"
        fi

        # Staging headroom has to come from the node, not from df: the
        # service runs with PrivateTmp=yes, so its staging area is a tmpfs
        # inside a mount namespace this shell cannot see. `df /` on the host
        # will cheerfully report 100 GB free while the node has none left.
        S_STAGE="$(get_field staging_free_mb)"
        if [[ -z "$S_STAGE" || "$S_STAGE" == "-1" ]]; then
            skip "staging headroom (backend does not stage frames to disk)"
        elif [[ "$S_STAGE" -lt 300 ]]; then
            fail "/status staging_free_mb=$S_STAGE -- under the ~290 MB a 'max' profile
        needs. Captures will start refusing with 507. Frames are staged in RAM
        and nothing frees them automatically: the orchestrator must collect
        them via /files."
        elif [[ "$S_STAGE" -lt 1000 ]]; then
            warn "/status staging_free_mb=$S_STAGE -- room for roughly $((S_STAGE / 25))
        more frames. Collect the staged frames before starting a long run."
        else
            pass "/status staging_free_mb=$S_STAGE (~$((S_STAGE / 25)) frames of headroom)"
        fi
        detail "$STATUS_JSON"
    else
        fail "GET /status returned nothing"
    fi
fi

# ------------------------------------------------------------- upkeep --------

head_ "Housekeeping"

JSTORAGE="$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null | grep -E '^\s*Storage=' | tail -1 | sed 's/.*= *//')"
if [[ "$JSTORAGE" == "persistent" ]]; then
    pass "journal is persistent (survives reboot)"
else
    warn "journald Storage=${JSTORAGE:-volatile} -- Pi OS defaults to volatile, so the
        logs from an unattended overnight run are lost at the next boot"
fi

if have timedatectl; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
        pass "clock synchronised ($(date -Is))"
    else
        warn "clock not NTP-synchronised. The Pi 5 has no RTC battery fitted by default;
        file mtimes on captures will be wrong. Point --ntp at the laptop."
    fi
fi

ROOTFREE="$(df -P / 2>/dev/null | awk 'NR==2{print $4}')"
if [[ -n "$ROOTFREE" ]]; then
    if [[ "$ROOTFREE" -lt 1048576 ]]; then
        fail "less than 1 GB free on / ($((ROOTFREE/1024)) MB)"
    else
        pass "root filesystem has $((ROOTFREE/1024/1024)) GB free"
    fi
fi

TEMP_RAW="$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || true)"
if [[ -n "$TEMP_RAW" ]]; then
    TEMP=$((TEMP_RAW / 1000))
    if [[ "$TEMP" -ge 80 ]]; then
        warn "SoC at ${TEMP} C -- throttling territory; check the Active Cooler"
    else
        pass "SoC temperature ${TEMP} C"
    fi
fi

if have vcgencmd; then
    THROT="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
    if [[ -n "$THROT" && "$THROT" != "0x0" ]]; then
        warn "throttling flags $THROT -- under-voltage or thermal. With two USB-tethered
        bodies this usually means the PSU. Use the official 27 W supply."
    else
        pass "no throttling or under-voltage flags"
    fi
fi

# ------------------------------------------------------------- verdict -------

printf '\n%s%s%s\n' "$C_HEAD" "----------------------------------------" "$C_OFF"
printf '  %s%d passed%s   %s%d failed%s   %s%d warnings%s   %d skipped\n' \
       "$C_OK" "$N_PASS" "$C_OFF" \
       "$([[ $N_FAIL -gt 0 ]] && echo "$C_ERR" || echo "$C_DIM")" "$N_FAIL" "$C_OFF" \
       "$([[ $N_WARN -gt 0 ]] && echo "$C_WARN" || echo "$C_DIM")" "$N_WARN" "$C_OFF" \
       "$N_SKIP"

if [[ $N_FAIL -gt 0 ]]; then
    printf '\n%sNode is NOT ready.%s Failures:\n' "$C_ERR" "$C_OFF"
    for f in "${FAILED[@]}"; do printf '  - %s\n' "${f%%$'\n'*}"; done
    exit 1
fi

printf '\n%sNode is ready.%s' "$C_OK" "$C_OFF"
if [[ $N_WARN -gt 0 ]]; then
    printf ' Read the %d warning(s) above before trusting a measurement.' "$N_WARN"
fi
printf '\n'
exit 0
