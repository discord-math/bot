
# Mathematics Server Discord Bot

This is the open source repository for the utility bot that manages various kinds of things on the Mathematics Discord server. With that purpose in mind, see `CONTRIBUTING.md` if you want to contribute to the bot. If you'd like to run this bot on your own server, that's fine too, but don't expect support.

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

### `db_manager`

Manage the database.

Commands:
- `config` -- edit the key-value config. Note that a key is a comma-separated sequence of strings, and a value is a JSON value.
    - `config` -- list namespaces.
    - `config <namespace>` -- list keys.
    - `config <namespace> <key>` -- display the value associated to the specified key.
    - `config <namespace> <key> <value>` -- set a value for the specified key.
    - `config --delete <namespace> <key>` -- delete the specified key.
- `config commands prefix "<prefix>"` -- set the prefix for the bot's "ordinary" commands.
- `config autoload` -- list plugins that are to be auto-loaded on bot startup. Note that loading a plugin doesn't put it on the auto-load list, and adding a plugin to the auto-load list doesn't load it immediately.
- `config autoload add <plugin> <order>` -- add a plugin to the auto-load list. The higher the order the later the plugin gets loaded.
- `config autoload remove <plugin>` -- remove a plugin from the auto-load list.
- ``sql ```query``` `` -- execute arbitrary SQL on the database. The queries must be wrapped in code blocks or inlines. Queries that result in changes always prompt for confirmation.
- `acl` -- edit Access Control Lists: permission settings for commands and other miscellaneous actions. An ACL is a formula involving users, roles, channels, categories, and boolean connectives. A command or an action can be mapped to an ACL, which will restrict who can use the command/action and where.
    - `acl list` -- list ACL formulas.
    - `acl show <acl>` -- display the formula for the given ACL in YAML format.
    - ``acl set <acl> ```formula``` `` -- set the formula for the given ACL. The formula must be a code-block containing YAML.
    - `acl commands` -- show all commands that are assigned to ACLs.
    - `acl command <command> [acl]` -- assign the given command (fully qualified name) to the given ACL, restricting its usage to the users/channels specified in that ACL. If the ACL is omitted the command can never be used.
    - `acl actions` -- show all actions that are assigned to ACLs. Actions are used by various plugins an correspond to permission checks outside of commands. `acl_override` is a permission check which, if passed, allows sidestepping permission checks on commands and other actions.
    - `acl action <action name> [acl]` -- assign the given action to the given ACL. See documentation for other plugins regarding action names.
    - `acl metas` -- show meta-ACLs, which control when a given ACL can be *edited*. If `X` is a meta-ACL for `Y`, then satisfying `X` is required to edit the formula for `Y`, to re-assign commands and actions that are currently assigned to `Y`, and to change what `Y`'s meta-ACL is. Note that to edit ACLs you still (additionally) have to have permissions for the `acl` command/subcommands.
    - `acl meta <acl> [meta-acl]` -- make `meta-acl` the meta-ACL for `acl`.

### `discord_log`

Output bot logs to a channel on Discord.

Config:
- `config syslog channel ["None"|channel]` -- configure the channel where the bot logs to.

### `eval`

Eval.

Commands:
- ``eval ```code``` `` -- execute arbitrary Python code in the bot. The code must be wrapped in code blocks or inlines. The scope includes all imported modules, as well as `ctx` and `client`. Output is redirected to Discord. Top-level `await` can be used.
- ``exec ```code``` `` -- synonym for the above.

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
- `config update` -- list repositories.
- `config update add <name> </path/to/repo>` -- add a repository. The specified path will be used as the CWD for `git` operations.
- `config update remove <name>` -- remove a repository.

### `help`

A more terse help command.

Commands:
- `help` -- display commands available to the current user.
- `help <command>` -- display help for a command.
- `help <command> <subcommand>...` -- display help for a subcommand.

### `tickets`

Manage infractions and administrative actions on a server. Administrative actions (kicks, bans, mutes, etc) get turned into "tickets" that have an assigned moderator, a comment, and a duration. The tickets are saved in a "ticket list" channel.

