#!/usr/bin/env bash
# RB-VOR installer for Debian 12+ / Raspberry Pi OS 64-bit.
#
# Assumes: a clean image, hand tools, a soldering iron, a 3D printer, no lab
# equipment. Run once, from a clone of the repository:
#
#     sudo ./install.sh
#
# Installs to /opt/rb-vor, config to /etc/rb-vor, and a systemd unit running as
# an unprivileged dedicated user. The daemon never needs root at runtime.
#
#     ./install.sh --uninstall     remove everything except captures and logs
#     ./install.sh --no-service    files only, no systemd
set -euo pipefail

PREFIX=/opt/rb-vor
CONFDIR=/etc/rb-vor
USER_NAME=rb-vor
UNIT=rb-vor.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DO_SERVICE=1

for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --no-service) DO_SERVICE=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*"; }
die()  { printf '\033[31m    error: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

if [ "${UNINSTALL:-0}" = 1 ]; then
  say "Uninstalling"
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT" /etc/udev/rules.d/99-rb-vor-sdr.rules \
        /etc/modprobe.d/rb-vor-blacklist.conf
  rm -rf "$PREFIX"
  systemctl daemon-reload
  udevadm control --reload-rules || true
  echo "    kept: $CONFDIR, /var/lib/rb-vor, /var/log/rb-vor"
  exit 0
fi

# --------------------------------------------------------------------------- #
say "Checking the platform"
. /etc/os-release 2>/dev/null || true
echo "    ${PRETTY_NAME:-unknown}, $(uname -m), python $(python3 -V 2>&1 | cut -d' ' -f2)"
python3 - <<'PY' || die "Python 3.11 or newer is required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

# --------------------------------------------------------------------------- #
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-numpy python3-scipy python3-jsonschema \
  python3-aiohttp python3-websockets \
  rtl-sdr librtlsdr0 python3-soapysdr soapysdr-module-rtlsdr usbutils \
  || die "package installation failed"

# --------------------------------------------------------------------------- #
# Open item O7: the RTL-SDR Blog V4 uses an R828D tuner and needs the
# rtl-sdr-blog fork of librtlsdr. On the stock driver it appears to tune but
# produces nothing usable above ~100 MHz -- which is exactly our band, so this
# is not a subtle degradation, it is total failure with no error message.
say "Checking the SDR driver"
V4_DETECTED=0
if command -v rtl_test >/dev/null 2>&1; then
  if timeout 5 rtl_test -t 2>&1 | grep -qi 'R828D\|Blog V4'; then
    V4_DETECTED=1
  fi
  timeout 5 rtl_test -t 2>&1 | sed 's/^/    /' || true
else
  warn "rtl_test not found; skipping the driver check"
fi
if [ "$V4_DETECTED" = 1 ] && ! strings /usr/lib/*/librtlsdr.so* 2>/dev/null | grep -qi 'rtlsdr_blog\|Blog V4'; then
  warn "RTL-SDR Blog V4 detected but the stock librtlsdr is installed."
  warn "The V4 needs the rtl-sdr-blog fork or it will produce nothing in the"
  warn "108-118 MHz band. See docs/install.md, section 'RTL-SDR Blog V4'."
fi

# --------------------------------------------------------------------------- #
say "Blacklisting the DVB-T kernel driver"
install -m 0644 "$SRC/systemd/rb-vor-blacklist.conf" /etc/modprobe.d/
for m in dvb_usb_rtl28xxu rtl2832 rtl2830; do
  modprobe -r "$m" 2>/dev/null || true
done
echo "    the kernel DVB driver claims RTL2832U dongles on sight; without this"
echo "    rtl-sdr fails with usb_claim_interface error -6"

# --------------------------------------------------------------------------- #
say "Creating the service user"
getent group plugdev >/dev/null || groupadd plugdev
if ! getent passwd "$USER_NAME" >/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin \
          --groups plugdev "$USER_NAME"
  echo "    created $USER_NAME"
else
  usermod -aG plugdev "$USER_NAME"
  echo "    $USER_NAME already exists"
fi

say "Installing udev rules"
install -m 0644 "$SRC/systemd/99-rb-vor-sdr.rules" /etc/udev/rules.d/
udevadm control --reload-rules && udevadm trigger || warn "udev reload failed"

# --------------------------------------------------------------------------- #
say "Installing files to $PREFIX"
install -d -m 0755 "$PREFIX" "$CONFDIR"
install -d -m 0755 -o "$USER_NAME" -g "$USER_NAME" /var/lib/rb-vor /var/log/rb-vor
cp -r "$SRC/rbvor" "$SRC/schema" "$PREFIX/"
cp -r "$SRC/tools" "$PREFIX/"
chmod -R a+rX "$PREFIX"

if [ -f "$CONFDIR/rb-vor.json" ]; then
  echo "    keeping existing $CONFDIR/rb-vor.json"
  cp "$SRC/config/rb-vor.json" "$CONFDIR/rb-vor.json.new"
  echo "    new default written to rb-vor.json.new for comparison"
else
  install -m 0644 "$SRC/config/rb-vor.json" "$CONFDIR/rb-vor.json"
fi

say "Validating the configuration"
( cd "$PREFIX" && python3 -c "
import sys; sys.path.insert(0, '.')
from rbvor.config import load, ConfigError
try:
    cfg = load('$CONFDIR/rb-vor.json')
except ConfigError as e:
    print('    ' + str(e)); sys.exit(1)
print('    ok:', cfg.source)
print('    calibrated:', cfg.calibrated)
" ) || die "configuration is invalid; refusing to install a broken service"

# --------------------------------------------------------------------------- #
if [ "$DO_SERVICE" = 1 ]; then
  say "Installing the systemd unit"
  install -m 0644 "$SRC/systemd/$UNIT" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable "$UNIT"
  echo "    enabled (not started -- see below)"
fi

# --------------------------------------------------------------------------- #
say "Done"
CALIBRATED=$(cd "$PREFIX" && python3 -c "
import sys; sys.path.insert(0,'.')
from rbvor.config import load
print('yes' if load('$CONFDIR/rb-vor.json').calibrated else 'no')")

if [ "$CALIBRATED" = "no" ]; then
  cat <<'MSG'

    The bearing calibration is not set, so the daemon will publish only flags
    (vorsignal 0) and never a bearing. This is deliberate: an uncalibrated
    bearing looks plausible and is wrong, which is the worst failure this
    system has.

    Measure it, then start the service:

        cd /opt/rb-vor
        python3 tools/vor_calibrate.py --json /tmp/bearing.json
        # merge the "bearing" block into /etc/rb-vor/rb-vor.json
        sudo systemctl start rb-vor

    Then confirm it against a real station -- see docs/calibration.md.
MSG
else
  echo
  echo "    sudo systemctl start rb-vor"
  echo "    journalctl -u rb-vor -f"
fi
