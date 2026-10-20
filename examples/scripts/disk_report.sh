#!/bin/sh
# A tier-0 example: a plain script that needs no model. Prints free disk space and the biggest folders in your home.
echo "Disk report $(date '+%Y-%m-%d %H:%M')"
df -h / | tail -1
echo "Largest folders in your home directory:"
du -sk "$HOME"/* 2>/dev/null | sort -rn | head -5 | awk '{printf "  %.1f GB  %s\n", $1/1048576, $2}'
