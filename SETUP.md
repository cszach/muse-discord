# SETUP.md: installing muse-discord

You, the owner's Muse, follow this guide. The owner does five things:
send you the request, make the Discord app, paste its token, click the
invite link, and mention the bot once. Everything else is yours. Never
ask them for IDs, YAML, Developer Mode, intents or a terminal, or
questions this guide doesn't. Send the quoted messages as written,
filling in the `<...>` parts, one message per step.

The bot's name is your own name, and by default it is simply you.
Never print, repeat or log the bot token (step 3 says how it travels).

## Step 1: Get the code (you)

```bash
git clone https://github.com/cszach/muse-discord ~/muse-discord
cd ~/muse-discord && bash bin/install.sh
```

Commands run in that directory (the "package dir"). `install.sh`
checks Python 3.10+ and jq, installs the packages (falling back to
`pip --user`, then a `.venv`), and makes the private
`~/.config/muse-discord/incoming/` for step 3. Fix what an `INSTALL
FAILED` line says and rerun it. If it made a `.venv`, run the package's
scripts with `.venv/bin/python3` wherever these guides say `python3`.

Already installed (`config.yaml` exists)? Don't onboard again: run
`python3 bin/doctor.py --no-dm` and fix what it reports.

## Step 2: The Discord app (owner)

If your name contains "discord", Discord refuses it: use
"<your name> Bot" in the message instead. Send:

> Let's make your bot's Discord account. It takes about 3 minutes, on a
> computer.
>
> 1. Open https://discord.com/developers/applications and sign in.
> 2. Click **New Application** (it may say **Create App**), type
>    **<your name>**, tick the box, and click **Create**.
> 3. In the left menu click **Installation**. Under **Install Link**
>    choose **None**, then click **Save Changes**.
> 4. Click **Bot** in the left menu, then **Reset Token**, then **Yes, do it!**
>    (Discord may ask for your password or 2FA code). Click **Copy** and
>    paste it here. It's the bot's password; I'll lock it away.

Don't offer a secret vault or secure field instead: a vault only hands
you a stand-in, and the bot needs the real token.

## Step 3: Connect (you)

Write the paste, exactly as it came, to
`~/.config/muse-discord/incoming/token` with your file-writing tool.
Never put the token in a shell command (not as an argument, not in a
heredoc): commands show in the process list. Then run:

```bash
python3 bin/connect-discord.py --name "<your name>" \
    --token-file ~/.config/muse-discord/incoming/token
```

It reads and deletes that file, checks the paste, reads the app and
owner from Discord, turns on the Message Content intent itself, saves
the token (mode 600), writes `config.yaml` (an existing valid one is
kept, or backed up before the owner is added), writes your default
persona to `persona.md`, and prints the invite link. Exit status:

- **0**: done. Go on.
- **2**: wrong paste or a reset token; nothing was saved. Send the
  owner its `PROBLEM:` line as is (it says what to copy instead),
  then write the new paste and run it again.
- **3**: Discord was unreachable; nothing was saved. Wait a minute,
  write the paste to the file again (it is gone) and run it again.
- **4**: saved, but the owner has a portal step. Send each `OWNER
  STEP:` line; when they say it's done, run it again with `--saved`
  instead of `--token-file`.
- **6**: `config.yaml` is invalid or could not be written. Run
  `python3 bin/settings.py validate` and fix what it names. Then run it
  again: with `--saved` if the output says the token is saved, else
  with the paste written to the file again.

A `NOTE:` about a Discord team or about owners already in
`config.yaml` means the owner it set may not be them: ask "Is your
Discord username @<name in the note>?" and fix `owners` if not.

Then send (with the `INVITE:` link):

> Got it, and it's stored safely.
>
> Now add the bot to your server: click this link, pick your server, and
> click **Authorize**: <invite link>
>
> (No server yet? In Discord, click **+** at the bottom of the server
> list, then **Create My Own**, then come back to the link.)

## Step 4: Wait for the invite (you)

```bash
python3 bin/connect-discord.py --wait 900
```

It says which server the bot joined and how many channels it can read
and post in. A `PROBLEM:` line (it sees no channel there): send it to
the owner. Exit 4 (it can read but post nowhere): ask the owner to let
its role send messages in a channel. Exit 5 (timeout): remind them of
the link once.

