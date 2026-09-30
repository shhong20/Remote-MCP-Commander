# Multi-file read

Remote MCP Commander v0.33.0 adds `read_files` for bounded codebase inspection.

A request accepts 1-20 absolute paths inside Agent allowed roots. Each path is limited to 4096 characters. The default read budget is 32 KiB per file and 256 KiB for the whole request; the hard limits are 64 KiB per file and 512 KiB total.

Each file is handled independently. A missing, binary, oversized, or out-of-root file is returned as a rejected item without aborting successful reads of the other requested files. `eof=false` tells the caller to continue a large text file with the normal cursor-based `read_file` tool.

If the aggregate byte budget is exhausted before every requested path is processed, the batch result sets `truncated=true` and reports both `requested_count` and the files actually returned.

New Agents advertise `file.read_many`, and the Gateway requires that capability before dispatch so rolling upgrades cannot silently drop the new request type.
