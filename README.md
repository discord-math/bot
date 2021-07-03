# Mathematics Server Discord Bot

This is the open source repository for the utility bot that manages various
kinds of things on the Mathematics discord server. With that purpose in mind,
feel free to contribute to this repository. If you'd like to run this bot on
your own server, that's fine too, but don't expect support.

## Plugins

These are the plugins provide commands or otherwise user-visible functionality:
  - `bot_manager`: managing the bot
    - `plugins` -- list the currently loaded plugins
    - `restart` -- restart the bot
    - `load <plugin>` -- load a plugin
    - `unload <plugin>` -- unload a plugin and its dependents
    - `reload <plugin>` -- reload a plugin and its dependents
    - `reloadmod <module>` -- reload a module (that's not a plugin)
    - `unsafereload <plugin>` -- do an "unsafe" in-place reload
    - `unsafeunload <plugin>` -- unload a single plugin
    - `autoload` -- manage plugins that are to be auto-loaded on bot startup.
      `autoload` to list, `autoload add <plugin>` to add,
      `autoload remove <plugin>` to remove. Note that marking a plugin as
      auto-load doesn't load it, and loading it doesn't make it auto-load.
  - `privileges`: managing who can execute commands
  - `locations`: managing where a command can be executed
  - `eval`: run arbitrary code on the bot
    - ``eval `<some python code>` ``
  - `db_manager`: manage the bot's database
    - `config` -- list namespaces. Most namespaces are `plugin.name`.
    - `config <namespace>` -- list keys in a namespace.
    - `config <namespace> <key>` -- show the value associated to a key.
    - `config --delete <namespace> <key>` -- delete the value associated to a
      key
    - `config <namespace> <key> <value>` -- set a value. The values are
      JSON objects.
    - ``sql `<sql code>` `` -- run an arbitrary SQL expression. If any
      modification is made, we prompt to commit/rollback.
  - `discord_log`: log errors to a discord channel. Configure with
    ``config plugins.discord_log channel `"<channel id>"` ``
  - `update [name]`: update the current branch in the named git repository
     (current working directory by default). Add repositories with
     ``config plugins.update name `"/path/to/repo/"` ``. Make sure that the used
     branch has an associated upstream with
     ```sh
     git branch --set-upstream-to=origin/main main
     ```
  - `version`: display the current version using git.
  - `keepvanity`: auto-assign a vanity URL if it's ever lost. Configure the URL
    with ``config plugins.keepvanity guild `"<guild id>"` ``,
    ``config plugins.keepvanity vanity `"vanity"` ``.
  - `speyr`: provide functionality that was offered by Speyr in the past (`!r#`,
    `!15m`).
  - `pins`: allow users to `pin` and `unpin` messages without giving them
    Manage Messages permission.
  - `talksrole`: a plugin implementing the @Talks role: can be pinged only in a
    specific channel, by anyone, by including a certain string in the message.

## Running

The bot requires:
 - Python 3.9+ (!)
 - PostgreSQL 10+
 - asyncpg
 - sqlalchemy
 - sqlalchemy-orm
 - urrllib
 - discord.py
 - discord-ext-typed-commands

You will need to create a static config file called `bot.conf` in the working
directory of the bot, see `bot.conf.example`.

Furthermore you will need to do a little bit of initial configuration so that
the bot can respond to your commands. From the directory containing `bot.conf`
run the following (adjust `PYTHONPATH` so that the necessary modules are in
scope):
```sh
python -m util.db.kv plugins.commands prefix '"."'
python -m util.db.kv plugins.autoload autoload,plugins.discord_log 'true'
python -m util.db.kv plugins.autoload autoload,plugins.bot_manager 'true'
# Create the shell and admin roles.
# Substitute your own discord user id.
python -m util.db.kv plugins.privileges shell,users '[207092805644845057]'
python -m util.db.kv plugins.privileges admin,users '[207092805644845057]'
```
Now you can run the bot by executing `main.py`.

If you'd like to use your own plugins from a different repository, say from
`$DIR`, you can put it on the `PYTHONPATH`, and the bot will look for plugins in
`$DIR/plugins/`.