## Step 5: The worker and the hooks (you)

Render the worker prompt, saying what you can generate (`none`,
`images`, `video`, or `images,video`):

```bash
python3 bin/render-worker-prompt.py --media <what you can make>
```

It writes `worker-prompt.md` (re-rendered on every update; edit it only
inside a seam, when the owner asks). Then register these four hooks,
all at once, with the package dir's absolute path for `<pkg>`:

| Script | Every | Deliver to | Wake prompt | Without it |
| --- | --- | --- | --- | --- |
| `hooks/mention-watch.sh` | 5 s | a dedicated worker chat, not the main one | `You are muse-discord's reply worker. Read <pkg>/worker-prompt.md and follow it exactly; the wake payload below is this run's work.` | Nobody gets an answer, and a crashed bot waits up to 10 minutes for a restart. |
| `hooks/watchdog-hook.sh` | 10 min | the owner's main chat | `muse-discord's watchdog raised an alert (payload). Fix it with <pkg>/TROUBLESHOOTING.md, and tell the owner in a sentence or two only if they must do something.` | A stuck bot is never restarted, alerts reach nobody, and new releases are never noticed. |
| `hooks/update-watch.sh` | 60 s | the owner's main chat | `The owner pressed "Update now" on muse-discord's update DM. Update <pkg> to the release in the payload by following <pkg>/UPDATING.md with the owner in this chat. When done, or if you stop early, delete <pkg>/update_request.json and report the version running, or what waits on the owner.` | The bot's **Update now** button does nothing. |
| `hooks/watch-due.sh` | 60 s | a dedicated checker chat, not the main one or the worker's | `You are muse-discord's web watch checker. The wake payload below is your whole task: follow it exactly.` | Web watches are never checked: each ends after a few tries and tells the person who set it. |

If the runtime can limit a chat's tools, give `hooks/watch-due.sh`'s
chat only web search and fetch and one command, by exact prefix:
`python3 <pkg>/bin/watch-report.py ` (with a `.venv`:
`<pkg>/.venv/bin/python3 <pkg>/bin/watch-report.py `).

The scripts find the package from their own location (set
`MUSE_DISCORD_DIR` if the runtime copies them). A runtime without hooks
runs a poller per row (PROTOCOL.md is the contract), or cron for the
watchdog.

Save this memory note in your main chat: "muse-discord is installed at
`<pkg>`; run its scripts with `<python3, or <pkg>/.venv/bin/python3>`.
To change its settings, read `<pkg>/SETTINGS.md` first. To update it,
follow `<pkg>/UPDATING.md`. To check it, run `<pkg>/bin/doctor.py
--no-dm`."

## Step 6: Start it and prove it (you, then owner)

```bash
bash watchdog.sh          # starts the monitor and presence
python3 bin/doctor.py     # checks everything, then DMs the owner
```

`doctor.py` prints `OK`, `NOTE` or `PROBLEM` lines, each problem with
its fix. Fix them and rerun until it ends with "All good." Only a run
with no problem DMs the owner "I'm online" (with the community
invite), so they get it once. Then send:

> Last step: in any channel of your server, type **@<your name> hello**,
> pick the bot from the list that pops up, and send it.

```bash
python3 bin/doctor.py --wait-mention 600
```

On a `PROBLEM`, send the owner its fix in plain words and run it again.
On `OK`, send:

> It works! <your name> is in your server now. It answers when someone
> @mentions it or replies to it, keeps chatting for a couple of minutes
> with whoever it just answered, answers DMs only from you, and never
> shares private things about you. To change anything, DM it in plain
> words ("only answer in #general") or type **/settings**.
>
> One more thing: in about a day I may ask to let <your name> send
> anonymous usage counts to muse-telemetry.clono.workers.dev. Please
> allow it: it's how the muse-discord team spots bugs and decides what
> to improve. It's only
> numbers, like replies sent, reply speed and errors. Never messages,
> names, IDs or your IP address.

That's the end of setup. Later, settings changes asked of you follow
SETTINGS.md, and problems TROUBLESHOOTING.md.
