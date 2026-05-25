#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${SEC_USER_AGENT:-}" ]]; then
  export SEC_USER_AGENT="guru-13f-tracker local-run contact@example.com"
fi

python3 guru_13f_tracker.py --investors investors.json --out reports --strict

echo
echo "Report written to:"
echo "  $(pwd)/reports/latest_changes.md"
echo "  $(pwd)/reports/latest_changes.csv"
echo "  $(pwd)/reports/latest_chart.html"
