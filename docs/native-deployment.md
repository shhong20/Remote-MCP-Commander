# Native deployment

The recommended first production topology keeps the Python services on loopback and terminates TLS at Caddy.

```text
Internet / ChatGPT MCP client
        |
        | HTTPS / WSS :443
        v
      Caddy
      |   |
      |   +--> 127.0.0.1:8766  MCP Streamable HTTP
      +------> 127.0.0.1:8765  Gateway / Agent WebSocket
```

The Gateway and MCP server run as separate unprivileged `remote-mcp-gateway` and `remote-mcp-server` system accounts so their environment credentials are not co-located under one Unix identity. Each controlled host runs the Agent as the ordinary OS user whose files and processes it is supposed to access.

Do not run the Agent as root merely to expand visibility. Add explicit OS permissions or a narrower dedicated integration instead.

## Gateway host layout

Suggested paths. The application tree can be root-owned/read-only to both service accounts; the state directory should be writable only by `remote-mcp-gateway`. The environment files can remain root-owned mode `0600` because systemd reads them before dropping privileges.

Suggested paths:

```text
/opt/remote-mcp-commander/releases/<id>/ application + venv
/opt/remote-mcp-commander/current -> releases/<id>
/opt/remote-mcp-commander/trusted-release-keys/  root-managed Ed25519 public keys
/etc/remote-mcp-commander/trusted-package-keys/ package-publication Ed25519 public keys
/etc/remote-mcp-commander/trusted-runtime-keys/ runtime-publication Ed25519 public keys
/srv/remote-mcp-artifacts/ immutable content-addressed package/runtime artifact sets
/etc/remote-mcp-commander/gateway.env    Gateway secrets/config, mode 0600
/etc/remote-mcp-commander/mcp.env        MCP secrets/config, mode 0600
/var/lib/remote-mcp-commander/           registry and audit journal
```

Create the two service accounts and state/config directories with normal OS administration tooling. Keep `/etc/remote-mcp-commander` readable only by root and the service account as appropriate, and keep the environment files mode `0600`.

Install the project into a virtual environment under `/opt/remote-mcp-commander/current/.venv`. Copy the two system service templates from `deploy/systemd/`, and copy/edit the environment examples from `deploy/`.

The packaged systemd services run `remote-mcp-doctor gateway` and `remote-mcp-doctor mcp` as `ExecStartPre` checks. Run the same commands manually after editing environment files; a failing check exits non-zero and prevents service startup.

Generate independent credentials, for example with a cryptographically secure system tool such as `openssl rand -hex 32`. Never reuse the control, approval-admin, MCP, or Agent credentials.

## TLS reverse proxy

Copy `deploy/caddy/Caddyfile.example`, replace both example hostnames and the ACME email, then validate and reload Caddy using your distribution's normal service management.

Only TCP 443 needs to be reachable from remote Agents/MCP clients. Keep ports 8765 and 8766 bound to loopback and blocked from external networks.

Caddy supports WebSocket proxying through `reverse_proxy`, so the Agent's `wss://.../ws/agent/<agent-id>` connection and ordinary Gateway HTTPS requests share the same TLS endpoint.

For an internal-only deployment, use your organization's DNS and trusted private PKI rather than weakening the application's HTTPS/WSS checks.

## Controlled host Agent

Install the project into the intended user's `~/.local/share/remote-mcp-commander/current/.venv`, then copy the user-service template to:

```text
~/.config/systemd/user/remote-mcp-agent.service
```

Copy `deploy/agent.env.example` to `~/.config/remote-mcp-commander/agent.env`, set mode `0600`, set the exact enrolled Agent ID, WSS Gateway URL, and explicit allowed roots.

Create the one-time enrollment code through the authenticated Gateway control API, then run `remote-mcp-enroll` as the same OS user that will run the Agent. The issued long-lived Agent credential stays in that user's owner-only token file.

Before enabling the Agent service, run `remote-mcp-doctor agent` as that same OS user. It validates the credential, exact Agent-ID/WebSocket URL binding, allowed roots, and reports detected runtime capabilities.

After enrollment:

```bash
systemctl --user daemon-reload
systemctl --user enable --now remote-mcp-agent.service
systemctl --user status remote-mcp-agent.service
```

If the Agent must remain connected after the user's interactive logout, enable user lingering according to your OS policy. This does not grant root privileges; it only keeps the user's systemd manager running.

## Preflight

Run the matching offline doctor after writing each environment file:

```bash
remote-mcp-doctor gateway
remote-mcp-doctor mcp
remote-mcp-doctor agent
```

The bundled systemd units run the same checks automatically with `ExecStartPre`.

## Operational checks

Before connecting an MCP client, verify the Gateway `/healthz` locally, inspect both systemd services, confirm Caddy can reach the loopback ports, enroll one test Agent, and verify `list_devices` plus `ping_device`.

Then validate read-only tools such as `system_health`, `lookup_port`, `service_status`, filesystem discovery, and `git_status` before enabling any workflow that uses approval-gated mutations.

Keep the approval-admin credential outside MCP client configuration. Mutation approvals are intentionally an operator-side action.

For atomic local release selection and rollback, see `release-lifecycle.md`.

## Release integrity

Release trees are sealed with SHA-256 manifests before activation, and the service templates set `PYTHONDONTWRITEBYTECODE=1` so normal startup does not mutate the sealed tree. Keep `.git`, credentials, registry data, audit logs, and other mutable state outside release directories. See `release-integrity.md`.

## Signed activation

Production `activate` and `rollback` verify Ed25519 signatures against the root-level `trusted-release-keys/` directory. Keep private signing keys off the deployment host. Add new public keys before rotation and retain old keys while rollback targets still depend on them. See `release-signing.md`.

## Trusted package intake

Before a package bundle is used to prepare a host release, run `remote-mcp-package verify-trusted` against the separate package-publication trust store. Package-signing keys and release-signing keys are independent trust domains; use separate key pairs and directories. See `package-publication.md`.

## Runtime wheel inputs

Before constructing a deployment virtual environment, generate a `remote-mcp-runtime` bundle for the trusted package, sign it with the separate runtime-publication key, and accept it on the host with `verify-trusted`. The runtime lock fixes the CPython/Linux target, exact pip resolver, and every dependency wheel hash, then re-resolves the closure offline.

Package, runtime, and release signing use distinct keys and trust directories. See `runtime-input-locks.md` and `runtime-publication.md`.

## Artifact registry

Publish a verified package/runtime pair with `remote-mcp-artifact publish` and pin the returned artifact ID in deployment automation. Keep both trust directories outside the registry and run `remote-mcp-artifact verify` on the deployment host before constructing a release. See `artifact-registry.md`.
