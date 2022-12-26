# Mathematics Server Discord Bot

This is the open source repository for the utility bot that manages various kinds of things on the Mathematics Discord server. With that purpose in mind, feel free to contribute to this repository. If you'd like to run this bot on your own server, that's fine too, but don't expect support.

## Plugins

These are the plugins provide commands or otherwise user-visible functionality:

### `bot_manager`

Manage the state of the bot.

Commands:
- `plugins` -- list the currently loaded plugins.
- `restart` -- restart the bot.
- `load <plugin>` -- load a plugin.
- `unload <plugin>` -- unload a plugin and its dependents.
- `reload <plugin>` -- reload a plugin and its dependents.
- `reloadmod <module>` -- reload a module (that's not a plugin).
- `unsafereload <plugin>` -- do an "unsafe" in-place reload.
- `unsafeunload <plugin>` -- unload a single plugin.
- `autoload` -- list plugins that are to be auto-loaded on bot startup. Note that loading a plugin doesn't put it on the auto-load list, and adding a plugin to the auto-load list doesn't load it immediately.
    - `autoload add <plugin>` -- add a plugin to the auto-load list.
    - `autoload remove <plugin>` -- remove a plugin from the auto-load list.

### `db_manager`

Manage the database.

Commands:
- `config` -- edit the key-value config. Note that a key is a comma-separated sequence of strings, and a value is a JSON value.
    - `config` -- list namespaces.
    - `config <namespace>` -- list keys.
    - `config <namespace> <key>` -- display the value associated to the specified key.
    - `config <namespace> <key> <value>` -- set a value for the specified key.
    - `config --delete <namespace> <key>` -- delete the specified key.
- ``sql ```query``` `` -- execute arbitrary SQL on the database. The queries must be wrapped in code blocks or inlines. Queries that result in changes always prompt for confirmation.

### `discord_log`

Output bot logs to a channel on Discord.

Config:
- `config plugins.discord_log channel <channel id>` -- ID of the channel to log to.

### `eval`

Eval.

Commands:
- ``eval ```code``` `` -- execute arbitrary Python code in the bot. The code must be wrapped in code blocks or inlines. The scope includes all imported modules, as well as `ctx` and `client`. Output is redirected to Discord. Top-level `await` can be used.
- ``exec ```code``` `` -- synonym for the above.

### `bot.privileges`

Manage privilege sets for commands. Some commands are associated with specific privilege sets, and a privilege set can include a set of roles and a set of users.

Commands:
- `priv new <name>` -- create a new privilege set.
- `priv delete <name>` -- delete a privilege set.
- `priv show <name>` -- show the roles and users in a privilege set.
- `priv add <name> user <user>` -- add a user to a privilege set.
- `priv add <name> role <role>` -- add a role to a privilege set.
- `priv remove <name> user <user>` -- remove a user from a privilege set.
- `priv remove <name> role <role>` -- remove a role from a privilege set.

### `bot.locations`

Manage locations for commands. Some commands are restricted to specific locations, and a location can include a set of channels and a set of categories.

Commands:
- `location new <name>` -- create a new location.
- `location delete <name>` -- delete a location.
- `location show <name>` -- show the channels and categories in a privilege set.
- `location add <name> channel <user>` -- add a channel to a location.
- `location add <name> category <role>` -- add a category to a location.
- `location remove <name> channel <user>` -- remove a channel from a location.
- `location remove <name> category <role>` -- remove a category from a location.

### `version`

Display the current version of the bot according to git.

Commands:
- `version` -- display the SHA1 revision and branch, if any, as well as any local changes on top of the checked out revision.

### `update`

Update the bot by pulling changes from git remote. The current branch needs to have an associated remote, which can be done by e.g.
```sh
git branch --set-upstream-to=origin/main main
```
Multiple repositories in different directories can be configured.

Commands:
- `update [name]` -- update the specified repository (by default the one in the current directory).

Config:
- ``config plugins.update <name> `/path/to/repo/` `` -- directory for the specified repository.

### `help`

A more terse help command.

Commands:
- `help` -- display commands available to the current user.
- `help <command>` -- display help for a command.
- `help <command> <subcommand>...` -- display help for a subcommand.

### `tickets`

Manage infractions and administrative actions on a server. Administrative actions (kicks, bans, mutes, etc) get turned into "tickets" that have an assigned moderator, a comment, and a duration. The tickets are saved in a "ticket list" channel.

Upon doing an action, the responsible moderator will DM'ed with a prompt to enter a duration and a comment for their action. The response should be a duration (if omitted -- permanent) followed by a comment. Durations are specified by a numbed followed by: `s`/`sec`/`second`, `m`/`min`,`minute`, `h`/`hr`/`hour`, `d`/`day`, `w`/`wk`/`week`, `M`/`month`, `y`/`yr`/`year`; or `p`/`perm`/`permanent`. If multiple actions are taken, they are put into a queue and prompted one at a time.

Commands:
- `note <target> [comment]` -- create a "note" ticket, not associated with any action, merely bearing a comment. If a duration is set, after it expires the ticket is hidden.
- `ticket top` -- re-deliver the prompt for comment for the first ticket in your queue.
- `ticket queue [mod]` -- display the ticket queue of the specified moderator (or yourself).
- `ticket take <ticket>` -- assign the specified ticket to yourself.
- `ticket assign <ticket> <mod>` -- assign the specified ticket to the specified moderator.
- `ticket set <ticket> <duration> [comment]` -- set the duration and comment for the specified ticket.
- `ticket append <ticket> <comment>` -- append to the ticket's comment.
- `ticket revert <ticket>` -- revert the administrative action associated with a ticket.
- `ticket hide <ticket>` -- hide a ticket from the list of tickets (e.g. if it was a mistake).
- `ticket show <user|ticket>` -- show all tickets affecting the given user, or show a specific ticket.
- `ticket showhidden <user|ticket>` -- show all hidden tickets affecting the given user, or show a specific ticket.
- `tickets ...` -- synonym for the above

Config:
- ``config plugins.tickets guild <guild id>`` -- guild for which tickets are being managed.
- ``config plugins.tickets tracked_roles `[<channel id>, ...]` `` -- list of roles assigning which counts as administrative action (e.g. a muted role).
- ``config plugins.tickets ticket_list <channel id>`` -- channel where the log of tickets should be displayed.
- ``config plugins.tickets prompt_interval <interval>`` -- if a moderator doesn't reply to a prompt for a comment, after how long (in seconds) should they be reminded?
- ``config plugins.tickets audit_log_precision <float>`` -- in large guilds the audit log may lag behind, causing the bot to only realize much later that an action has had been taken. This is a delay (in seconds) to compensate.
- ``config plugins.tickets cleanup_delay <float>`` -- if a non-ticket message is posted in the ticket list channel, it will be deleted after this delay.

### `persistence`

Remember members' roles when they leave the guild and rejoin.

Config:
- ``config plugins.persistence roles `[<role id>, ...]` `` -- list of roles to remember.

### `modmail`

Run a mod-mail system. A separate bot user is spun up listening for DM's. Upon receiving a DM, the bot forwards it to a staff channel, and confirms delivery to the user by reacting under the message. When staff reply to the modmail, the reply is forwarded back to the user. The staff is prompted for whether the reply should be anonymous or not.

Config:
- ``config plugins.modmail token `"<token>"` `` -- bot token for the modmail user. Ideally this is a separate user, otherwise modmail would conflict with ticket prompts.
- ``config plugins.modmail guild <guild id>`` -- the guild for which this modmail is run.
- ``config plugins.modmail channel <channel id>`` -- the staff channel to which the modmails are forwarded.
- ``config plugins.modmail role <role id>`` -- the role which is pinged by (new) modmails.
- ``config plugins.modmail thread_expiry <duration>`` -- multiple consecutive messages from the same user are considered to be a part of the same thread and don't ping the role. This duration specifies how long it has to pass (in seconds) since the last message for it to be considered a separate thread.

### `automod`

Scan messages for "bad" keywords. A keyword is either a substring to be sought for, a word (has to be delimited with spaces), or a regular expression. If a message matches any of the patterns, it is deleted, and depending on the pattern the author could be muted, kicked, or banned.

Additionally, if the message contains a link to a known phishing domain, the user is banned immediately.

Commands:
- `automod list` -- list all the patterns currently searched for.
- `automod add substring <substring>...` -- add a keyword with one or more substrings. The substrings will be case-insensitively matched anywhere inside a message.
- `automod add word <word>...` -- add a keyword with one or more words. The words will be case-insensitively matched against whole words in a message.
- `automod add regex <regex>...` -- add a keyword with one or more regexes. The regexes are python flavor, case insensitive by default.
- `automod remove <id>` -- remove a pattern with given ID.
- `automod action <id> <delete|note|mute|kick|ban>` -- set the action for the pattern with the given ID. Note creates a "note" ticket for the user, summarizing the patterns they matched.
- `automod exempt` -- list roles that are exempt from punishment.
- `automod exempt add <role>` -- add a role to be exempt from punishment.
- `automod exempt remove <role>` -- make a role not exempt from punishment.

Config:
- ``config plugins.automod mute_role <role id>`` -- which role to assign for the "mute" action.

### `phish`

As part of automod, maintain a list of known phishing domains.

Commands:
- `phish check <url or domain>` -- check if the domain appears in the phishing database, or if it is marked locally.
- `phish add <url or domain>` -- locally mark a domain as malicious, and try to submit it to the phishing database.
- `phish remove <url or domain>` -- locally mark a domain as safe.

Config:
- ``config plugins.phish api `"https://.../"` `` -- the API used to obtain the list of phishing domains.
- ``config plugins.phish identity `"<identity>"` `` -- the identity presented to the API.
- ``config plugins.phish resolve_domains `["domain.com", ...]` `` -- the list of domains for which we want to resolve redirects (URL shorteners).
- ``config plugins.phish submit_url `"https://.../"` `` -- the API used to submit new domains.
- ``config plugins.phish submit_token `"<token>"` `` -- the authorization header for the submission API.

### `clopen`

Manage an Open/Closed help channels system. Channels can be "available", "occupied", "pending", "closed", or "hidden". If you post in an "available" channel, the bot pins your message and the channel becomes "occupied". As long as there is conversation in the channel, it remains "occupied". If a timeout is reached, you are prompted to close the channel and the channel becomes "pending". If you ignore the prompt, the channel will close automatically. If you decline the prompt, the channel goes back to "occupied", and the timeouts are doubled. Deleting the original post also closes the channel. A "closed" channel will remain in the "occupied" category for a while before moving back into either "occupied" or "hidden".

This plugin currently also manages a help forum. Posts can be marked solved or unsolved, and a broader set of users are allowed to manage the post via a UI.

Commands:
- `close` -- an "occupied" or "pending" channel can be closed at any time by either its owner or certain roles. If used in the help forum, assume the user meant `solved`.
- `reopen` -- if the channel is "closed" or "available", it can be reopened by the previous owner or certain roles. If used in the help form, assume the user meant `unsolved`.
- `clopen_sync` -- synchronize the state of the channels.
- `solved` -- mark a forum post as solved (can be done by the owner of the thread or certain roles).
- `unsolved` -- mark a forum post as unsolved (can be done by the owner of the thread or certai roles).

Config:
- ``config plugins.clopen channels `[<channel id>, ...]` `` -- the list of channels in rotation.
- ``config plugins.clopen available_category <category id>`` -- where "available" channels are placed.
- ``config plugins.clopen used_category <category id>`` -- where "occupied" channels are placed.
- ``config plugins.clopen hidden_category <category id>`` -- where "hidden" channels are placed.
- ``config plugins.clopen owner_timeout <timeout>`` -- how long (in seconds, initially) since the last message by the owner before the owner is prompted about closure, and how long until the channel is automatically closed if there was no response.
- ``config plugins.clopen timeout <timeout>`` -- how long (in seconds) since the last message by anyone else before the owner is prompted about closure.
- ``config plugins.clopen min_avail <number>`` -- how many channels minimum should be "available". If not enough channels are available, channels may be unhidden, or new channels may be created.
- ``config plugins.clopen max_avail <number>`` -- how many channels maximum should be "available". If too many channels are available, some may be hidden.
- ``config plugins.clopen max_channels <number>`` -- do not create channels past this point.
- ``config plugins.clopen limit <number>`` -- limit on how many channels can be occupied by a person at once.
- ``config plugins.clopen limit_role <role id>`` -- role that is assigned when the limit is reached.
- ``config plugins.clopen forum <channel id>`` -- id of the forum channel.
- ``config plugins.clopen pinned_posts [<thread id>, ...]`` -- list of thread ids in which posts should be cleaned up.
- ``config plugins.clopen solved_tag <tag id>`` -- the tag for solved posts.
- ``config plugins.clopen unsolved_tag <tag id>`` -- the tag for unsolved posts.

### `factoids`

Manage a collection of short recall-able posts. A factoid is a single message containing either some text or a rich embed. Factoids can have multiple names, and typing `<prefix><name>` followed by anything -- will output the factoid with the specified name. Names can contain spaces, and in case of conflicts the longest matching name will be used.

Privileged users can add rich embed factoids, allow mentions inside factoids, restrict factoids to privs and locations.

Commands:
- `tag add <name>` -- add a factoid and assign a name to it. You will be prompted to input the factoid contents in a separate message.
- `tag alias <name> <newname>` -- add an alias "newname" to the factoid named "name".
- `tag edit <name>` -- edit the specified factoid. You will be prompted to input the new factoid contents as a separate message. If the factoid has any aliases, those are updated as well.
- `tag delete <name>` -- delete a factoid and all its aliases.
- `tag unalias <name>` -- remove an alias. The last alias for a factoid cannot be removed -- use `tag delete` instead.
- `tag info <name>` -- show info about a factoid.
- `tag top` -- show factoid usage statistics.
- ``tag flags <name> `<json>` `` -- set flags on a factoid. Factoids with flags can only be edited by admins. The flags are a JSON dictionary with the following keys:
    - `"mentions"` -- if true, an invocation of the factoid will ping the roles and users in its contents.
    - `"priv"` -- a string referring to a privilege set (configured with `priv`) that is required to use the factoid.
    - `"location"` -- a string referring to a location (configured with `location`) where the factoid can be used.
- `tag flags <name>` -- show flags on the given factoid.

Config:
- ``config plugins.factoids prefix `"<prefix>"` `` -- prefix for recalling factoids, could be distrinct from the bot prefix.

### `rolereactions`

Manage role-reactions: reacting on unreacting specific emoji under specific messages will add or remove specific roles.

Commands:
- `rolereact new <message>` -- make the given message a role-react message.
- `rolereact delete <message>` -- make the given message not a role-react message.
- `rolereact list` -- list messages with role-reactions.
- `rolereact show <message>` -- show role-reactions on a message.
- `rolereact add <message> <emoji> <role>` -- add a role-reaction. If the emoji is available to the bot, it will react under the message.
- `rolereact remove <messag> <emoji>` -- remove a role-reaction.

### `log`

Log member and message updates. Member joins, leaves, nickname and username changes, bulk message deletes are all logged in the "permanent" channel. Message edits and deletes are all logged in the "temporary" channel. As the name suggests the "temporary" channel is periodically cleaned up.

Config:
- ``config plugins.log temp_channel <channel id>`` -- the "temporary" channel.
- ``config plugins.log pemr_channel <channel id>`` -- the "permanent" channel.
- ``config plugins.log keep <interval>`` -- how long (in seconds) to keep edits/deletes for.
- ``config plugins.log interval <interval>`` -- how often (in seconds) to clean up the "temporary" channel.
- ``config plugins.log file_path `/path/to/attachments/` `` -- where to save message attachments.

### `keepvanity`

If for any reason the server loses a vanity URL, this plugins attempts to restore it whenever possible.

Config:
- ``config plugins.keepvanity guild <guild id>`` -- the guild.
- ``config plugins.keepvanity vanity `"vanity"` `` -- the vanity code, without `discord.gg` and without the `/`.

### `pins`

Allow a set of roles to manage pins in certain locations, without giving the permission to manage messages (implies deletes).

Commands:
- `pin [message]` -- pin the specified message, or the message being replied to. If there's no space in the pins we'll offer to unpin the oldest message and otherwise wait until something else is unpinned.
- `unpin [message]` -- unpin the specified message, or the message being replied to.

### `reminders`

Manage reminders.

Commands:
- `remindme <interval> <text>` -- pings you with the given text after the given interval elapses. Intervals can be specified in the same way as for tickets, except multiple units can be chained together, e.g. `10 min 15 sec`.
- `remind <interval> <text>` -- synonym for the above.
- `reminder` -- list active reminders.
- `reminders` -- synonym for the above.
- `reminder remove <index>` -- remove a reminder by index.
- `reminders remove <index>` -- synonym for the above.

### `roleoverride`

Ensure that certain roles are mutually exclusive, e.g. a mute role must exclude any roles that enable sending messages.

Config:
- ``config plugins.roleoverride <roleA> `[<roleB>, ...]` `` -- whenever roleA is added, roleB will be removed.

### `bulk_perms`

Edit permissions in bulk, by exporting them into a CSV file and then importing back.

Commands:
- `exportperms` -- sends a CSV file with all the permission settings in the current guild.
- `importperms` -- you have to attach a CSV file in the same format as provided by `exportperms`, and the bot will load permissions from the CSV file. Channels not mentioned in the CSV file are unaffected.

### `consensus`

Set up polls. Users can vote yes/no/abstain on polls, and also raise concerns, in which case all previous votes are notified of the concern. The author of the poll gets notified when the poll times out if there haven't been any raised concerns.

Commands:
- `poll <duration> <comment>` -- set up a poll with the provided expiration duration.
- `polls` -- list currently open polls.

### `roles_dialog`

A button and a `/roles` slash-command for managing self-assigned roles.

Config:
- ``config plugins.roles_dialog roles `[[<role id|text>, ...], ...]` `` -- the self-assignable roles. Lists with multiple elements correspond to choosing one of the roles. A string instead of a role ID corresponds to an option that doesn't assign any role (e.g. "None of the above"). Lists with one element are grouped together and correspond to choosing any subset of the roles.
- ``config plugins.roles_dialog <role id>,desc `"<text>"` `` -- extended description for the role.

### `roles_review`

A review system for role requests. When a role is requested via `roles_dialog`, the user is prompted to answer some questions, and the answers are posted in a review channel, where members of the channel can vote to approve or reject the application. A chosen role can veto-approve or veto-reject an application.

Commands:
- `review_reset <user> <role>` -- when a user's application is denied, they are not allowed to submit another until this command is invoked on them.
- `review_queue` -- show a list of links to unresolved applications.

Config:
- ``config plugins.roles_review <role> `{...}` `` -- attempting to self-assign the role will instead make the user go through the application process. The keys in the object are as followws:
    - `"prompt": ["<question>[\n<placeholder>]", ...]` -- list of questions to ask an applicant.
    - `"review_channel": <channel id>` -- channel where the applications will be posted and voted on.
    - `"review_role": <role id>` -- (optional) the role that is allowed to vote on applications.
    - `"veto_role": <role id>` -- (optional) the role that is allowed to veto applications.
    - `"upvote_limit": <number>` -- how many upvotes are needed to accept an application.
    - `"downvote_limit": <number>` -- how many downvotes are needed to accept an application.
    - `"pending_role": <role id>` -- (optional) role given to the applying user while the application is pending.
    - `"denied_role": <role id>` -- (optional) role given to the applying user if their application is denied.

### `whois`

A `/whois <user>` slash-command for locating users and printing useful information about them.

### Miscellaneous Configuration
- ``config bot.commands prefix `"<prefix>"` `` -- set the prefix for the bot's "ordinary" commands.

## Running

The bot requires:
 - Python 3.9+
 - PostgreSQL 10+
 - Libraries listed in `requirements.txt`

You will need to create a static config file called `bot.conf` in the working
directory of the bot, see `bot.conf.example`.

Furthermore you will need to do a little bit of initial configuration so that
the bot can respond to your commands. From the directory containing `bot.conf`
run the following (adjust `PYTHONPATH` so that the necessary modules are in
scope):
```sh
python -m util.db.kv bot.commands prefix '"."'
python -m util.db.kv bot.autoload plugins.bot_manager 'true'
# Create the shell and admin roles.
# Substitute your own discord user id.
python -m util.db.kv bot.privileges shell,users '[207092805644845057]'
python -m util.db.kv bot.privileges admin,users '[207092805644845057]'
```
Now you can run the bot by executing `main.py`. You can continue the configuration over Discord (you may find it useful to load some of the administration modules). For example:
```
.load db_manager
.config plugins.discord_log channel 268882890576756737
.load discord_log
.load eval
.autoload add db_manager
.autoload add discord_log
.audoload add eval
```
The `python -m util.db.kv` shell command is analogous to the `.config` Discord command.

If you'd like to use your own plugins from a different repository, say from
`$DIR`, you can put it on the `PYTHONPATH`, and the bot will look for plugins in
`$DIR/plugins/`.
