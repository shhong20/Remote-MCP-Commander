# Filesystem discovery

Filesystem discovery lets MCP clients find safe paths before using bounded file reads/writes.

## MCP tools

- `list_file_roots(agent_id)` lists the absolute roots explicitly configured by the Agent.
- `list_directory(agent_id, path, limit=200)` lists one directory non-recursively.
- `file_info(agent_id, path)` returns lstat-style metadata for one path.

These tools do not create, delete, move, rename, search, or recursively walk files.

## Allowed-root boundary

All directory listing paths must canonicalize inside `COMMANDER_ALLOWED_ROOTS_JSON`. A directory symlink that resolves outside those roots is rejected before listing.

`file_info` resolves the parent directory inside allowed roots but intentionally does not follow the final path component. This allows a symlink to be identified as a symlink without exposing its target metadata or target contents.

Relative paths are rejected.

## Returned metadata

Directory entries contain only:

- name and absolute in-root path
- type: `file`, `directory`, `symlink`, or `other`
- regular-file size when applicable
- UTC modification time

Listings are bounded to `1..500` entries and report `truncated=true` when more entries exist. Symlink target strings are not returned.

`file_info` additionally returns the permission mode in octal form. It does not hash or read file contents.

## Deliberately deferred

Recursive traversal, filename/content search, directory creation, delete, move/rename, chmod/chown, symlink creation, and arbitrary binary operations remain outside this surface until each gets a separate policy and safety design.
