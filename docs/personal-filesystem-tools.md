# Personal filesystem tools

Version 0.29 adds structured filesystem operations for the trusted single-user server workflow.

## Search

`search_files` recursively searches only inside Agent allowed roots. `mode=files` matches file names; `mode=content` scans bounded UTF-8 text files. Searches do not follow directory symlinks, skip binary/NUL content, scan at most 10,000 regular files, and return at most 200 matches.

Optional `file_glob` filters by basename, for example `*.py`.

## Mutations

Personal-mode Agents advertise `filesystem.mutate` and expose:

- `create_directory(path, parents=false)`
- `copy_file(source, destination, overwrite=false)` for regular files
- `move_path(source, destination, overwrite=false)`
- `delete_path(path)` for regular files or empty directories only

Every source and destination must stay inside an allowed root. Final symlinks and operations on the allowed root itself are rejected. Recursive deletion is deliberately unavailable.

Gateway persistent-audit preflight runs before each structured filesystem mutation. Hardened Agents do not advertise `filesystem.mutate`.

The Personal mode shell remains available for advanced tasks, but these structured tools should be preferred when they express the requested operation directly.
