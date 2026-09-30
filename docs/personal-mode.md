# Personal single-user mode

`COMMANDER_OPERATION_MODE=personal` is for one trusted operator controlling their own server account. It deliberately trades some least-privilege restrictions for Desktop-Commander-like development ergonomics while keeping the Agent bound to the permissions of the OS user that runs it.

The default remains `hardened`. Gateway and Agent must use the same mode.

```env
COMMANDER_OPERATION_MODE=personal
```

When personal mode is enabled, generic command execution gains common developer and operations tools such as `bash`, `sh`, `ls`, `find`, `grep`, `rg`, `git`, `python3`, `pytest`, `pip`, `node`, `npm`, `docker`, `journalctl`, and `systemctl` when those executables exist in the Agent user's PATH.

The configured `COMMANDER_ALLOWED_EXECUTABLES` values are additive in personal mode. Hardened mode retains the original four built-in generic profiles.

Personal commands inherit a deliberately small user-context environment: `PATH`, `HOME`, user/locale variables, XDG paths, and `SSH_AUTH_SOCK` when present. `COMMANDER_*` credentials and arbitrary Agent environment variables are not forwarded to child processes.

If `COMMANDER_ALLOWED_ROOTS_JSON` is empty, personal mode exposes only the Agent user's home directory by default. Set explicit absolute roots to replace that default when narrower or additional filesystem scope is needed.

Personal mode also supplies default PTY executables (`bash`, `sh`, `python`, `python3`, `node`). PTY startup still uses the existing one-use `pty.start` approval flow; personal mode does not silently remove that mutation boundary.

This mode is not an OS sandbox. In particular, `bash -lc` can compose commands and interpreters can execute arbitrary code as the Agent user. Run the Agent as an ordinary unprivileged account and do not grant that account broader OS privileges merely for Commander convenience.

Recommended single-user starting point:

```env
COMMANDER_OPERATION_MODE=personal
COMMANDER_ALLOWED_ROOTS_JSON=[]
```

With the empty root list above, the effective root becomes the Agent user's home directory.
Structured `search_files`, `create_directory`, `copy_file`, `move_path`, and non-recursive `delete_path` tools are available in v0.29; see `personal-filesystem-tools.md`.