Upon doing an action, the responsible moderator will DM'ed with a prompt to enter a duration and a comment for their action. The response should be a duration (if omitted -- permanent) followed by a comment. Durations are specified by a numbed followed by: `s`/`sec`/`second`, `m`/`min`,`minute`, `h`/`hr`/`hour`, `d`/`day`, `w`/`wk`/`week`, `M`/`month`, `y`/`yr`/`year`; or `p`/`perm`/`permanent`. If multiple actions are taken, they are put into a queue and prompted one at a time.

If the responsible moderator doesn't match the `auto_approve_tickets` action, the ticket will be marked as "unapproved".

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
- `ticket approve <ticket>` -- remove the "unapproved" flag from the ticket.
- `tickets ...` -- synonym for the above

Config:
- ``config plugins.tickets guild <guild id>`` -- guild for which tickets are being managed.
- ``config plugins.tickets tracked_roles `[<role id>, ...]` `` -- list of roles assigning which counts as administrative action (e.g. a muted role).
- ``config plugins.tickets ticket_list <channel id>`` -- channel where the log of tickets should be displayed.
- ``config plugins.tickets prompt_interval <interval>`` -- if a moderator doesn't reply to a prompt for a comment, after how long (in seconds) should they be reminded?
- ``config plugins.tickets audit_log_precision <float>`` -- in large guilds the audit log may lag behind, causing the bot to only realize much later that an action has had been taken. This is a delay (in seconds) to compensate.
- ``config plugins.tickets cleanup_delay <float>`` -- if a non-ticket message is posted in the ticket list channel, it will be deleted after this delay.

### `persistence`

Remember members' roles when they leave the guild and rejoin.

Config:
- `config persistence` -- list roles to remember.
- `config persistence add <role>` -- add a role.
- `config persistence remove <role>` -- remove a role.

### `modmail`

Run a mod-mail system. A separate bot user is spun up listening for DM's. Upon receiving a DM, the bot forwards it to a staff channel, and confirms delivery to the user by reacting under the message. When staff reply to the modmail, the reply is forwarded back to the user. The staff is prompted for whether the reply should be anonymous or not.

Config:
- `config modmail <server> new <token> <channel> <role> <duration>` -- set up modmail for the given server. The supplied token should ideally be for a separate user, otherwise modmail would conflict with ticket prompts. The channel is the staff channel to which the modmails are forwarded. The role is the one pinged by (new) modmails. Multiple consecutive messages from the same user are considered to be a part of the same thread and don't ping the role. The supplied duration specifies how long it has to pass since the last message for it to be considered a separate thread.
- `config modmail <server> token [token]` -- edit the modmail client token for the given server.
- `config modmail <server> token [channel]` -- edit the staff channel.
- `config modmail <server> role [role]` -- edit the role that gets pinged.
- `config modmail <server> thread_delay [duration]` -- edit the thread creation threshold.

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

### `phish`

As part of automod, maintain a list of known phishing domains.

Commands:
- `phish check <url or domain>` -- check if the domain appears in the phishing database, or if it is marked locally.
- `phish add <url or domain>` -- locally mark a domain as malicious, and try to submit it to the phishing database.
- `phish remove <url or domain>` -- locally mark a domain as safe.

Config:
- `config phish api_url ["None"|https://.../]` -- configure the API used to obtain the list of phishing domains.
- `config phish identity ["None"|identity]` -- configure the identity presented to the API.
- `config phish submit_url ["None"|https://.../]` -- configure the API used to submit new domains.
- `config phish submit_token ["None"|token]` -- configure the authorization header for the submission API.
- `config phish shortener` -- list domains for which we want to resolve redirects (URL shorteners).
- `config phish shortener add <domain>` -- add shortener.
- `config phish shortener remove <domain>` -- remove shortener.

### `clopen`

Manage an Open/Closed help channels system. Channels can be "available", "occupied", "pending", "closed", or "hidden". If you post in an "available" channel, the bot pins your message and the channel becomes "occupied". As long as there is conversation in the channel, it remains "occupied". If a timeout is reached, you are prompted to close the channel and the channel becomes "pending". If you ignore the prompt, the channel will close automatically. If you decline the prompt, the channel goes back to "occupied", and the timeouts are doubled. Deleting the original post also closes the channel. A "closed" channel will remain in the "occupied" category for a while before moving back into either "occupied" or "hidden".

