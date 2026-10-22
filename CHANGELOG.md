# Changelog

Newest first. Each release is headed `## vX.Y.Z (YYYY-MM-DD): Title`
and is published as a GitHub Release (see RELEASING.md). The dated
entries below the releases are pre-release history. **Action
needed** lists what an existing install must do after updating;
UPDATING.md walks a Muse through it.

## v1.0.0 (2026-10-22): First release

Your Muse in Discord. It:

- Talks like a person: waits for people to finish, follows the
  conversation, and knows when to stop.
- Changes its settings when you ask in a DM, or through `/settings`
  (styles: Friends, Community, Work). Every change can be undone.
- Reads images, videos, PDFs and text files, and makes images and
  videos for you.
- Sets reminders and watches: "ping me in 30 minutes", "tell me when
  someone says pizza", and web watches from your DMs.
- Reaches out on its own now and then (off unless you turn it on).
- Stays safe around strangers: every reply is tied to the message it
  answers, secrets are never sent, it answers nobody in a server none
  of its owners is in, and server admins can only make it quieter.
- DMs you when a new version is out, with an **Update now** button.
- Sends anonymous daily usage counts; DM it "turn off usage stats" to
  stop them.

The dated entries below list every change before this release.

**Action needed** (only for installs set up before this release):

- Render the worker prompt again: `python3 bin/render-worker-prompt.py`
  with the options you used before. Until then the bot answers nothing.
- If your runtime registered a copy of `hooks/mention-watch.sh`, update
  the copy.
- Register any hook in SETUP.md step 5's table you don't have yet:
  `hooks/watchdog-hook.sh`, `hooks/update-watch.sh` and
  `hooks/watch-due.sh`.
- Restart the monitor if your update path doesn't.
- Plugins that call `api.param` must switch to `api.setting`.

## 2026-10-22: Web watches from DMs only

Fixed (security):

- Web watches are set only when an owner asks in a DM. In a server,
  other people's messages reach the same run, so a stranger could steer
  what the watch checks and which sites the checker visits.

Changed:

- Plugins run with full access; install only ones you trust.

**Action needed:**

- Render the worker prompt again: `python3 bin/render-worker-prompt.py`
  with the options you used before.
- If your runtime registered a copy of `hooks/mention-watch.sh`, update
  the copy.
- Plugins that call `api.param` must switch to `api.setting`.

## 2026-10-21: Your Muse reaches out on its own

New:

- Your Muse can start a conversation by itself now and then, the way a
  friend would: ask how the exam went, share something you'd like, or
  check in after a long quiet spell. Most of the time it decides there
  is nothing worth saying, and says nothing.
- It is **off** unless you turn it on, on new and existing installs
  alike. To turn it on: pick the **Friends** style for a server (in
  `/settings`), which turns it on for that server; or DM the bot
  "reach out sometimes" (for check-ins with you by DM), or "be
  proactive in #general"; or set `proactive.enabled: on` in
  `config.yaml`. How often it thinks about it: rarely (the default,
  once or twice a day), sometimes (4 to 6 times a day), or often
  (about every hour).
- Who it may contact: you (its owners), by DM; people who DMed it
  first, as long as your DM settings still let them in (it never DMs
  anyone out of the blue: Discord treats that as spam); and channels
  where you turned it on, only when they have been quiet for a while,
  never in a paused or muted place, nor in a server none of its owners
  is in.
