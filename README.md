# AiRTraffic Control

**Voice air-traffic control for AI agent fleets** — hackathon MVP for [AI Infra Summit](https://aiinfrasummit.com/) Speechmatics bonus track (submit by Sep 16, 2026 ~6:00 PM PT / Sep 17 01:00 UTC).

One screen: big listening state, a short list of running workers (name / status / PID), and voice commands with a **confirm gate on kill**. Every control action writes a timestamped **audit receipt** (before/after status + PID).

## Why

Agent fleets already have dashboards and CLIs. They rarely have a hands-free control surface that is as fast as talking to a tower. AiRTraffic Control is a thin ATC layer: listen → parse → act → prove it in the audit log.

## Speechmatics (required path)

This project uses **Speechmatics Realtime** properly — not browser `SpeechRecognition` as the primary path.

1. **Backend** `POST /api/speechmatics/token` mints a short-lived Realtime JWT from `SPEECHMATICS_API_KEY` via:
   ```
   POST https://mp.speechmatics.com/v1/api_keys?type=rt
   Authorization: Bearer <SPEECHMATICS_API_KEY>
   ```
2. **Frontend** captures the mic, converts audio to **PCM16 @ 16 kHz**, and streams to:
   ```
   wss://global.rt.speechmatics.com/v2?jwt=<token>
   ```
3. On final **`AddTranscript`**, the UI `POST`s the text to `/api/command`, which parses and executes.

Optional **text fallback** (`/api/command/text`) exists for demos without a mic. It is not the primary ASR path.

> Honest note: this is **not** an AssemblyAI Voice Agents / LeMUR build. STT is Speechmatics Realtime; control logic is our FastAPI registry.

## Quick start

```bash
cd /Users/simeong/Projects/airtraffic-control   # or your clone
./start.sh
# open http://127.0.0.1:8765
```

`start.sh` will:

- Create `.venv` and install `requirements.txt`
- On macOS, load `SPEECHMATICS_API_KEY` from Keychain (`service=god-mode`, `account=speechmatics`) into `.env` **without echoing the secret**
- Boot FastAPI (serves the frontend) and, in default **hybrid** mode, spawn **3 real demo worker processes** plus any live God Mode stack processes

Manual key setup:

```bash
cp .env.example .env
# put SPEECHMATICS_API_KEY=... in .env (never commit .env)
```

## Voice commands

| Say | Effect |
|-----|--------|
| `status` | Fleet summary |
| `pause fake build` | Pause worker (SIGUSR1) |
| `resume fake build` | Resume (or restart if dead) |
| `redirect research to docs` | Set worker target |
| `kill log spam` | **Arm** kill (does not kill yet) |
| `confirm kill` | Execute armed kill (PID goes away) |

Workers: **Log Spam**, **Fake Build**, **Fake Research** — real OS processes with real PIDs.

## Prove-it demo script (~90s)

1. Run `./start.sh` → open UI → note three PIDs in the Workers table.
2. Click **Listen** (allow mic) or use the text box.
3. Say / type **`status`** → audit shows a status receipt.
4. Say **`pause fake build`** → status becomes `paused`, PID unchanged.
5. Say **`kill log spam`** → UI/audit shows kill **armed**; process still alive.
6. Say **`confirm kill`** → Log Spam status `killed`, PID `—`; audit shows before PID → after none.
7. Say **`resume fake build`** → back to `running`.
8. Say **`redirect research to docs`** → Target column updates; audit receipt logged.

## API sketch

- `POST /api/speechmatics/token` — mint RT JWT
- `GET /api/workers` · `GET /api/status` · `GET /api/audit`
- `POST /api/workers/{id}/pause|resume|redirect`
- `POST /api/workers/{id}/kill` then `POST .../kill/confirm`
- `POST /api/command` · `POST /api/command/text`
- `GET /api/adapters/god-mode` — live God Mode adapter describe (`status: live`, worker count)
- `GET /api/health` — includes `adapter_mode` (`hybrid` / `local` / `demo`)

## Layout

```
backend/          FastAPI, registry, Speechmatics mint, demo adapter
backend/workers/  log_spam / fake_build / fake_research processes
backend/adapters/ demo.py · god_mode.py (live local discovery) · composite.py (hybrid)
frontend/         single-page HTML/CSS/JS (Speechmatics WS client)
tests/            confirm-gate + registry pytest
start.sh          boot script
```

## Tests

```bash
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. pytest -q
```


## Local / God Mode fleet

Default adapter mode is **`ATC_ADAPTER=hybrid`**: the three demo workers stay available for the Speechmatics demo, and the live `GodModeAdapter` also lists user `simeong` processes from the God Mode stack (`GOD_STACK`, default `/Users/simeong/local-claude-offline-stack`).

- `hybrid` (default) — demo workers + discovered stack processes (`god-rt`, `campaign-harness`, `csuper`, `god-watch`, `mcp_hands`, and python/zsh/node scripts under the stack)
- `local` — discovery only; demo workers are **not** spawned
- `demo` — the original three fake workers only

Protected processes are never listed or signalled: ATC's own uvicorn on `:8765`, anything listening on `:8088`/`:8089` (god proxy), Claude.app GUI/helpers, Cursor / Grok Bot agents, `loginwindow`, and system daemons.

Pause of a discovered PID is a real `SIGSTOP`; resume is `SIGCONT`. Restart respawns demo-owned workers only; for a discovered PID, restart resumes if paused or raises a clear error.



## God Mode single steer path

`redirect` / `steer` on a discovered **God RT** worker is no longer label-only. Voice, text, and UI all hit one path:

1. Persist the clearance label (as before).
2. If the target is an **allowlisted quiet god-rt verb**, run `$GOD_STACK/god-rt <verb>` for real.
3. Speak/confirm a short summary — or an honest error if god-rt is missing/down.

Allowlisted (quiet / read-status only): `campaign_status` (aliases: status, probe), `brief`, `list` / `campaign_list`, `ready`, `next`.

Denied from ATC: `engage`, `exploit_*`, `GO`, loud promote, `campaign_init`, etc. Pause/kill remain emergency stops on the OS process. Claude / Cursor / Grok GUI stay on the discovery denylist.

Examples:

```text
steer god-rt to brief
redirect god-rt to campaign_status
ask god-rt for ready
```

Demo workers and non–god-rt discovered processes still use label-only redirect.

## Conversation memory

Voice (`/api/command`) and text (`/api/command/text`) share a short rolling session history (last ~12 turns: utterance, spoken reply, action, worker, target). The LLM tool-caller sees this block; the fast path resolves unambiguous anaphora:

| Say | Resolves |
|-----|----------|
| `pause it` | last worker |
| `do that again` | last controllable action |
| `what did you mean by that` | last spoken clearance |

`GET /api/memory` · `POST /api/memory/clear`

## License

MIT — see [LICENSE](./LICENSE).
