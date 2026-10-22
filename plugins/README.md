# Plugins

No plugin ships with muse-discord; this folder is where one goes: a
folder `plugins/<name>/` with two files.

```yaml
# plugins/example/plugin.yaml
id: example
hooks: [gate_message]
safe_in_new_servers: true   # false: off in servers until one turns it on
settings:                   # values live at plugins.example.<key>
  enabled:
    type: on-off
    default: off
    scopes: [everywhere, dms, all_servers, server, channel]
```

```python
# plugins/example/plugin.py
def register(api):
    def gate_message(ctx, msg, guild_id):
        api.log("example: saw", msg["id"])
        return None  # or a dict merged into the queue item

    return {"gate_message": gate_message}
```

`api` has `log(*args)` and `setting(key, scope)` (`scope` is
`ctx["scope"]`).

**Only install plugins you trust.** A plugin runs inside the monitor
with its full access, the bot token included.

The monitor loads a plugin once some scope turns it on
(`plugins.<id>.enabled: on` in `config.yaml`, or by DMing the bot "turn
on example"; in `/settings` only server admins can turn one off). The hooks, `ctx`
and the setting types are in PROTOCOL.md, "Plugin hooks". A failing
plugin is logged and skipped, never fatal.
