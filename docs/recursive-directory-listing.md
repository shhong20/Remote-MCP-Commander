# Recursive directory listing

`list_directory_tree` provides a bounded read-only repository/tree view without requiring shell `find` commands.

Defaults are `depth=2`, hidden paths excluded, at most 100 entries per visited directory, and at most 1000 returned entries overall. Depth is bounded to 5, per-directory output to 200, and total output to 2000 entries.

The response is a flat deterministic list. Each row includes the absolute path, root-relative path, entry kind, depth, optional file size, and modification time. Directories sort before non-directories and names sort case-insensitively with a stable tie-breaker.

Symbolic links are reported as `symlink` entries but never traversed. Nested directories are resolved and checked against Agent allowed roots before scanning. Hidden files and directories can be included explicitly with `include_hidden=true`.

`truncated=true` means a per-directory limit, total-entry limit, or nested access/race condition prevented a complete traversal. Root access errors are returned as rejected requests.

New Agents advertise `filesystem.tree_list`; the Gateway requires the capability before dispatch so rolling upgrades fail closed rather than silently falling back.
