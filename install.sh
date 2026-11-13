#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
  --check)
    bash -n "$ROOT/bin/vps-watchdog"
    bash -n "$ROOT/bin/vps-auto-updater"
    python3 -m unittest discover -s "$ROOT/tests" -v
    exit 0
    ;;
  --install) ;;
  *) printf 'Usage: %s --check | --install\n' "$0" >&2; exit 2 ;;
esac
[[ $EUID == 0 && "$(uname -s)" == Linux ]] || { echo 'Install on Linux as root.' >&2; exit 1; }
for tool in bash curl jq timeout flock python3 docker systemctl ufw tailscale cscli apt apt-get dpkg unattended-upgrade; do
  command -v "$tool" >/dev/null || { echo "Missing dependency: $tool" >&2; exit 1; }
done
docker compose version >/dev/null
for legacy in vps-watchdog.service vps-os-auto-update.service vps-container-auto-update.service; do
  if systemctl cat "$legacy" >/dev/null 2>&1; then
    echo 'Existing monitoring installation found. Review a migration; this installer will not create a duplicate.' >&2
    exit 1
  fi
done
[[ ! -e /usr/local/libexec/modelmlab-monitoring ]] || { echo 'Installation exists; review upgrades separately.' >&2; exit 1; }
for unit in "$ROOT"/systemd/*; do
  [[ ! -e "/etc/systemd/system/${unit##*/}" ]] || { echo 'Unit exists; nothing replaced.' >&2; exit 1; }
done
"$ROOT/install.sh" --check
install -d -m 0755 /usr/local/libexec/modelmlab-monitoring
install -m 0755 "$ROOT/bin/vps-watchdog" "$ROOT/bin/vps-auto-updater" /usr/local/libexec/modelmlab-monitoring/
install -m 0644 "$ROOT/bin/notice_state.py" /usr/local/libexec/modelmlab-monitoring/
install -d -m 0700 /etc/modelmlab-monitoring /var/lib/modelmlab-monitoring/watchdog /var/lib/modelmlab-monitoring/updater/backups /var/log/modelmlab-monitoring/updater
install -m 0600 "$ROOT/monitoring.env.example" /etc/modelmlab-monitoring/monitoring.env.example
for unit in "$ROOT"/systemd/*; do install -m 0644 "$unit" /etc/systemd/system/; done
systemctl daemon-reload
printf 'Installed files only. Configure monitoring.env and webhook files, then preview before enabling timers.\n'
