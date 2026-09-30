# Per-command environment overrides

Personal mode can attach bounded environment overrides to `execute`, `start_command`, and `start_pty` without embedding assignments into shell strings.

```text
execute(..., env={"CUDA_VISIBLE_DEVICES":"0", "NODE_ENV":"production"})
start_command(..., env={"CI":"1"})
start_pty(..., env={"TERM":"xterm-256color"})
```

The feature is advertised as `command.env` only in Personal mode. Gateway calls that include `env` require that capability, and the Agent independently validates the override again before process creation.

Limits: at most 32 variables, key length 64, value length 4096, and 16 KiB aggregate encoded key/value data. Variable names must use normal POSIX-style identifiers.

`COMMANDER_*`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `GCONV_PATH`, `BASH_ENV`, and `ENV` are rejected. These restrictions keep connector credentials and loader/shell startup injection out of the structured override channel. Personal-mode shell commands remain subject to the OS user's normal permissions.

Audit records store only sorted environment variable names, never values. If PTY approval is explicitly required, the approval target hash includes the environment map so an approval cannot be reused with different overrides.