This plugin currently also manages a help forum. Posts can be marked solved or unsolved, and a broader set of users are allowed to manage the post via a UI.

The OP can always manage their own channel/post. Additionally it can be managed by those who match the `manage_clopen` action.

Commands:
- `close` -- an "occupied" or "pending" channel can be closed at any time by either its owner or certain roles. If used in the help forum, assume the user meant `solved`.
- `reopen` -- if the channel is "closed" or "available", it can be reopened by the previous owner or certain roles. If used in the help form, assume the user meant `unsolved`.
- `clopen_sync` -- synchronize the state of the channels.
- `solved` -- mark a forum post as solved.
- `unsolved` -- mark a forum post as unsolved.

Config:
- `config clopen <server> channels` -- list the channels that are considered part of the system.
- `config clopen <server> available [category]` -- configure where the "available" channels are placed.
- `config clopen <server> used [category]` -- configure where the "occupied" channels are placed.
- `config clopen <server> hidden [category]` -- configure where the "hidden" channels are placed.
- `config clopen <server> owner_timeout [duration]` -- configure how long (initially) since the last message by the owner before the owner is prompted about closure, and how long until the channel is automatically closed if there was no response.
- `config clopen <server> timeout [duration]` -- configure how long since the last message by anyone else before the owner is prompted about closure.
- `config clopen <server> min_avail [number]` -- configure how many channels minimum should be "available". If not enough channels are available, channels may be unhidden, or new channels may be created.
- `config clopen <server> max_avail [number]` -- configure how many channels maximum should be "available". If too many channels are available, some may be hidden.
- `config clopen <server> max_channels [number]` -- configure the max number of channels that can be created. This number should not exceed 50, as is is impossible to place more than 50 channels in a category.
- `config clopen <server> limit [number]` -- configure the limit on how many channels can be occupied by a person at once.
- `config clopen <server> limit_role [role]` -- configure the role that is assigned when the limit is reached. This role should prevent the user from posting in the "available" category.

- `config clopen <server> forum [forum]` -- configure the forum channel.
- `config clopen <server> solved_tag [tag_id]` -- configure the tag for solved posts.
- `config clopen <server> unsolved_tag [tag_id]` -- configure the tag for unsolved posts.
- `config clopen <server> pinned` -- list posts in which messages should be cleaned up.
- `config clopen <server> pinned add <post_id>` -- add a pinned post.
- `config clopen <server> pinned remove <post_id>` -- remove a pinned post.

- `config clopen <server> new <available_category> <used_category> <hidden_category> <limit_role> <forum> <solved_tag_id> <unsolved_tag_id>` -- initially configure the system for on a server.
- `config clopen <server> channels add <channel>` -- register a channel to be used with the system.

### `factoids`

Manage a collection of short recall-able posts. A factoid is a single message containing either some text or a rich embed. Factoids can have multiple names, and typing `<prefix><name>` followed by anything -- will output the factoid with the specified name. Names can contain spaces, and in case of conflicts the longest matching name will be used.

When using a factoid users will be matched against the `use_tags` action.

Users matching the `manage_tag_flags` action can add rich embed factoids, allow mentions inside factoids, restrict factoids to an ACL.

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
    - `"acl"` -- a string referring to an ACL (configured with `acl`) that is required to use the factoid.
- `tag flags <name>` -- show flags on the given factoid.

Config:
- `config factoids prefix ["None"|prefix]` -- configure the prefix for recalling factoids, could be distrinct from the bot prefix.

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
- ``config plugins.log perm_channel <channel id>`` -- the "permanent" channel.
- ``config plugins.log keep <interval>`` -- how long (in seconds) to keep edits/deletes for.
- ``config plugins.log interval <interval>`` -- how often (in seconds) to clean up the "temporary" channel.
- ``config plugins.log file_path `/path/to/attachments/` `` -- where to save message attachments.

### `keepvanity`

If for any reason the server loses a vanity URL, this plugins attempts to restore it whenever possible.

