#!/usr/bin/env bash
# Install the daily job. Defaults to 07:40 local time - deliberately not on the
# hour, so it is not queued behind every other cron job on the machine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOUR="${HOUR:-7}"
MIN="${MIN:-40}"
MODE="${1:-cron}"

case "$MODE" in
cron)
  LINE="$MIN $HOUR * * * $HERE/scripts/daily.sh"
  ( crontab -l 2>/dev/null | grep -v "$HERE/scripts/daily.sh" || true; echo "$LINE" ) | crontab -
  echo "installed crontab entry:"
  echo "  $LINE"
  echo "check with: crontab -l"
  ;;
systemd)
  UNIT="$HOME/.config/systemd/user"
  mkdir -p "$UNIT"
  cat > "$UNIT/hrbp-sourcer.service" <<UNITEOF
[Unit]
Description=hrbp-sourcer daily candidate run

[Service]
Type=oneshot
WorkingDirectory=$HERE
ExecStart=$HERE/scripts/daily.sh
UNITEOF
  cat > "$UNIT/hrbp-sourcer.timer" <<UNITEOF
[Unit]
Description=Run hrbp-sourcer once a day

[Timer]
OnCalendar=*-*-* $(printf '%02d' "$HOUR"):$(printf '%02d' "$MIN"):00
Persistent=true

[Install]
WantedBy=timers.target
UNITEOF
  systemctl --user daemon-reload
  systemctl --user enable --now hrbp-sourcer.timer
  echo "installed systemd user timer. check with:"
  echo "  systemctl --user list-timers hrbp-sourcer.timer"
  ;;
*)
  echo "usage: $0 [cron|systemd]   (env: HOUR, MIN)" >&2
  exit 2
  ;;
esac
