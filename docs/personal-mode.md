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

Personal mode also supplies default PTY executables (`bash`, `sh`, `python`, `python3`, `node`). PTY startup is approval-free by default in Personal mode, while Hardened mode keeps the existing one-use `pty.start` approval flow.

This mode is not an OS sandbox. In particular, `bash -lc` can compose commands and interpreters can execute arbitrary code as the Agent user. Run the Agent as an ordinary unprivileged account and do not grant that account broader OS privileges merely for Commander convenience.

Recommended single-user starting point:

```env
COMMANDER_OPERATION_MODE=personal
COMMANDER_ALLOWED_ROOTS_JSON=[]
```

With the empty root list above, the effective root becomes the Agent user's home directory.
Structured `search_files`, `create_directory`, `copy_file`, `move_path`, and non-recursive `delete_path` tools are available in v0.29; see `personal-filesystem-tools.md`.

## Interactive PTY approval

In Personal mode, PTY startup does not require an external one-use approval by default. This removes redundant friction because Personal mode already permits broad user-level shell execution. The Agent PTY executable allowlist and OS user permission boundary still apply.

Set `COMMANDER_PERSONAL_PTY_APPROVAL_REQUIRED=true` on the Gateway to restore the one-use approval requirement while keeping the rest of Personal mode enabled. Hardened mode always requires PTY approval regardless of this setting. If either approval field is supplied manually, both fields must be supplied and the approval is consumed normally.


## Structured process signals

`signal_process` supports `term`, `kill`, `int`, and `hup` while binding every action to the inspected `(pid, create_time_ms)` identity so PID reuse cannot redirect a stale request. Personal mode does not require an external approval by default because the same Agent user can already signal its own processes through the broad shell path. Hardened mode always requires a one-use approval whose operation is bound to the exact signal type.

Set `COMMANDER_PERSONAL_PROCESS_APPROVAL_REQUIRED=true` on the Gateway to restore one-use approval for structured process signals in Personal mode. The existing `terminate_process` tool remains approval-required in all modes for backward compatibility.

## Expanded Personal command profiles

The Personal generic profile also includes common development and operations utilities such as `jq`, `rsync`, `ssh`, `scp`, `ffmpeg`, `ffprobe`, `sqlite3`, `psql`, `tmux`, `screen`, checksum tools, compression tools, and diff/patch utilities when those executable names resolve in the Agent user's PATH.

Some broad privilege, raw-process, and low-level storage commands remain intentionally outside the generic profile. Managed process and session signaling should use the structured `signal_process` and `signal_session` paths so PID identity checks and audit safeguards remain in effect.

This profile boundary is a structured-API preference, not an OS sandbox guarantee. Personal mode already permits user-level command composition through the explicitly supported shell profiles, and every child process remains limited to the permissions of the OS account running the Agent.


## Custom generic command profiles

Personal mode can extend the built-in generic command set without a code change by adding bare executable names to `COMMANDER_ALLOWED_EXECUTABLES` on both the Gateway and Agent. For example, `COMMANDER_ALLOWED_EXECUTABLES=uv,poetry` makes those names eligible for the generic command path when they resolve in the Agent user's effective `PATH`.

Custom profiles remain fail-closed: names must be bare executable names, the Gateway must allow the same name, the Agent allowlist must contain it, and the executable must resolve on the Agent. Hardened mode ignores custom widening and remains limited to the built-in safe generic profiles. This mechanism does not bypass OS permissions or the existing cwd/env/output/session bounds.
