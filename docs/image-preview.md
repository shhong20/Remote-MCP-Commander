# Image preview

`preview_image` renders a local image from an Agent allowed root as MCP image content so clients can inspect it without using shell commands or external URLs.

Supported formats are PNG, JPEG, GIF, and WebP. The Agent validates the file extension and the corresponding format signature/header before returning any image bytes. It also extracts dimensions from the format header and rejects images larger than 8192×8192 or 32 megapixels.

## Safety boundaries

- read-only; the image is never modified
- only files inside configured Agent allowed roots are accepted
- final-path symlinks are rejected
- input file size is limited to 1 MiB
- extension and binary signature must agree
- maximum dimension is 8192 pixels per side
- maximum pixel count is 33,554,432
- no external URL fetches
- no image decoder, renderer, or subprocess is executed on the Agent
- Gateway audit records metadata only: path, format, size, width, height, and rejection state; image bytes/base64 are never logged

On success the MCP tool returns a short text metadata block followed by native MCP image content. Rejected images return an anticipated tool error with the bounded rejection reason rather than a generic internal-error message.