Config:
- `config keepvanity` -- list managed servers.
- `config keepvanity add <server> <vanity>` -- add a server. The vanity code must be provided without `discord.gg` and without the `/`.
- `config keepvanity remove <server>` -- remove a server.

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
- `reminder remove <id>` -- remove a reminder by ID.
- `reminders remove <id>` -- synonym for the above.

### `roleoverride`

Ensure that certain roles are mutually exclusive, e.g. a mute role must exclude any roles that enable sending messages.

Config:
- `config roleoverride` -- list role overrides.
- `config roleoverride add <retained role> <excluded role>` -- add a role override such that whenever you have both of the roles, the excluded role is removed.
- `config roleoverride remove <retained role> <excluded role>` -- remove a role override.

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

A button and a `/roles` slash-command for managing self-assigned roles. Roles are self-assigned by the means of a series of dropdowns. Each dropdown lets you either choose one of the options, or a subset of the options.

Config:
- `config roles_dialog` -- list dropdowns and the IDs of their options.
- `config roles_dialog mode <index> ["choice"|"multi"]` -- configure whether the dropdown at the given index acts as a single choice or multple choice.
- `config roles_dialog new <index>` -- create a new option in the dropdown at the given index.
- `config roles_dialog role <id> ["None"|role]` -- configure which role the option option with the given ID assigns, if any.
- `config roles_dialog label <id> ["None"|label]` -- configure the name of the option with the given ID, if different from the role name.
- `config roles_dialog description <id> ["None"|description]` -- configure the description of the option with the given ID, if any.
- `config roles_dialog remove <id>` -- remove an option with the given ID.

### `roles_review`

A review system for role requests. When a role is requested via `roles_dialog`, the user is prompted to answer some questions, and the answers are posted in a review channel, where members of the channel matching the `review_can_vote` action can vote to approve or reject the application. Members matching the `review_can_veto` action can veto-approve or veto-reject an application.

Commands:
- `review_reset <user> <role>` -- when a user's application is denied, they are not allowed to submit another until this command is invoked on them.
- `review_queue [any|mine]` -- show a list of links to unresolved applications. The argument `any` will list all unresolved applications, whereas `mine` will list only applications that the user hasn't yet voted on. Without an argument, defaults to `mine`.

Config:
- `config roles_review <role> new <review channel> <upvote limit> <downvote limit> <invitation> <prompt...>` -- configure the given role to go through the review process.
- `config roles_review <role> prompt [text...]` -- configure the questions used for the application. Each question should be in a separate code block.
- `config roles_review <role> review_channel [channel]` -- configure the channel where the applications are posted.
- `config roles_review <role> upvote_limit [limit]` -- configure how many votes it takes for an application to be approved.
- `config roles_review <role> downvote_limit [limit]` -- configure how many votes it takes for an application to be rejected.
- `config roles_review <role> pending_role ["None"|role]` -- configure an optional "pending" role, which is given in the interim to the applicant.
- `config roles_review <role> denied_role ["None"|role]` -- configure an optional role that is given to the application when their application is denied.
- `config roles_review <role> invitation [text]` -- configure the message that is sent to a user that self-assigns the pending role (via Discord UI), inviting them to fill out an application.

### `whois`

A `/whois <user>` slash-command for locating users and printing useful information about them.

## Running

The bot requires:
 - Python 3.9+
 - PostgreSQL 12+
 - Libraries listed in `requirements.txt`

You will need to create a static config file called `bot.conf` in the working
directory of the bot, see `bot.conf.example`.

Next you will need to do a little bit of initial configuration so that the bot can respond to your commands. From the directory containing `bot.conf` run the following (adjust `PYTHONPATH` so that the necessary module is in scope):
```sh
python -m util.setup
```
This will prompt you for your Discord user ID, and the command prefix.

Now you can run the bot by executing `main.py`. You can continue the configuration over Discord, for example:
```
.load discord_log
.config syslog 268882890576756737
.config autoload add discord_log 0
```

If you'd like to use your own plugins from a different repository, say from
`$DIR`, you can put it on the `PYTHONPATH`, and the bot will look for plugins in
`$DIR/plugins/`.
