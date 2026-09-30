# Recursive filesystem operations

Recursive directory mutation is intentionally a two-step workflow in Personal mode:

1. `inspect_tree(agent_id, path)` computes a bounded content fingerprint.
2. Pass the returned `tree_sha256` as `expected_tree_sha256` to `copy_directory` or `delete_tree`.

The mutation re-scans the tree immediately before changing it. If file contents, structure, modes, or the root inode changed since inspection, the operation is rejected.

## Safety boundaries

- source and destination must stay inside configured allowed roots;
- an allowed root itself can never be recursively copied/deleted through the mutation tools;
- symlinks and special files are rejected during inspection;
- copy destinations must not already exist and cannot be inside the source tree;
- copied trees are fingerprinted again after copy; a mismatch removes the new destination and reports failure;
- recursive delete uses the platform's symlink-attack-resistant `shutil.rmtree` implementation on POSIX;
- default bounds are 5,000 entries and 256 MiB of regular-file content;
- hard API bounds are 50,000 entries and 1 GiB;
- recursive mutation is advertised only in Personal mode as `filesystem.tree_mutate`.

`inspect_tree` remains read-only and is advertised as `filesystem.tree` whenever allowed roots exist.
