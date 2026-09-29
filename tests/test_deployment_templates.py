from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def env_value(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"missing environment key: {key}")


def test_gateway_systemd_service_is_unprivileged_and_hardened() -> None:
    unit = read("deploy/systemd/remote-mcp-gateway.service")

    assert "User=remote-mcp-gateway" in unit
    assert "Group=remote-mcp-gateway" in unit
    assert "User=root" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "ProtectProc=invisible" in unit
    assert "ProcSubset=pid" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "ReadWritePaths=/var/lib/remote-mcp-commander" in unit


def test_mcp_systemd_service_is_unprivileged_and_depends_on_gateway() -> None:
    unit = read("deploy/systemd/remote-mcp-server.service")

    assert "User=remote-mcp-server" in unit
    assert "User=root" not in unit
    assert "Requires=remote-mcp-gateway.service" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "ProtectProc=invisible" in unit
    assert "ProcSubset=pid" in unit
    assert "CapabilityBoundingSet=" in unit


def test_agent_template_is_a_user_service_without_privilege_escalation() -> None:
    unit = read("deploy/systemd/remote-mcp-agent.user.service")

    assert "User=root" not in unit
    assert "EnvironmentFile=%h/.config/remote-mcp-commander/agent.env" in unit
    assert "ExecStart=%h/.local/share/remote-mcp-commander/" in unit
    assert "NoNewPrivileges=true" in unit
    assert "RestrictSUIDSGID=true" in unit
    assert "WantedBy=default.target" in unit


def test_gateway_and_mcp_examples_bind_only_to_loopback() -> None:
    gateway = read("deploy/gateway.env.example")
    mcp = read("deploy/mcp.env.example")

    assert env_value(gateway, "COMMANDER_BIND_HOST") == "127.0.0.1"
    assert env_value(mcp, "COMMANDER_MCP_HOST") == "127.0.0.1"
    assert env_value(mcp, "COMMANDER_GATEWAY_HTTP") == "http://127.0.0.1:8765"

    combined = gateway + mcp
    assert "0.0.0.0" not in combined
    assert "::" not in combined


def test_deployment_examples_do_not_embed_credentials() -> None:
    gateway = read("deploy/gateway.env.example")
    mcp = read("deploy/mcp.env.example")

    assert env_value(gateway, "COMMANDER_CONTROL_TOKEN") == ""
    assert env_value(gateway, "COMMANDER_APPROVAL_ADMIN_TOKEN") == ""
    assert env_value(mcp, "COMMANDER_CONTROL_TOKEN") == ""
    assert env_value(mcp, "COMMANDER_MCP_TOKEN") == ""
    assert env_value(gateway, "COMMANDER_AUDIT_FSYNC") == "true"


def test_mcp_public_metadata_urls_require_https_examples() -> None:
    mcp = read("deploy/mcp.env.example")

    assert env_value(mcp, "COMMANDER_MCP_TRANSPORT") == "streamable-http"
    assert env_value(mcp, "COMMANDER_MCP_ISSUER_URL").startswith("https://")
    assert env_value(mcp, "COMMANDER_MCP_RESOURCE_URL").startswith("https://")


def test_caddy_only_proxies_to_loopback_application_ports() -> None:
    caddy = read("deploy/caddy/Caddyfile.example")

    assert "reverse_proxy 127.0.0.1:8765" in caddy
    assert "reverse_proxy 127.0.0.1:8766" in caddy
    assert "0.0.0.0:8765" not in caddy
    assert "0.0.0.0:8766" not in caddy
    assert "Strict-Transport-Security" in caddy


def test_agent_example_requires_wss_and_explicit_allowed_roots() -> None:
    agent = read("deploy/agent.env.example")

    assert env_value(agent, "COMMANDER_GATEWAY_WS").startswith("wss://")
    roots = env_value(agent, "COMMANDER_ALLOWED_ROOTS_JSON")
    assert roots.startswith("[") and roots.endswith("]")
    assert roots != "[]"
