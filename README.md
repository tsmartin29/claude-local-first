# claude-local-first

Move recurring work off the model. A scheduler for plain scripts, an escalation ladder (script, local model, cheap API model, Claude), a menu-bar usage meter, and a cheap-mode launcher. Everything is a suggestion you can decline.

Made by Tommy Martin. Built while running [ClubPlugins](https://clubplugins.com).

**You stay in control: every move to a cheaper tier is a suggestion you can decline.** Nothing changes tier on its own unless you accepted that specific move, or you turned on `auto_move` yourself (it is off by default).

## Why

If you run Claude Code all day, a lot of what people ask an AI to do is not judgment: a report that is the same every two hours, a check that only matters when something changed, a nightly backup, a digest, a disk clean-up. A script does these for free, every time, the same way. A model is for the parts that need judgment, code changes and releases.

Honest numbers from our own setup: when we estimated where our usage went from local transcripts, scheduled AI tasks were a small share of it. The big savings came from moving recurring checks to plain scripts and keeping long chats short (one task per session, a handoff note instead of a 200k-token history, few sub-agents at once). This repo is the tooling we built around that habit. It will not make a bad prompt cheap, and it will not tell you your plan's real limit: the meter's numbers are estimates (marked `~`).

## The ladder

| Tier | What runs | Cost |
|---|---|---|
| 0 | A local deterministic script (shell or Python), scheduled by launchd. No model at all. | none |
| 1 | A local model through [Ollama](https://ollama.com), for summaries and classification. Used only if Ollama is running. | none (your own machine) |
| 2 | A cheap model through OpenRouter, using `claude-cheap` (Claude Code pointed at OpenRouter). | pennies |
| 3 | Claude. Judgment, code changes, releases. The ladder never spends Claude by itself: it writes a handoff file (or runs `claude -p` only if you pass `--allow-claude`). | your plan |

`ladder run task.yaml` starts a task at the tier it runs at today and climbs up when a tier fails or is unavailable. Moving a task to a cheaper tier than where it runs today needs your OK (see below). Tasks marked `never_cheap: true` always go to Claude and are never suggested for anything cheaper.

## What is in the box

| Command | What it does |
|---|---|
| `localjob` | `add / list / run / logs / remove` scheduled plain commands (launchd on macOS; systemd user timers or a cron line on Linux). Logs with rotation, a RAM/disk gate, one notification on failure, a one-paragraph result appended to `INBOX.md`, and the exact fix when macOS blocks a job. |
| `ladder` | `run / plan / stats` for task files. Records which tier ran and an estimate of input not sent to an API. |
| `scriptify` | Reads your own Claude Code transcripts (read-only) and lists shell commands you keep running, ready to turn into a `localjob`. Secrets are redacted. Nothing is uploaded; the module has no network code. |
| `localfirst` | `suggestions / accept / decline [--never] / undo / status / refresh`. |
| `meter/` | Menu-bar app (Swift) with a green/amber/red dot, a CPU readout, suggestions with Accept / Not now / Never, and "Open cheap mode (Terminal)". Plus `usage_from_transcripts.py`, which estimates usage from local transcripts. |
| `claude-cheap`, `claude-cheap-key`, `cheap-job` | Tier 2: Claude Code on OpenRouter models with a clean environment, banner, status line and guardrails. |
| `relay/relay.py` | Optional: when usage is empty, tells you (or, with `auto_run`, starts one cheap-mode run over a folder of handoff files). |

Standard library only for everything in Python (3.9+). The meter needs the Xcode command line tools (`swiftc`).

## Quick start

```bash
git clone https://github.com/tsmartin29/claude-local-first && cd claude-local-first
python3 -W ignore -m unittest discover -s tests      # optional, 100+ tests, nothing is loaded into launchd
./install.sh                                         # copies to ~/.local-first/lib, links commands into ~/.local/bin
```

Schedule something that needs no AI:

```bash
localjob add disk-check --every 1h -- df -h /
localjob run disk-check          # run it now, in the foreground
localjob list
localjob logs disk-check
localjob remove disk-check       # the plist goes to the Trash
```

Schedules: `30s`, `15m`, `2h`, `1d`, or `daily@06:47`. Useful options: `--min-avail-gb 4 --min-disk-gb 10` (skip the run if the machine is short; a skip is not a failure), `--heavy` (queue behind [memguard](https://github.com/tsmartin29/memguard)'s `heavyrun` if it is on your PATH), `--inbox always|fail|never`, `--timeout 600`, `--dry-run` (print the plist, write nothing), `--backend systemd|cron`.

Find what to schedule:

```bash
scriptify                 # commands repeated across sessions or days, with how often, and a ready-made localjob line
```

Run a task on the ladder:

```bash
ladder plan examples/summarise-notes.yaml    # which tiers are allowed and available, and why
ladder run examples/disk-report.yaml
ladder stats
```

A task file (a small YAML subset, or JSON):

```yaml
name: disk-report
tier0: {command: ["sh", "scripts/disk_report.sh"]}   # optional: a script, no model
prompt: "Summarise this in two sentences."           # used by tiers 1-3
inputs: [notes.md]                                   # files appended to the prompt for tiers 1-2
tier: 3                                              # where it runs today (default 0 if it has a script, else 3)
never_cheap: false                                   # true = always Claude
max_tier: 3
```

Turn on the pieces you want:

```bash
./install.sh --meter     # build the menu-bar meter, start it at login, refresh usage + suggestions every 15 minutes
./install.sh --relay     # optional usage-empty watcher
claude-cheap-key         # tier 2: save your OpenRouter key in the Keychain once
claude-cheap haiku       # tier 2: Claude Code on OpenRouter
```

## Suggestions, and why they are only suggestions

The meter turns your usage into a band: AMBER when the 5-hour window is at 80% or more, or weekly is at 85% or more (or one model's weekly at 85%, or extra usage at 90% of its cap); RED at 100%. All numbers are in `config.json` under `bands`.

When usage rises into AMBER or RED, the menu and `ladder` show suggestions, for example:

- `Move 'daily report' to tier 0 (script)` (a task you have, or a command `scriptify` found you keep running)
- `Run 'summarise chats' on tier 2 (cheap) instead of Claude`

For each one you can **Accept**, **Not now**, or **Never suggest this again**, in the menu (click the dot; a small blue dot appears next to the usage when suggestions are waiting) or from the command line:

```bash
localfirst suggestions
localfirst accept t-summarise-chats
localfirst decline t-summarise-chats            # not now: it comes back the next time usage rises into a higher band
localfirst decline t-summarise-chats --never    # never again
localfirst undo t-summarise-chats               # forget a decision
localfirst status                               # band, accepted moves, tokens-not-sent estimate
```

Decisions are saved in `~/.local-first/decisions.json`. Rules:

- A move happens only if you accepted that specific one, or you set `"auto_move": true` in `config.json` (default `false`). Even with `auto_move`, declined, never and `never_cheap` items are respected, and script suggestions only ever become a draft file.
- Accepting a **script** suggestion writes a draft `localjob add ...` file to `~/.local-first/drafts/` and schedules nothing: the timing and the command are yours to review.
- Script suggestions from `scriptify` skip anything that looks like it changes the outside world (deploy, publish, push, upload, delete, send, sign, and similar): that is judgment work, not something for a timer.
- The meter never moves work. Its buttons run `localfirst accept|decline`, and the cheap-mode item opens `claude-cheap` in Terminal.

## Calibrating the meter

The meter reads one local JSON file (`~/.local-first/usage.json`). It makes no network calls, uses no private Claude-app API, reads no cookies and holds no tokens. By default `meter/usage_from_transcripts.py` fills it by adding up the token counts in your own Claude Code logs (`~/.claude/projects/**/*.jsonl`) for the last 5 hours and 7 days and dividing by budgets you set in `config.json`. Anthropic does not publish plan limits as token counts, and other Claude surfaces leave no local log, so this is an **estimate** and the meter marks it `~`.

Calibrate once: when Claude tells you you are at, say, 80% of a window, run `python3 ~/.local-first/lib/meter/usage_from_transcripts.py --print`, read `five_hour_tokens`, and set `five_hour_budget_tokens` to that number divided by 0.8. Do the same for the weekly number. Until you do, the meter shows a grey dot ("UNCALIBRATED") instead of guessing a band; then set `"budgets_calibrated": true` in `~/.local-first/config.json`. If you would rather type exact numbers from your own account page, set `"usage_source": "file"` and write `usage.json` yourself (format in `meter/usage.example.json`).

## Cheap mode (tier 2)

`claude-cheap` starts Claude Code against OpenRouter, so work is billed to your OpenRouter credit instead of your Claude plan. It starts `claude` with `env -i`, so your normal Claude login is never sent to OpenRouter. It sets the window title, prints a two-line banner, names the session "cheap mode", appends guardrails to the system prompt, and passes a status line:

`⚡ CHEAP MODE · <model> · <folder> · $X left · inbox N`

The credit is read with one request to `https://openrouter.ai/api/v1/credits`, cached for two minutes and refreshed in the background. Everything is configurable with environment variables or a plain `~/.local-first/cheap.env` file (`KEY=VALUE`): `CHEAP_KEYCHAIN_SERVICE`, `CHEAP_INBOX`, `CHEAP_DIR`, `CHEAP_GUARDRAILS` or `CHEAP_GUARDRAILS_FILE`, `CHEAP_MAIN_MODEL`, `CHEAP_FAST_MODEL`, `CHEAP_HAIKU_MODEL`, `CHEAP_STATUSLINE=0`. Model ids are OpenRouter ids and change; check [openrouter.ai/models](https://openrouter.ai/models). The default guardrails say: no builds, releases or deploys, move files to the Trash instead of deleting, append results to the inbox, be concise. Read them and edit them for your own work.

`cheap-job handoff.md` runs one handoff file in cheap mode and appends the result to the inbox. `relay/relay.py` can do the same automatically when your usage is empty, but only if you set `"auto_run": true` (or the global `auto_move`); by default it just notifies you.

What is sent where: a tier 2 run sends the prompt and any `inputs` to OpenRouter and the model provider. `redact_for_api` (on by default) masks anything that looks like a secret first, but redaction is a heuristic: only put files in `inputs` that you are happy to send. Tier 0 and tier 1 never leave your machine.

## macOS privacy (TCC): the one that will bite you

launchd jobs cannot read `~/Desktop`, `~/Documents`, `~/Downloads`, iCloud Drive or removable drives unless the program launchd starts has **Full Disk Access**. A job that works in Terminal can fail silently from launchd with `Operation not permitted`.

- Best fix: **keep job scripts and job data outside those folders** (for example `~/.local-first/data` or `~/bin`). `./install.sh` copies the tools to `~/.local-first/lib` for the same reason; do not run scheduled jobs from a clone in `~/Desktop`.
- `localjob add` warns when a command, working folder or the wrapper is inside a protected folder.
- If a job fails with `Operation not permitted`, `localjob` detects it, writes the exact steps to the job's log, and puts a short note in the notification and `INBOX.md`: System Settings, Privacy & Security, Full Disk Access, `+`, press Cmd+Shift+G, paste the interpreter path (`/usr/bin/python3` unless you passed `--python`), switch it on, then `launchctl kickstart -k gui/<uid>/<label>`. If macOS will not let you add `/usr/bin/python3`, install Python from python.org or Homebrew and re-add the job with `--python /path/to/it`.

## Examples from our own setup

These are described generically; the scripts themselves are yours to write. None of them uses a model.

- **A report every 2 hours.** A script reads the sales and ad-platform exports, writes a short readout file and prints the three numbers that changed. `localjob add report --every 2h -- ...`
- **Download alerts every 5 minutes.** A script checks a counter and prints nothing unless it changed; `--inbox fail` keeps the inbox quiet. `localjob add download-alerts --every 5m ...`
- **A nightly source backup.** `localjob add source-backup --every daily@03:30 --min-disk-gb 2 ...` pushes work to a private remote. (This is the job that needed Full Disk Access for us; see the macOS privacy section.)
- **A memory guard every 15 seconds.** [memguard](https://github.com/tsmartin29/memguard) pauses runaway background jobs before the machine swaps. `localjob add memguard --every 15s ...`
- **A usage-meter feed.** `usage_from_transcripts.py` every 15 minutes writes the file the menu bar reads.
- **A daily disk clean-up pass.** Old files go to the Trash (never deleted) and a three-line report lands in the inbox. `localjob add disk-cleanup --every daily@06:47 ...`

More in `examples/jobs.sh`, and three task files in `examples/`.

## Honest limits

- macOS first. The Linux paths (systemd user timers, cron line, `/proc/meminfo`) are generated and unit-tested but have not been run on a real Linux machine.
- The menu-bar app compiles and reads the same files the tests cover, but its menu is not covered by automated UI tests.
- Tier 1 needs Ollama running and a model pulled; we tested the client against a local stub server, not against every model. Small local models are fine for classification and short summaries and poor at anything that needs care.
- "Tokens not sent" is an estimate (characters divided by 4), counted only for runs that stayed on tier 0 or 1, and only for the input files (and, for tier 1, the prompt) that would otherwise have gone to an API. It is not a bill.
- `scriptify` finds repeats; it cannot tell whether a command depends on the state of the session it came from. Review before you schedule.
- The task-file reader supports a small YAML subset (no lists of maps, no inline maps). Use JSON for anything fancier.

## Related

- [memguard](https://github.com/tsmartin29/memguard): pauses or stops runaway agent jobs before the Mac runs out of RAM; `localjob --heavy` queues behind its `heavyrun`.
- [claude-safety-hooks](https://github.com/tsmartin29/claude-safety-hooks): hooks that stop an agent from permanently deleting files, overwriting without a backup, or saying "done" untested.
- [solo-company-claude-playbook](https://github.com/tsmartin29/solo-company-claude-playbook): how we run a small company with many Claude Code sessions.

## Layout

```
local_first/   localjob, ladder, scriptify, localfirst (cli), suggest, bands, decisions, common, miniyaml
bin/           the commands and the cheap-mode launcher
meter/         menu-bar app (Swift), usage estimator, build script, login agent template
cheap/         status line for cheap mode
relay/         optional usage-empty watcher
examples/      task files and example localjob lines
tests/         unittest, stdlib only, temp HOME, never loads launchd
```

## Licence

MIT. See `LICENSE`.

Made by Tommy Martin. Built while running [ClubPlugins](https://clubplugins.com).
