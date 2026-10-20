#!/bin/sh
# Example localjob lines. Every one runs with no model. Edit the paths, then run the lines you want.
# Keep scripts and data OUTSIDE ~/Desktop, ~/Documents and ~/Downloads (macOS privacy blocks scheduled jobs there).

# A report every 2 hours, written by a plain script:
localjob add report --every 2h -- python3 "$HOME/bin/report.py"

# A check every 5 minutes that only raises an alert when something changed (the script prints nothing when all is well):
localjob add download-alerts --every 5m --inbox fail -- python3 "$HOME/bin/new_downloads.py"

# A nightly backup of your source folders to a private remote:
localjob add source-backup --every daily@03:30 --min-disk-gb 2 -- sh "$HOME/bin/backup.sh"

# A memory guard every 15 seconds (see github.com/tsmartin29/memguard):
localjob add memguard --every 15s --inbox fail -- python3 "$HOME/.config/memguard/memguard.py"

# Refresh the usage meter's data feed every 15 minutes:
localjob add usage-feed --every 15m --inbox never -- python3 "$HOME/.local-first/lib/meter/usage_from_transcripts.py"

# A daily clean-up pass that moves old files to the Trash and reports what it moved:
localjob add disk-cleanup --every daily@06:47 -- sh "$HOME/bin/cleanup.sh"
