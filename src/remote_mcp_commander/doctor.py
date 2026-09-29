from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from remote_mcp_commander.agent.capabilities import detect_capabilities
from remote_mcp_commander.agent.file_ops import allowed_roots
from remote_mcp_commander.config import Settings, get_settings

CheckStatus = Literal["pass", "warn", "fail"]
Role = Literal["gateway", "mcp", "agent"]
CheckAction = Callable[[], str | None]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


def _check(name: str, action: CheckAction) -> CheckResult:
    try:
        detail = action()
    except (OSError, ValueError) as exc:
        return CheckResult(name=name, status="fail", detail=str(exc))
    return CheckResult(name=name, status="pass", detail=detail or "ok")


def _destination_check(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("configured state file must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("configured state path is not a regular file")

    probe = path.parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.exists() or not probe.is_dir():
        raise ValueError("no existing parent directory is available")
    if not os.access(probe, os.W_OK):
        raise ValueError("state path parent is not writable by the current user")
    return "state path can be created or updated"


def _loopback_bind_check(name: str, host: str) -> CheckResult:
    if host in LOOPBACK_HOSTS:
        return CheckResult(name, "pass", "bind is loopback-only")
    return CheckResult(
        name,
        "warn",
        "bind is not loopback-only; terminate TLS at a trusted reverse proxy",
    )


def check_gateway(settings: Settings) -> list[CheckResult]:
    results = [
        _check(
            "gateway.security",
            lambda: (settings.validate_gateway_security(), "security settings are valid")[1],
        ),
        _check("gateway.audit_path", lambda: _destination_check(settings.audit_file)),
        _check("gateway.registry_path", lambda: _destination_check(settings.registry_file)),
        _loopback_bind_check("gateway.bind", settings.bind_host),
    ]
    if settings.approval_admin_token:
        results.append(
            CheckResult("gateway.approval", "pass", "approval admin credential is configured")
        )
    else:
        results.append(
            CheckResult(
                "gateway.approval",
                "warn",
                "approval admin credential is empty; mutation approvals cannot be issued",
            )
        )
    return results


def check_mcp(settings: Settings) -> list[CheckResult]:
    results = [
        _check(
            "mcp.gateway_security",
            lambda: (
                settings.validate_mcp_gateway_security(),
                "Gateway control connection is valid",
            )[1],
        ),
        _check(
            "mcp.http_security",
            lambda: (settings.validate_mcp_http_security(), "MCP transport security is valid")[1],
        ),
    ]
    if settings.mcp_transport == "streamable-http":
        results.append(_loopback_bind_check("mcp.bind", settings.mcp_host))
        results.append(
            CheckResult(
                "mcp.authentication",
                "pass" if settings.mcp_auth_mode == "oauth" else "warn",
                (
                    "single-user OAuth configured; run remote-mcp-connect-check after deployment"
                    if settings.mcp_auth_mode == "oauth"
                    else "static bearer auth is for token-capable clients, not ChatGPT web linking"
                ),
            )
        )
    else:
        results.append(CheckResult("mcp.bind", "pass", "stdio transport has no network listener"))
    return results


def _agent_security_check(settings: Settings) -> str:
    token = settings.load_agent_token()
    settings.validate_agent_security(token)
    return "Agent credential and Gateway transport are valid"


def check_agent(settings: Settings) -> list[CheckResult]:
    results = [_check("agent.security", lambda: _agent_security_check(settings))]
    try:
        roots = allowed_roots(settings.allowed_roots)
    except (OSError, ValueError) as exc:
        results.append(CheckResult("agent.allowed_roots", "fail", str(exc)))
        return results

    if roots:
        results.append(
            CheckResult("agent.allowed_roots", "pass", f"{len(roots)} allowed root(s) validated")
        )
    else:
        results.append(
            CheckResult(
                "agent.allowed_roots",
                "warn",
                "no allowed roots configured; filesystem and Git tools stay disabled",
            )
        )

    capabilities = detect_capabilities(settings, roots)
    detail = ", ".join(capabilities) if capabilities else "no runtime capabilities detected"
    results.append(CheckResult("agent.capabilities", "pass", detail))
    return results


def run_checks(role: Role, settings: Settings) -> list[CheckResult]:
    if role == "gateway":
        return check_gateway(settings)
    if role == "mcp":
        return check_mcp(settings)
    return check_agent(settings)


def _result_payload(role: Role, results: list[CheckResult]) -> dict[str, object]:
    failures = sum(result.status == "fail" for result in results)
    warnings = sum(result.status == "warn" for result in results)
    return {
        "role": role,
        "ok": failures == 0,
        "failures": failures,
        "warnings": warnings,
        "checks": [asdict(result) for result in results],
    }


def _print_text(role: Role, results: list[CheckResult]) -> None:
    print(f"Remote MCP Commander doctor: {role}")
    for result in results:
        print(f"[{result.status.upper():4}] {result.name}: {result.detail}")
    payload = _result_payload(role, results)
    print(
        f"Summary: {len(results)} checks, "
        f"{payload['failures']} failed, {payload['warnings']} warning(s)"
    )


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Remote MCP Commander deployment settings"
    )
    parser.add_argument("role", choices=("gateway", "mcp", "agent"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    role: Role = args.role

    try:
        effective_settings = settings or get_settings()
        results = run_checks(role, effective_settings)
    except ValueError as exc:
        results = [CheckResult("configuration", "fail", str(exc))]

    payload = _result_payload(role, results)
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_text(role, results)
    return 0 if payload["ok"] else 1


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