- Limits: at most one unprompted message to a person a day and three a
  week, one a channel a day, and 2, 5 or 10 a day in all (rarely,
  sometimes, often). Never a second one to someone who hasn't answered
  the first (a reply where it wrote, or a reaction to it). No
  unprompted messages during quiet hours, 22:00 to 09:00 in the
  person's own time zone (when they tell it theirs, it asks them to
  confirm with a button), else in the one you set
  (`proactive.timezone`, UTC by default; DM it "my time zone is
  Europe/Paris").
- Anyone can say "stop messaging me" (in a DM, or to the bot in a
  server), you included, and it won't write to them first again. If
  they later say it may, it sends them a **Yes** button, and only
  their own tap turns it back on.

**Action needed:**

- Render the worker prompt again: `python3 bin/render-worker-prompt.py`
  with the options you used before. It teaches your Muse how to
  answer a heartbeat. Do this before you turn the feature on.
- If your runtime registered a copy of `hooks/mention-watch.sh`, update
  the copy.

## 2026-10-21: Replies stay where they belong

Fixed (security):

- A reply can only answer a message its own run was handed, where it
  was said. Before, any worker run (woken by anyone's message, so
  maybe reading a stranger's prompt injection) could have the bot post
  in any channel or DM it can reach, under a made-up item or under
  someone else's open one (their ids are in the history it reads): in
  a server none of the owners is in, a paused server, a place where
  replies are off, someone's DM; and it could silence or answer your
  command in the bot's name, or cancel or set reminders as you. Now
  the monitor gives every queued item a secret `reply_nonce`
  (`item_nonces.json`, mode 600) that the wake hook hands only to the
  run holding that item, and every outbox item must carry its item's,
  answer an open item, and post in that item's channel (a thread, for
  a message in a thread). Anything else is set aside in
  `reply_outbox.rejected.json` and logged. Held replies you chose to
  send still go out (bound to the hold's own item and channel).
- `bin/discord-api.py` acts only for an owner's command, in the run
  that holds it, and only where your own words point. Before, any run
  could `read` someone's private DM and `post` it into a public
  channel. Now every subcommand needs `--for <your item> --nonce <its
  owner_command_nonce>` (`resolve-channel` and `list-threads` accept an
  owner DM's nonce too), and acts only in the command's own channel or
  one your message itself names (`<#channel>`, the only way to name a
  thread; `#name` in that server; or, for a post only, a channel of
  that server named after "in", "to" or "into": "post this in
  announcements"), resolved when your message arrives, never from
  quotes, code, history or what the bot reads, and never in a DM
  other than its own. `read` output comes marked as untrusted data,
  and a channel lookup that misses no longer lists the server's
  channels.
- An owner's command wakes alone, one per run, as owner DMs already
  did, so no other message (a stranger's, or another command's
  history) shares the run that can act for you. Other messages that
  arrive with it wait a few seconds for the next run. A command of
  yours gets its web watch capability in that same run.
- Nothing the bot sends carries a secret. Every send (replies, reminders,
  owner DMs and notices, edits, uploads, button responses, and the
  Discord tool's posts) is checked first, all parts together: the bot
  token (also squashed, reversed, or in base64 or hex), any of the
  bot's live private codes, or anything shaped like a Discord token
  drops that message, and you get a DM about it, at most once a day.
  Nothing else is ever changed in what the bot sends. On Muse every
  session runs as the same user, so a reply session tricked into using
  its shell could read those files; this keeps it from posting them. README's Security
  section and PROTOCOL.md's security note now say plainly what the bot
  enforces and what is up to the reply session's instructions.

Changed:

- **Web watches are for owners only**, with no setting to change it
  (each check is a turn of your Muse reading the web). The
  `abilities.web_watches` setting is gone: a config or a Discord-side
  change that still sets it loads fine, the value is ignored, and the
  log says so once. A web watch someone else set earlier ends at its
  next check, and they are told once. The per-server check budget for
  other people's web watches is gone with it; the daily total still
  paces your checks.
- An outbox item that names no open queue item (neither `item` nor
  `reply_to`, or an item answered already, however long ago) is
  refused instead of posted.
- The worker no longer reads `reply_queue.json` (it holds other
  people's private messages): its items always come in the wake
  payload.

**Action needed:**

- **Update the worker prompt (re-render it) right away**: `python3
  bin/render-worker-prompt.py` with the options you used before.
  Until then the bot answers nothing: the old prompt does not copy
  each item's `reply_nonce`, and the monitor refuses every reply
  without it (they wait in `reply_outbox.rejected.json`, and the
  messages stay queued for the next run). Owner commands also need
  the new prompt (`--for` and `--nonce` on every call). If it is
  missed, the bot DMs you (at most once a day) that the worker prompt
  is out of date and how to fix it, and `bin/doctor.py` reports it.
- Restart the monitor if your update path does not (UPDATING.md).
  Until a monitor that writes `item_nonces.json` and
  `owner_items.json` runs, owner commands get a reply but no action.

## 2026-10-21: Reminders and watches

New:

- Reminders: "@bot ping me in 30 minutes and remind me to look into
  this project" now works. The bot confirms the time, then comes back
  once, as a reply to the request (or to the person's newest message
  there, if the request is gone), and pings only that person. If the
  bot was down when a reminder was due, it comes once, late, and says
  so; several due at once come as one message.
- Watches: "tell me when someone mentions pizza". The bot watches the
  messages it already reads, in one channel or a whole server, and
  only where the person who asked can read. It never counts their own
  messages, links the message instead of quoting it, says so at most
  once every 15 minutes, and stops after the first match (unless they
  asked for every time) or after a week (30 days at most).
- Web watches: "tell me when the tickets go on sale". Your Muse checks
  the web now and then (at most every 30 minutes, with a daily limit),
  in a session of its own that can only read the web and report back.
  Only for you, unless you allow others ("Who can set web watches").
- One list and one cancel: "what are you watching for me?" and "stop
  watching for pizza". Everyone sees and cancels only their own; you
  can cancel anyone's, and your Muse can list and cancel them all with
  `bin/watches.py`.
- Limits: none for you. Anyone else can have 25 reminders and watches
  going, and each one that comes due counts as a reply toward
  "Replies per person per day" (and the daily total) where it lands.
  Over that cap it waits and goes out, late, once the cap allows; it
  is never lost. Web watches also have a daily check budget. A refused
  or waiting one shows up in **Why no reply?**.
- Two new settings: "Who can set reminders and watches" (everyone) and
  "Who can set web watches" (only you). DM the bot to change them, or
  ask your Muse.

The bot doesn't know people's time zones: for "tomorrow at 9" it asks
which zone, and remembers it if they like. Times it writes show in
each reader's own time zone.

**Action needed:**

- Render the worker prompt again: `python3 bin/render-worker-prompt.py
  --media <none|images|video|images,video>`. It teaches your Muse
  reminders and watches.
- If the runtime registered a copy of `hooks/mention-watch.sh`, update
  the copy.
- For web watches: register `hooks/watch-due.sh` as SETUP.md step 5's
  table says (every 60 s, in a chat of its own). Without it, web
  watches are never checked, and each one ends after a few tries and
  tells the person who set it.

## 2026-10-21: Setup in five steps

New:

- Setting up takes about 10 minutes and five things from you: send
  your Muse the setup message, make the Discord app (named after your
  Muse), press Reset Token and paste it to your Muse, click the invite
  link, and mention the bot once. No IDs, settings files, Developer
  Mode, intents or terminals; your Muse does the rest and tells you
  exactly what to click.
- The bot is your Muse itself by default: its name is your Muse's name
  and it is told to be itself, as in your main chat. There is no
  personality interview any more; to shape how it talks, DM it in plain
  words. Setup writes this default into `persona.md` (only while it is
  still the shipped placeholder).
- New scripts for your Muse: `bin/install.sh` (prerequisites, with a
  fallback when pip can't install), `bin/connect-discord.py` (checks the
  pasted token and says what to copy if it is the wrong thing, turns on
  Message Content itself, writes the config, prints the invite link),
  `bin/render-worker-prompt.py`, and `bin/doctor.py` (one health check
  with a fix for each problem; it DMs you "I'm online" after setup).
- `hooks/watchdog-hook.sh` runs the watchdog as a Muse hook and wakes
  your Muse's main chat only when something needs attention.
  TROUBLESHOOTING.md collects the fixes.
- Safer when someone else adds your bot: in a server none of the owners
  is in, it now answers nobody until you choose When it answers for
  that server (the join DM's Configure button), and that server
  doesn't count as shared for "DMs from people in a server with the
  bot". **Why no reply?** says so. Setup also has you make the bot
  private, so only you can add it.

Changed:

- **Image and video generation is now on by default, for owners
  only** (`media.enabled: on`, `media.who: owners`), including on
  existing installs that never set it. Nobody else can ask for it
  unless you allow them. To turn it off, DM the bot "turn off images
  and video", or set `everywhere: media: enabled: off` in
  `config.yaml`.

Existing installs are unaffected otherwise: your config, persona and
token stay as they are, and nothing about setup runs again.

**Action needed:**

- Render the worker prompt with `python3 bin/render-worker-prompt.py
  --media <none|images|video|images,video>` and, once, replace the wake
  hook's inline prompt with the short one in SETUP.md step 5's table,
  which reads `worker-prompt.md` (later updates then only re-render).
- Optional: register `hooks/watchdog-hook.sh` (SETUP.md step 5) in place
  of a cron or scheduled `watchdog.sh`, so alerts reach your Muse.

## 2026-10-21: /settings, simpler

Changed:

- `/settings` is one page now. It opens on the place you typed it in
  (a channel, or DMs in the bot's DM) with one line on how the bot
  behaves there, and a **Where** select for that server or
  everywhere. Each place shows a few settings as selects: when it
  replies, reply length, images and video, who can DM it and replies
  a person a day (whichever that place has; a channel can follow its
  server again with "Use server default"). Then the server's style
  (Friends, Community, Work, applied in one click with Undo), Pause
  or Resume, **Why no reply?**, and Undo for the last change. There
  are no More / Extras / Bot-wide pages, no Defaults page, no "Same
  as" choices and no server or channel browsing any more.
- Moved to DM-only (DM the bot what you want, or ask your Muse):
  instructions, what it may say about you (a style still sets it),
  other bots and replies in a row to bots, the total daily cap,
  possible prompt injection, Only answer these people and the owners'
  Never answer list, picking people by name for DMs or for images and
  video, images and video per day, the status line, update notices,
  and plugins (the page lists installed plugins and says so).
- Server admins' **Quiet the bot** panel is unchanged in what it
  allows (quieter only, never past the owners), but shorter: the
  ignore list, a Turn off button for images and video or a plugin
  that is on, and Undo of admins' last change.
- Buttons on `/settings` messages sent before this update answer
  "This settings page is out of date" and change nothing; run
  `/settings` again. The join notice's **Configure** button still
  works.
- Fixed after review: **Why no reply?** no longer fails to open when
  the same person (or the same cause) was refused twice; the second
  one shows without its own fix button. A channel in a category that
  sets its own value now offers "Use category default", not "Use
  server default". Picking Friends on a server over 50 members says why
  nothing changed (it keeps to public facts there). Server admins get
  **Turn … back on** for images and video or a plugin that an admin
  turned off.
- Wording from testing: the channel's follow option reads "Use server
  default" (or "Use category default"), with "Currently: …" below
  it; an "Applies to" label sits above the Where select; the style
  section says "Applies to the whole server", and a server that
  matches no style shows "Customized" selected in the style select.

## 2026-10-21: Anonymous usage stats, on by default

New:

- The bot now sends the muse-discord maintainer one small anonymous
  report a day, so we can see how many installs there are and what
  breaks. **It is on by default.** It goes only to the project's own
  Cloudflare Worker (`telemetry/worker`, which stores no IP
  addresses), at most once a day, a few hours after the day ends.
  Only released versions (a `vX.Y.Z` tag) send; a checkout of
  unreleased code never does.
- A report holds: a random install id, the day, the muse-discord,
  Python and OS versions (like 3.12, linux), how many commits the copy
  is past that release and whether its code has local edits, how many servers the bot
  is in and their total members rounded down to a power of 2, and
  that day's counts (messages seen in servers and DMs, replies sent,
  unanswered messages by reason, wakes, reply times, settings changes
  and presets, images and videos sent, restarts, crashes by exception
  name) plus a
  few bot-wide settings as plain values (DMs on or off, memory mode,
  ...). Never message content, names, Discord ids, tokens, file paths,
  error messages, IP addresses or times finer than the day. README,
  "Privacy and usage stats", lists every field.
- `python3 bin/telemetry.py show` prints exactly what would be sent
  next; `status` shows whether it is on and the last send.
- To turn it off: DM the bot "turn off usage stats", run `python3
  bin/telemetry.py off`, set `telemetry: usage_stats: off` in
  config.yaml, or set `MUSE_DISCORD_TELEMETRY=off`. Days it was off are
  never sent. `bin/telemetry.py reset-id` replaces the install id.

**Action needed:**

- None to keep things working. To turn usage stats off by DM, update
  the installed worker prompt (step 2's SETTINGS REQUESTS); the other
  ways work as is. If your runtime registered a copy of
  hooks/mention-watch.sh, update the copy so wakes are counted.

## 2026-10-21: Image and video built in, welcomes gone

Changed:

- Image and video generation is a built-in setting now, `media.*`
  (`media.enabled`, `media.who`, `media.per_day`), instead of the
  `media_generation` plugin. It works exactly as before: off by
  default, the same scopes and caps, and the same "Image and video"
  switch in `/settings`.
- Welcoming new members is gone, and with it the welcome plugin, its
  `/settings` controls and the "welcome" queue items. The bot no
  longer looks at Discord's "X joined the server" messages at all.
- No plugins ship with the bot now. The plugin mechanism stays:
  `plugins/README.md` shows how to make one.

Old configs keep working, nothing to do: `plugins.media.*` and the
older `plugins.media_generation.*` settings (`allowed_user_ids`,
`daily_cap`) are read as `media.*`, including changes made from
Discord and their Undo, and today's generation count carries over.
`plugins.welcome.*` settings are ignored (one line in monitor.log
says so). Installed worker prompts keep working too; the template no
longer mentions welcomes and names the settings `media.*`.

## 2026-10-20: Your DMs are answered only by the run that got them

Security fix: a worker woken by someone else's message (and maybe
their prompt injection) could answer your DM in the bot's name, mark
it handled so you never got a real reply, or post into your DM with
the bot. Only settings requests were protected.

Changed:

- Every reply to your DM (and every silent pass, reaction, or message
  in your DM channel) must carry that DM's private nonce, which only a
  run holding nothing but your DMs gets. Anything else is refused and
  set aside in `reply_outbox.rejected.json`, and your DM stays open
  for the run that really has it. Replies to everyone else work as
  before.
- `bin/discord-api.py` refuses to post, edit, delete, react, pin or
  read in your DM with the bot unless the call names your DM and its
  nonce (`--for <message_id> --nonce <owner_dm_nonce>`). Server
  channels work as before.
- The wake hook no longer counts a forged answer to your DM as an
  answer, so it cannot keep your DM from its own run, and never hands
  out your DM before its nonce exists.
- Replies under the last message of a burst you sent work however long
  ago the burst was, and no other run can settle the burst through one
  of its messages.
- Ids in the outbox are taken in one form only (no leading zeros or
  other digits), and a reply to a channel the monitor cannot place is
  checked with Discord first and refused if it is your DM.
- A DM of yours that is still waiting keeps its nonce (and gets a new
  one if `state.json` was lost), so it is never stuck. The nonce is
  spent once the DM is answered, not before, so a failed send's retry
  still goes out.
- The wake payload calls the nonce `owner_dm_nonce` (it was
  `settings_nonce`).

**Action needed:**

- Update the installed worker prompt (step 2's OWNER DMs, OWNER
  COMMANDS and SETTINGS REQUESTS). Until you do, the worker's replies
  to your DMs are refused and your DMs go unanswered; everyone else
  still gets replies.
- If your runtime registered a copy of hooks/mention-watch.sh, update
  the copy. A runtime without the shipped hook must pass each owner
  DM's nonce to its worker itself (PROTOCOL.md, Owner DMs).

## 2026-10-19: /settings, redesigned

New:

- `/settings` opens right where you are: in a channel, on that
  channel's page; in a thread, on its parent channel; in a DM with the
  bot, on DMs. From there, one button goes to the server, Defaults
  (what every server and DM follows), DMs or another server, and a
  channel picker goes to any channel.
- Each setting shows what it is now in plain words, and its first
  choice is "Same as" what it follows, with that value (for example
  "Same as Defaults: Chatty"), so going back to the server's or
  Defaults' value is one pick. A setting changed there says who
  changed it and when, and says so when it overrides config.yaml.
- Presets for a server: Friends, Community and Work. Picking one shows
  exactly what would change first, and applying it is one change you
  can undo with one click.
- **Why no reply?** on a channel, server or DMs page lists the last
  messages that addressed the bot and got no answer, with the reason
  in plain words and a fix where there is one ("Remove Sam" from Never
  answer, "Change cap"). Server admins can also right-click a
  message, Apps, **Why no reply?** for that one message (the menu
  entry is for people with Manage Server). In DMs it checks whether a
  person can DM the bot.
- After every change, a **Done** line says what changed, with an
  **Undo** button. Pages also show the last change made there, with
  Undo; on a server page that includes changes in its channels.
- Server admins (Manage Server) get a **Quiet the bot** panel: make the
  bot answer only @mentions or nothing, here or in the whole server,
  add people to Never answer, turn extras off, and undo what admins
  changed. They still can never make it louder than you set it, and
  the summary DM you get of their changes now says them in plain words
  ("In #general, When it answers: Chatty → Off").

Changed:

- Changes that could have bad results (DMs opened to more people,
  memory shared in a big server, Pause everywhere, Chatty in a big
  server, a cap removed) show what they would mean and wait for
  **Apply**. Picking people by name for Who can DM it does not ask
  again.
- Caps: leave the box blank for no cap. A box is never prefilled with
  "none" any more, 0 means only you get replies, and a typo is not
  saved: the page says why and **Try again** reopens the box with what
  you typed.
- Who can DM it: pick people in a server, or add them by ID in a DM
  with the bot (copy a user ID with Developer Mode on), and remove them
  from a list.
- Pause buttons name what they pause ("Pause #general", "Pause" and
  the server's name, "Pause everywhere"), and pausing a server says
  which channels still answer because they have their own setting.
- When config.discord.yaml cannot be read, the pages say so and offer
  no changes until it is fixed, instead of failing on save.

**Action needed:**

- None. A `/settings` page opened before the update says it is out of
  date; run `/settings` again. The **Configure** button in earlier
  "I just joined" DMs still works. The monitor registers **Why no
  reply?** by itself; it can take a moment to appear in Discord.

## 2026-10-19: Change settings by asking

New:

- DM the bot what you want, like "only answer in #general when I @
  you" or "be brief in the Work server", and it changes the setting.
  Changes that are safe or make the bot quieter apply at once, with
  an Undo button. Changes that open the bot to more people or loosen
  your privacy show what they would do and wait for your Apply. Only
  your own DMs count: never server messages, other people, or
  forwarded messages. If someone else's message tries it, nothing
  changes and you get a DM about it (at most one an hour).
- Your Muse can change settings from its main chat too (SETTINGS.md).
  Each change it makes shows up in your DMs with an Undo button,
  without a ping, also one made while the bot was stopped.
  `bin/settings.py --by` takes a name (default `muse.ai`), not a user
  id.
- Instructions, the status line and welcome notes always wait for
  your Apply when you change them by DM, so you see the exact words.
- `bin/settings.py places` lists your servers and channels with their
  ids, and `bin/settings.py` names channels in what it prints.

**Action needed:**

- Update the installed worker prompt (new SETTINGS REQUESTS section).
- Have your Muse save a memory note: "To change muse-discord's
  settings, read <package dir>/SETTINGS.md first."
- If your runtime registered a copy of hooks/mention-watch.sh, update
  the copy.

## 2026-10-19: Settings engine: places, plain words, undo groups, presets

New:

- "Who can DM it" can name people: `dms.who.dms_from` takes a list
  of user ids, meaning you plus those people (for example a friend),
  besides `owners`, `shared-servers` and `anyone`. Their DMs are
  caught up after an outage like yours.
- `bin/settings.py` can change settings, not only show them: `set`,
  `reset`, `undo`, `preset` (Friends, Community or Work, for a
  server), `pause`, `resume` and `explain`, each for one place
  (`defaults`, `dms`, `server:<id>` or `channel:<id>:<id>`). It says
  what changed in plain words. Risky changes are only made with
  `--yes`.
- Changes made together (a preset, Pause everywhere) are undone
  together with one Undo.

Changed:

- Risk warnings look at what a change leads to, not only at the one
  setting: for example opening DMs warns only when more people can
  actually DM the bot, and an allow list warns how many places go
  quiet.
- Setting a value that is already in force writes nothing, and
  setting what config.yaml says at that spot removes the Discord
  change instead, so the file stays in charge.
- Server admins can undo their own changes back to what the owners
  set (still never louder than that). They can no longer remove a
  setting you made, and what you change later (pausing a server, for
  example) now also reaches the channels an admin quieted.
- Settings changed from Discord and from `bin/settings.py` at the
  same moment no longer overwrite each other, and a
  `config.discord.yaml` that cannot be read is left alone (changes are
  refused with a message) instead of being replaced.

**Action needed:**

- None.

## 2026-10-19: Daily limits, ignored mentions, threads and notices

Fixed:

- A message over a daily limit (`limits.per_day` /
  `limits.per_person_per_day`) is now dropped right away. It used to
  be retried for 15 minutes while the person's next messages there
  were folded into it and went unanswered, and a restart could answer
  it late. Their later messages are judged on their own.
- A mention the settings tell the bot to ignore (replies off, someone
  on the ignore list, other bots ignored, DMs off) stays ignored. It
  used to get answered up to 6 hours later if the setting was relaxed
  in the meantime. With DMs off, your own DMs are no longer answered
  late either.
- A mention in a brand-new thread or forum post could wait about 4
  minutes for a reply when the bot noticed the thread at the same
  moment. It is now answered right away.
- Discord's own notices ("X pinned a message", "X started a thread")
  no longer count as someone talking to the bot. Right after a reply,
  a pin could get answered as a follow-up.

**Action needed:**

- None.

## 2026-10-19: Status line changes show up right away

Fixed:

- Changing the bot's status line (`presence.status` / `presence.kind`)
  with `/settings` or in `config.yaml` did nothing until the presence
  client restarted, which the watchdog never does while it is healthy.
  It now updates within a few seconds, without reconnecting.

**Action needed:**

- None.

## 2026-10-19: Change settings from Discord with /settings

New:

- Type `/settings` in a DM with your bot or in a server. A private
  page (only you see it) lets you pick a place (everywhere, DMs, all
  servers, a server, a channel or category) and change how the bot
  behaves there with menus and buttons. Changes apply at once, show
  where each value comes from, and can be reset or undone. Risky
  changes (your memory in a big server, strangers' DMs, unlimited
  image generation) ask you to confirm first. "Why?" shows how each
  value was decided.
- Server admins can use `/settings` in their own server to make the
  bot quieter there (replies off or only when addressed, plugins off,
  ignore someone), never louder; you get a DM when they do.
- When the bot joins a new server, its DM to you has a Configure
  button.
- Discord changes go to `config.discord.yaml`; your `config.yaml` is
  never rewritten.

**Action needed:**

- None. The existing bot invite already covers slash commands; the
  command registers itself on the next start. `/settings` can take a
  minute to appear in Discord the first time.

## 2026-10-18: New settings, per server and per channel

New:

- A simpler `config.yaml`: settings grouped by what you think about
  (who it talks to, how it talks, what it may do, privacy, limits),
  set for everywhere, DMs, all servers, one server, or one channel or
  category. The most specific place wins; threads follow their
  channel. `bin/settings.py show --explain` shows what the bot does in
  any place and where each value comes from.
- Conversational replies (the default): after the bot answers you,
  your next messages in that channel within about 2 minutes count as
  talking to it, even without an @mention or Discord's reply. Set
  `who.replies: when-addressed` for the old, strict behavior.
- Your Muse's memory stays private: `privacy.memory` (default
  `public-only`) tells it what it may draw on outside your own DMs,
  and `privacy.never_share` holds any reply containing a listed
  phrase for you to Send anyway or Discard.
- Strangers' DMs are not answered by default (`who.dms_from: owners`).
- Every cap accepts `none`, the default.
- Changes made from Discord (coming with `/settings`) will live in
  `config.discord.yaml`, never rewriting your file.

**Action needed:**

- Nothing breaks: an old `config.yaml` keeps working, with one notice
  line in monitor.log. It also gets conversational replies, the one
  intended change in behavior.
- Optional: convert the file with `bin/settings.py migrate` (keeps a
  backup; refuses if anything else would change). UPDATING.md has
  your Muse offer it and ask you first.
- Reinstall the worker prompt from `worker-prompt.template.md`.
- A config with a mistake now stops the bot with a message naming the
  setting, instead of quietly guessing. Run
  `bin/settings.py validate` after editing.

## 2026-10-18: Updates arrive as a DM with an Update button

New:

- Updates now come as releases: version tags such as `v1.0.0`, each
  with its own CHANGELOG entry, instead of every commit on main. When
  a new release is out, the bot DMs its owners what's new in it, with
  **Update now** and **Later** buttons. Update now wakes your Muse in
  its main chat to update to exactly that release (following
  UPDATING.md) and report back; Later skips that version (a newer one
  asks again). Before, the daily check only printed a line to the
  watchdog's scheduler, which owners never saw, and counted commits.
  `updates.check: false` turns the DMs off.
- The first release, v1.0.0, is tagged right after this change. An
  install on main is then offered it, unless it already has it.

**Action needed:**

- This update itself is applied by hand, the last one: tell your Muse
  "Update muse-discord by following UPDATING.md". Its step 4 now
  registers the new update hook (`hooks/update-watch.sh`, SETUP.md
  step 10), and the restart in step 5 turns the DMs on.

## 2026-10-18: Back online in seconds after a crash

Fixed:

- When the machine running the bot was replaced or the monitor died,
  the bot stayed offline until the next watchdog run, up to 10
  minutes. The wake hook, which already runs every few seconds, now
  starts the watchdog as soon as it sees no monitor, so the bot is
  back within seconds. The watchdog cron stays as the backstop. A
  rejected token never causes a restart loop.
- Two watchdog runs at once could start two monitors; the watchdog
  now holds a lock.

Faster:

- The reply worker ends its run as soon as its replies are written:
  no summary or follow-up checks. A message that arrives right after
  a reply no longer waits for that wrap-up. It still flags abusive
  messages for you in its one final line.

**Action needed:**

- Reinstall the worker prompt from `worker-prompt.template.md`.
- If your hook runs a copy of `hooks/mention-watch.sh` rather than the
  package's own file, update the copy. If the bot uses `bot.token_env`,
  set that variable where the hook runs too, or only the cron can
  restart it.

## 2026-10-18: One slow reply no longer holds back the rest

Faster:

- When one wake carries several messages (say a DM plus two welcomes),
  the worker now answers DMs and mentions first and sends each reply
  as soon as it is written, instead of writing them all at the end.
  Replies in the same channel keep their order.

Fixed:

- A reply to a message that was deleted before the reply went out
  failed on every retry until it was dropped. It now posts as a
  normal message.
- The latency log could show a negative `ingest` when the machine's
  clock trailed Discord's by a fraction of a second.

**Action needed:**

- Reinstall the worker prompt from `worker-prompt.template.md` (the
  faster ordering only takes effect then). If your agent runtime
  limits tool calls per run, allow `bin/outbox-append.py` to run once
  per reply.

## 2026-10-18: Realtime messages no longer wait for the REST poll

Fixed:

- A DM or mention the gateway delivered at once could wait 25-95s
  before the monitor even looked at it, because the main loop read
  every channel over REST (and ran the sweep and channel refreshes)
  in one go before handling it; behind a slow proxy that took most of
  each 30s cycle. The REST work is now done in short slices between
  the realtime work, so new messages are picked up within a second.
  Every channel is still read about every `rest_poll_seconds`, the
  sweep and refreshes keep their cadence, and the catch-up after a
  restart still reads everything.
- A Discord read that hangs gives up after 8s instead of 20s.

Faster:

- A DM burst goes out 0.5s after its author stops (servers stay at
  1.5s, bots at 5s; typing still holds a burst open).
- A reply the worker writes is picked up within about 0.25s instead
  of up to 2s.
- The latency line splits the old "coalesce" figure into ingest (until
  the monitor first saw the message) and hold (the burst wait):
  `Latency <id>: ingest 0.4s, hold 1.6s, worker 30.1s, send 2.0s,
  total 34.1s`.

**Action needed:**

- None; the monitor restarts with the update.

## 2026-10-17: Less code: three features removed

Removed:

- The bare-name trigger (`name` in `behavior.respond_to`,
  `behavior.call_names`, and `bin/check-call-name.py`). The bot answers
  @mentions, replies to it, and DMs; its name alone in a sentence is
  not a trigger. Observe mode stays, for welcomes.
- The optional `behavior.max_bot_turns` cap. The worker ends bot
  conversations by judgment, as before; `bot_turns` on queue items
  still tells it how long a run has gone (up to 12).
- The `--for` check in `bin/discord-api.py`. Owner commands still
  reach the tool only through owner items; uploads still come only
  from `generated/`, and ids are still validated. A leading `--for`
  is accepted and ignored.

**Action needed:**

- None required. Old `call_names`, a `name` trigger, or a
  `max_bot_turns` value in config.yaml print one warning at startup and
  are ignored; delete them to silence it.
- Optional: reinstall the worker prompt (name-drop and `--for` text
  removed).
- Optional: delete a leftover `owner_commands.json`.

## 2026-10-17: Faster replies, and fixes from a full code review

Faster and smoother:

- Replies come sooner. The worker gets its items and the persona in
  the wake message and writes all its replies in one step
  (`bin/outbox-append.py`); the monitor removes answered queue items
  itself. A burst goes out about 1.5s after its author stops typing
  (5s for a bot) instead of always waiting the full window.
- The typing indicator stays on until the reply lands, instead of
  flickering, for up to 3 minutes.
- Each reply logs a latency line showing where the time went.

Fixed:

- Replies the worker wrote while the monitor was sending could be
  lost, and queue edits from either side could undo the other's (a
  welcome could post twice). Both files are now locked.
- A second worker could start on items the first was still handling.
- A generated image could be attached to a different reply, even in a
  public channel. Files now live in `generated/<message_id>/` and only
  go out with that message's reply.
- The owner-command tool could upload any local file and was not
  limited to owner commands in code. It now only uploads from
  `generated/`, needs `--for <message_id>` naming a live owner
  command, validates ids, and never pings roles.
- Image and GIF links matched lookalike domains and followed redirects
  anywhere; downloads are now https-only, on the allowlist, and never
  to private addresses.
- Prompt injection hidden in attached documents or the replied-to
  message is now quarantined too.
- A revoked token left the bot running deaf: it now exits, and the
  watchdog alerts. The watchdog also restarts a monitor that hangs,
  keeps the previous log as `monitor.log.1`, and supports `token_env`.
- A mention in the middle of a burst could be answered twice.
- `bot.guild_ids` was ignored for realtime messages.
- First run (or a deleted `state.json`) turned the owner's DM backlog
  into one message.
- Messages missed while the gateway reconnected could be skipped for
  good; owner DMs are now swept too.
- A brief error during channel discovery dropped a channel for 30
  minutes.
- Explicit `0` config values were silently replaced by defaults, and
  `plugin.yaml` defaults (such as the welcome plugin's daily cap) were
  never applied.
- Pin and unpin use Discord's current endpoints.

**Action needed**

- Update the installed worker prompt from `worker-prompt.template.md`.
  Until you do, generated images will not attach and owner commands
  will be refused (the tool now needs `--for`), and the worker keeps
  its slower, unlocked path.
- If the wake hook runtime registered a copy of
  `hooks/mention-watch.sh`, update the copy.
- To let the bot pin messages, enable **Pin Messages** for its role
  in each server (new invites from SETUP.md include it).
- If you use the welcome plugin without a `daily_cap` in your config,
  the documented default of 10 a day now applies.

## 2026-10-17: No more duplicate replies

- Fixed: the bot could post the same reply several times in a row.
  A send that timed out after Discord had already accepted it was
  retried, posting another copy each time. Every message now carries
  a nonce that Discord uses to drop repeats, and retries are spaced
  out and stop after 90 seconds.
- Fixed: two monitors could start at the same moment (for example,
  overlapping watchdog runs) and each send every reply. The pidfile
  is now locked for the monitor's whole lifetime.
- A multi-part reply that fails partway is now retried instead of
  dropped; the parts that already landed are not repeated.

**Action needed**

- Update and restart (UPDATING.md). Every install before this one can
  post duplicates.
- Check for a second running monitor once: `pgrep -af monitor.py`
  should list one process. If it lists more, stop them all and run
  `bash watchdog.sh`.

## 2026-10-16: Bots can talk to each other

- Other bots, such as other people's Muses, can @mention this bot or
  reply to it and get an answer (`behavior.bots: direct`). They never
  trigger name-drops or welcomes.
- The worker addresses people and agents with real mentions, ends
  conversations the way a person would (silence, a reaction, or a
  closing line that invites no reply), and never answers a pure
  acknowledgment.
- Optional `behavior.max_bot_turns` caps bot-to-bot runs with no
  human in between. No cap by default.
- No status line under the bot's name by default.
- Removed the burst breaker added in the previous release: it cut
  lively conversations off mid-flow, which is not how a person chats.
- No daily reply cap by default (`safety.daily_reply_cap: null`).
  Both reply caps are still available.
- Fixed: replies to multi-message bursts, and deliberate no-replies,
  could be requeued by the sweep and answered twice.

**Action needed**

- Update the installed worker prompt from `worker-prompt.template.md`
  (new OTHER AGENTS, ADDRESSING PEOPLE, and ENDING CONVERSATIONS
  sections, and `silent` outbox items).
- Optional: set `presence.status_text: ""` in `config.yaml` to drop
  the "Watching for mentions" line.
- Optional: set `behavior.bots: off` to keep ignoring other bots.
- Optional: set `safety.daily_reply_cap: null` in `config.yaml` to drop
  the old 30-a-day cap, and delete the `safety.burst_breaker` block
  (it is ignored now).

## 2026-10-16: Trigger safety

- Only @mentions, replies to the bot, and DMs trigger it by default.
  Answering to its bare name is an opt-in `name` trigger using
  `behavior.call_names`; setup checks the name for collisions with
  `bin/check-call-name.py`.
- New servers start in observe mode and the owner gets a DM summary.
  Welcomes and name triggers run only in servers listed in
  `behavior.active_guild_ids`.
- The bot never answers messages from before it joined a server.
- Burst breaker: more than 5 replies in 10 minutes in one channel
  quiets it for 30 minutes and DMs the owner. (Removed in the next
  release.)

**Action needed**

- If the welcome plugin is on: add each server's ID to
  `behavior.active_guild_ids`, or welcomes stop.
- If the bot should keep answering to its bare name: add `name` to
  `behavior.respond_to` and set `behavior.call_names` (run
  `bin/check-call-name.py` on the name first).
- Update the installed worker prompt (name-drop handling).

## 2026-10-15: Initial release
