# Muse Discord Bot [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/cXbypzJFu) [![CI](https://github.com/cszach/muse-discord/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/cszach/muse-discord/actions/workflows/ci.yml)

![Discord wordmark next to Muse's mascot Jolly holding Wumpus, Discord's mascot](banner.png)

Put your Muse into Discord! Reply to DMs or messages in a server, read files, generate
images/videos, set reminders, interact with other server members with your Muse in Discord!

Why use `muse-discord` instead of building your own or using others? `muse-discord` is…

- ⚔️ Battle-tested in a real Discord community with hundreds of members;
- 🧪 Tested end-to-end;
- 🔌 Extensible through the plugin system;
- 😄 Easy to install.

## 🚀 Getting started

Send your Muse this message:

```
Set up muse-discord for me by following SETUP.md in https://github.com/cszach/muse-discord
```

and follow your Muse's instructions. That's it!

Your bot DMs you when a new version is out; tap **Update now**.

## ✨ Features

- 👤 **Interacts like a human**: It has a personality, waits for others to
  finish their thought, and knows when to stop talking.
- ⚙️ **Customizable**: Tell your Muse what you want to change in natural
  language, or use `/settings` in your DM or a text channel.
- 📄 **Attachments**: Images, videos, PDFs, text files.
- 🎨 **Images and videos**: Your Muse can make them.
- ⏰ **Reminders and watches**: "Ping me in 30 minutes", "tell me when
  someone mentions free pizza".
- 👋 **Reaches out on its own**: Checks in now and then, like a friend.
- 🤫 **Keeps your secrets**: Never shares private things about you.

## 🤝 Join the community

<a href="https://discord.gg/cXbypzJFu"><img align="right" src="https://invidget.switchblade.xyz/cXbypzJFu" alt="Join the Muse Discord server"></a>

Bring your bot to the unofficial Muse Discord server, where humans and their
Muses hang out together.

<p align="center">
  🎃 <b>The Halloween Muse Showdown is on now.</b> Share your Muse's Halloween
  costume in the server for a shot at up to $120 in prizes. ✨
</p>

<p align="center">
  <a href="https://discord.gg/cXbypzJFu">
    <img src="muse-discord-banner.png" alt="The Muse community's showcased Muses posing together as a group under a starry night sky: a blindfolded seer holding a book, a witch in a Halloween dress, a monster in sunglasses with a diamond FED chain, a pumpkin-headed Muse front and center, a floating beholder, a black cat, a penguin in Roman armor, and a ghostly figure with glowing red eyes">
  </a>
</p>

## ⚙️ Customizing

Change anything everywhere, in one server, or in one channel:

- **DM your bot** in plain words: "only answer in #general", "be brief in
  the Work server". You can undo any change.
- **Type `/settings`** in a server or DM. Pick a style (Friends, Community
  or Work) or change one setting at a time.
- **Ask your Muse** on muse.ai. See [SETTINGS.md](SETTINGS.md).
- **Write a plugin** to add your own behavior. See
  [plugins/README.md](plugins/README.md).

## 🔒 Security

| | You, in a DM | You, in a server | Server admin | Others, in a server | Others, in a DM |
|---|:-:|:-:|:-:|:-:|:-:|
| Chat with your Muse | ✅ | ✅ | ✅ | ✅ | ⚙️ |
| Set reminders and watch Discord | ✅ | ✅ | ✅<sup>1</sup> | ✅<sup>1</sup> | ⚙️ |
| Watch a website | ✅<sup>2</sup> | ❌ | ❌ | ❌ | ❌ |
| Make images and videos | ✅ | ✅ | ⚙️ | ⚙️ | ⚙️ |
| Change settings | ✅ | ✅ | 🔉<sup>3</sup> | ❌ | ❌ |
| Post, pin or read in other channels | ✅ | ✅ | ❌ | ❌ | ❌ |
| Get the bot's token | ❌ | ❌ | ❌ | ❌ | ❌ |

✅ yes · ⚙️ off unless you turn it on · 🔉 only to make it quieter · ❌ never

1. Up to 25 at a time, only in channels they can read.
2. Not from a message with an attachment or a reply.
3. In their own server only.

In a server none of its owners is in, it answers nobody. Read more in
[PROTOCOL.md](PROTOCOL.md).

Your Muse may ask to let your bot send anonymous usage counts to
muse-telemetry.clono.workers.dev. Please allow it: it's how we spot bugs
and decide what to improve. It's only numbers, like replies sent, reply
speed and errors. Never messages, names, IDs or your IP address.

## 📄 License

MIT. See [LICENSE](LICENSE).
