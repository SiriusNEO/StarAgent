from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from staragent.codex_app_server import codex_app_server_requests
from staragent.event_log import redact_log_text
from staragent.harness_config import harness_process_environment
from staragent.text import strip_ansi
from staragent.windows import background_process_kwargs, windows_process_argv

AGENT_AUTH_TIMEOUT_SECONDS = 3.0
CODEX_DOCTOR_TIMEOUT_SECONDS = 4.0
CODEX_LOGIN_TIMEOUT_SECONDS = 30.0
CODEX_BROWSER_CALLBACK_TIMEOUT_SECONDS = 30.0
CODEX_BROWSER_CALLBACK_PORTS = frozenset({1455, 1457})
AGENT_LOGOUT_TIMEOUT_SECONDS = 10.0
AGENT_AUTH_STATUSES = {
    "authenticated",
    "not_authenticated",
    "configured",
    "not_configured",
    "unavailable",
    "error",
    "unknown",
}
AGENT_CREDENTIAL_TYPES = {
    "api_key",
    "bearer_token",
    "chatgpt",
    "command",
    "environment",
    "external",
    "none",
    "unknown",
}
AUTH_ACTIONS = {
    "codex": "codex login",
    "claude": "claude auth login",
    "opencode": "opencode auth login",
}
CODEX_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "amazon-bedrock": "Amazon Bedrock",
}
AGENT_LOGOUT_ARGS = {
    "codex": ("logout",),
    "claude": ("auth", "logout"),
}
AGENT_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
}
CLAUDE_CLOUD_ENVIRONMENTS = (
    ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI"),
    ("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"),
)
CLAUDE_CREDENTIAL_ENVIRONMENTS = (
    ("ANTHROPIC_AUTH_TOKEN", "Anthropic bearer token"),
    ("ANTHROPIC_API_KEY", "Anthropic API key"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "Claude OAuth token"),
    ("ANTHROPIC_PROFILE", "Anthropic profile"),
)


def probe_agent_auth(agent: str, executable: str) -> dict[str, object]:
    if agent == "codex":
        return probe_codex_auth(executable)
    if agent == "claude":
        return probe_claude_auth(executable)
    if agent == "opencode":
        return probe_opencode_auth(executable)
    return unknown_agent_auth(agent, "Access detection is not supported for this CLI.")


def probe_codex_auth(executable: str) -> dict[str, object]:
    environment = auth_environment("codex")
    try:
        responses = codex_app_server_requests(
            executable,
            {
                "account/read": {"refreshToken": False},
                "config/read": {"includeLayers": False},
            },
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=environment,
        )
        status = codex_auth_from_app_server(responses, environment)
        if status is not None:
            return status
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
        pass

    doctor_status = probe_codex_doctor_auth(executable, environment)
    if doctor_status is not None:
        return doctor_status
    return probe_codex_login_auth(executable, environment)


def codex_auth_from_app_server(
    responses: object,
    environment: dict[str, str],
) -> dict[str, object] | None:
    if not isinstance(responses, dict):
        return None
    account_result = responses.get("account/read")
    config_result = responses.get("config/read")
    if not isinstance(account_result, dict) or not isinstance(config_result, dict):
        return None
    config = config_result.get("config")
    requires_openai_auth = account_result.get("requiresOpenaiAuth")
    if not isinstance(config, dict) or not isinstance(requires_openai_auth, bool):
        return None

    provider_id, provider_name, provider_config = codex_provider(config)
    account = account_result.get("account")
    if requires_openai_auth:
        if isinstance(account, dict):
            credential_type, method = codex_account_credential(account)
            return agent_auth_status(
                "codex",
                status="authenticated",
                source="codex-app-server",
                provider=provider_name,
                credential_type=credential_type,
                method=method,
            )
        return agent_auth_status(
            "codex",
            status="not_authenticated",
            source="codex-app-server",
            provider=provider_name,
            action=AUTH_ACTIONS["codex"],
            detail="Codex requires an OpenAI login for the active provider.",
        )

    if isinstance(account, dict):
        credential_type, method = codex_account_credential(account)
        return agent_auth_status(
            "codex",
            status="configured",
            source="codex-app-server",
            provider=provider_name,
            credential_type=credential_type,
            method=method,
        )

    env_key = clean_environment_name(
        provider_config.get("env_key") or provider_config.get("envKey")
    )
    if env_key:
        return agent_auth_status(
            "codex",
            status="configured" if environment.get(env_key) else "not_configured",
            source="codex-app-server",
            provider=provider_name,
            credential_type="environment",
            credential_name=env_key,
        )

    command_auth = provider_config.get("auth")
    if isinstance(command_auth, dict) and clean_auth_text(command_auth.get("command")):
        return agent_auth_status(
            "codex",
            status="configured",
            source="codex-app-server",
            provider=provider_name,
            credential_type="command",
        )
    if provider_config.get("experimental_bearer_token") or provider_config.get(
        "experimentalBearerToken"
    ):
        return agent_auth_status(
            "codex",
            status="configured",
            source="codex-app-server",
            provider=provider_name,
            credential_type="bearer_token",
        )
    return agent_auth_status(
        "codex",
        status="configured",
        source="codex-app-server",
        provider=provider_name or provider_id,
        credential_type="none",
    )


def codex_provider(config: dict[str, object]) -> tuple[str, str, dict[str, object]]:
    provider_id = clean_auth_text(
        config.get("model_provider") or config.get("modelProvider") or "openai",
        max_chars=80,
    )
    provider_lookup = provider_id.lower()
    provider_maps = config.get("model_providers") or config.get("modelProviders")
    provider_config: dict[str, object] = {}
    if isinstance(provider_maps, dict):
        candidate = provider_maps.get(provider_id)
        if not isinstance(candidate, dict):
            candidate = next(
                (
                    value
                    for key, value in provider_maps.items()
                    if str(key).lower() == provider_lookup and isinstance(value, dict)
                ),
                None,
            )
        if isinstance(candidate, dict):
            provider_config = candidate
    provider_name = clean_auth_text(provider_config.get("name"), max_chars=80)
    if not provider_name:
        provider_name = CODEX_PROVIDER_LABELS.get(provider_lookup, provider_id)
    return provider_id, provider_name, provider_config


def codex_account_credential(account: dict[str, object]) -> tuple[str, str]:
    account_type = re.sub(r"[^a-z]", "", str(account.get("type") or "").lower())
    if account_type == "chatgpt":
        return "chatgpt", "ChatGPT"
    if account_type == "apikey":
        return "api_key", "OpenAI API key"
    return "external", ""


def probe_codex_doctor_auth(
    executable: str,
    environment: dict[str, str],
) -> dict[str, object] | None:
    try:
        result = subprocess.run(
            auth_process_argv(executable, "doctor", "--json", "--summary", env=environment),
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=CODEX_DOCTOR_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return codex_auth_from_doctor(payload)


def codex_auth_from_doctor(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return None
    config_check = checks.get("config.load")
    auth_check = checks.get("auth.credentials")
    if not isinstance(config_check, dict) or not isinstance(auth_check, dict):
        return None
    config_details = config_check.get("details")
    auth_details = auth_check.get("details")
    if not isinstance(config_details, dict) or not isinstance(auth_details, dict):
        return None

    provider_id = clean_auth_text(config_details.get("model provider"), max_chars=80).lower()
    if not provider_id:
        return None
    provider_name = codex_doctor_provider_name(checks, provider_id)
    source = "codex-doctor"
    env_match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*)\s+\((present|missing)\)",
        str(auth_details.get("provider auth env var") or "").strip(),
        flags=re.IGNORECASE,
    )
    if env_match:
        env_key, state = env_match.groups()
        return agent_auth_status(
            "codex",
            status="configured" if state.lower() == "present" else "not_configured",
            source=source,
            provider=provider_name,
            credential_type="environment",
            credential_name=env_key,
        )

    requires_value = str(auth_details.get("model provider requires OpenAI auth") or "").lower()
    requires_openai_auth = provider_id == "openai" or requires_value == "true"
    check_status = str(auth_check.get("status") or "").lower()
    if requires_openai_auth:
        if check_status == "ok":
            stored_mode = str(auth_details.get("stored auth mode") or "")
            credential_type, method = codex_account_credential({"type": stored_mode})
            return agent_auth_status(
                "codex",
                status="authenticated",
                source=source,
                provider=provider_name,
                credential_type=credential_type,
                method=method,
            )
        if check_status == "fail":
            return agent_auth_status(
                "codex",
                status="not_authenticated",
                source=source,
                provider=provider_name,
                action=AUTH_ACTIONS["codex"],
                detail="Codex requires an OpenAI login for the active provider.",
            )
        return None
    if check_status == "ok":
        return agent_auth_status(
            "codex",
            status="configured",
            source=source,
            provider=provider_name,
            credential_type="none",
        )
    if check_status == "fail":
        return agent_auth_status(
            "codex",
            status="not_configured",
            source=source,
            provider=provider_name,
        )
    return None


def codex_doctor_provider_name(checks: dict[str, object], provider_id: str) -> str:
    network_check = checks.get("network.websocket_reachability")
    if isinstance(network_check, dict):
        details = network_check.get("details")
        if isinstance(details, dict):
            name = clean_auth_text(details.get("provider name"), max_chars=80)
            if name:
                return name
    return CODEX_PROVIDER_LABELS.get(provider_id, provider_id)


def probe_codex_login_auth(
    executable: str,
    environment: dict[str, str],
) -> dict[str, object]:
    try:
        result = subprocess.run(
            auth_process_argv(executable, "login", "status", env=environment),
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("codex", "Codex login check timed out.")
    except OSError as exc:
        return auth_error("codex", exc)

    output = redact_log_text(
        first_auth_line(result.stdout, result.stderr),
        max_chars=300,
    )
    if result.returncode == 0:
        method = ""
        match = re.search(r"logged\s+in\s+using\s+(.+)$", output, flags=re.IGNORECASE)
        if match:
            method = clean_auth_text(match.group(1), max_chars=80)
        return agent_auth_status(
            "codex",
            status="authenticated",
            source="codex-login-status",
            provider="OpenAI",
            credential_type=codex_login_credential_type(method),
            method=method,
            detail="Codex reports an active login.",
        )
    if "not logged in" in output.lower():
        return agent_auth_status(
            "codex",
            status="not_authenticated",
            source="codex-login-status",
            provider="OpenAI",
            action=AUTH_ACTIONS["codex"],
            detail="Run Codex login to authenticate this service account.",
        )
    return auth_error(
        "codex",
        output or f"Codex login check exited with code {result.returncode}.",
        source="codex-login-status",
    )


def codex_login_credential_type(method: str) -> str:
    normalized = method.lower()
    if "chatgpt" in normalized:
        return "chatgpt"
    if "api" in normalized and "key" in normalized:
        return "api_key"
    return "unknown"


def login_codex_with_api_key(api_key: str, executable: str = "") -> dict[str, object]:
    secret = api_key.strip()
    if not secret:
        raise ValueError("An OpenAI API key is required.")
    if len(secret) > 8192:
        raise ValueError("The OpenAI API key is too long.")

    environment = auth_environment("codex")
    command = executable or shutil.which("codex", path=environment.get("PATH")) or ""
    if not command:
        raise ValueError("Codex is not installed in the Node service PATH.")
    try:
        result = subprocess.run(
            auth_process_argv(command, "login", "--with-api-key", env=environment),
            check=False,
            input=f"{secret}\n",
            text=True,
            capture_output=True,
            timeout=CODEX_LOGIN_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "error",
            "detail": "Codex API key login timed out.",
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": "error",
            "detail": clean_auth_text(exc),
        }

    stdout = (result.stdout or "").replace(secret, "[REDACTED]")
    stderr = (result.stderr or "").replace(secret, "[REDACTED]")
    output = first_auth_line(stdout, stderr)
    if result.returncode != 0:
        return {
            "ok": False,
            "status": "error",
            "detail": redact_log_text(
                output or f"Codex login exited with code {result.returncode}.",
                max_chars=300,
            ),
        }
    return {
        "ok": True,
        "status": "authenticated",
        "credential_type": "api_key",
        "detail": "Codex accepted and stored the API key on this Node.",
    }


def relay_codex_browser_callback(callback_url: str) -> dict[str, object]:
    """Deliver Codex's browser callback to the Node-local login server.

    Codex registers a loopback OAuth redirect, so a browser on another machine
    cannot reach it directly. Keep this relay deliberately narrow: it may only
    contact Codex's two registered loopback ports and callback/success paths.
    """
    callback = _codex_loopback_target(callback_url, expected_path="/auth/callback")
    if not callback.query_has_result:
        raise ValueError("The Codex callback URL is missing its authorization result.")

    try:
        status, location = _request_codex_loopback(callback)
        if 300 <= status < 400:
            if not location:
                return _codex_callback_rejected()
            redirect = _codex_loopback_redirect(location, callback.port)
            if redirect is not None:
                status, _ = _request_codex_loopback(redirect)
            else:
                status = 200
        if not 200 <= status < 300:
            return _codex_callback_rejected()
    except (ConnectionError, OSError, TimeoutError, http.client.HTTPException):
        return {
            "ok": False,
            "status": "error",
            "detail": (
                "No matching Codex browser login is waiting on this Node. "
                "Start browser login here, finish it in the browser, then paste the URL again."
            ),
        }

    return {
        "ok": True,
        "status": "pending",
        "detail": "The browser callback reached Codex. Waiting for Codex to finish signing in.",
    }


def _codex_callback_rejected() -> dict[str, object]:
    return {
        "ok": False,
        "status": "error",
        "detail": (
            "Codex rejected that callback. Start a new browser login and paste "
            "the latest localhost URL."
        ),
    }


@dataclass(frozen=True)
class _CodexLoopbackTarget:
    port: int
    request_target: str
    query_has_result: bool = False


def _codex_loopback_target(
    value: str,
    *,
    expected_path: str,
    expected_port: int | None = None,
) -> _CodexLoopbackTarget:
    raw = str(value or "").strip()
    if not raw or len(raw) > 16_384:
        raise ValueError("Paste the complete Codex localhost callback URL.")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The Codex callback URL is invalid.") from exc
    if (
        parsed.scheme.lower() != "http"
        or (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in CODEX_BROWSER_CALLBACK_PORTS
        or (expected_port is not None and port != expected_port)
        or parsed.path != expected_path
    ):
        raise ValueError("Only a Codex localhost callback URL is accepted.")
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query_has_result = any(params.get("state", ())) and (
        any(params.get("code", ())) or any(params.get("error", ()))
    )
    request_target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return _CodexLoopbackTarget(
        port=port,
        request_target=request_target,
        query_has_result=query_has_result,
    )


def _codex_loopback_redirect(value: str, port: int) -> _CodexLoopbackTarget | None:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme.lower() == "https":
        # Recent Codex versions can use a hosted success page. Credentials have
        # already been persisted before this redirect is returned.
        return None
    return _codex_loopback_target(
        value,
        expected_path="/success",
        expected_port=port,
    )


def _request_codex_loopback(target: _CodexLoopbackTarget) -> tuple[int, str]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        target.port,
        timeout=CODEX_BROWSER_CALLBACK_TIMEOUT_SECONDS,
    )
    try:
        connection.request(
            "GET",
            target.request_target,
            headers={"Host": f"localhost:{target.port}", "User-Agent": "StarAgent"},
        )
        response = connection.getresponse()
        status = int(response.status)
        location = str(response.getheader("Location") or "")
        response.read(64 * 1024)
        return status, location
    finally:
        connection.close()


def logout_agent(agent: str, executable: str = "") -> dict[str, object]:
    args = AGENT_LOGOUT_ARGS.get(agent)
    if args is None:
        raise ValueError(f"Interactive logout is required for Agent CLI: {agent}")
    label = AGENT_LABELS.get(agent, agent)
    environment = auth_environment(agent)
    command = executable or shutil.which(agent, path=environment.get("PATH")) or ""
    if not command:
        raise ValueError(f"{label} is not installed in the Node service PATH.")
    try:
        result = subprocess.run(
            auth_process_argv(command, *args, env=environment),
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_LOGOUT_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "error",
            "detail": f"{label} logout timed out.",
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": "error",
            "detail": clean_auth_text(exc),
        }
    output = redact_log_text(
        first_auth_line(result.stdout, result.stderr),
        max_chars=300,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "status": "error",
            "detail": output or f"{label} logout exited with code {result.returncode}.",
        }
    return {
        "ok": True,
        "status": "not_authenticated",
        "detail": f"{label} credentials were removed from this Node.",
    }


def probe_claude_auth(executable: str) -> dict[str, object]:
    environment = auth_environment("claude")
    configured = claude_environment_auth(environment)
    if configured is not None:
        return configured
    try:
        result = subprocess.run(
            auth_process_argv(executable, "auth", "status", "--json", env=environment),
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("claude", "Claude authentication check timed out.")
    except OSError as exc:
        return auth_error("claude", exc)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        logged_in = payload.get("loggedIn")
        if isinstance(logged_in, bool):
            return agent_auth_status(
                "claude",
                status="authenticated" if logged_in else "not_authenticated",
                source="claude-auth-status",
                method=clean_auth_text(payload.get("authMethod"), max_chars=80),
                action="" if logged_in else AUTH_ACTIONS["claude"],
                detail=(
                    "Claude Code reports an active login."
                    if logged_in
                    else "Run Claude authentication to sign in this service account."
                ),
            )
    output = first_auth_line(result.stderr, result.stdout)
    return auth_error(
        "claude",
        output or f"Claude authentication check exited with code {result.returncode}.",
        source="claude-auth-status",
    )


def claude_environment_auth(environment: dict[str, str]) -> dict[str, object] | None:
    for name, provider in CLAUDE_CLOUD_ENVIRONMENTS:
        value = environment.get(name, "").strip().lower()
        if value and value not in {"0", "false", "no", "off"}:
            return agent_auth_status(
                "claude",
                status="configured",
                source="claude-environment",
                provider=provider,
                credential_type="environment",
                credential_name=name,
                method="Cloud provider credentials",
                detail=f"Claude Code is configured to use {provider} on this Node.",
            )
    for name, method in CLAUDE_CREDENTIAL_ENVIRONMENTS:
        if environment.get(name, "").strip():
            return agent_auth_status(
                "claude",
                status="configured",
                source="claude-environment",
                provider="Anthropic",
                credential_type="environment",
                credential_name=name,
                method=method,
                detail=f"Claude Code found {name} in its managed environment.",
            )
    return None


def probe_opencode_auth(executable: str) -> dict[str, object]:
    environment = auth_environment("opencode")
    try:
        result = subprocess.run(
            auth_process_argv(executable, "auth", "list", env=environment),
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=AGENT_AUTH_TIMEOUT_SECONDS,
            env=environment,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return auth_error("opencode", "OpenCode authentication check timed out.")
    except OSError as exc:
        return auth_error("opencode", exc)

    output = strip_ansi(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0:
        return auth_error(
            "opencode",
            first_auth_line(result.stderr, result.stdout)
            or f"OpenCode authentication check exited with code {result.returncode}.",
            source="opencode-auth-list",
        )
    counts = [
        int(value)
        for value in re.findall(
            r"\b(\d+)\s+(?:credentials?|environment\s+variables?)\b",
            output,
            flags=re.IGNORECASE,
        )
    ]
    if counts:
        provider_count = sum(counts)
        if provider_count:
            noun = "credential source" if provider_count == 1 else "credential sources"
            return agent_auth_status(
                "opencode",
                status="configured",
                source="opencode-auth-list",
                method="Provider credentials",
                provider_count=provider_count,
                detail=f"OpenCode reports configured {noun}.",
            )
        return agent_auth_status(
            "opencode",
            status="not_configured",
            source="opencode-auth-list",
            action=AUTH_ACTIONS["opencode"],
            detail="OpenCode reports no configured provider credentials.",
        )
    if "no credentials" in output.lower():
        return agent_auth_status(
            "opencode",
            status="not_configured",
            source="opencode-auth-list",
            action=AUTH_ACTIONS["opencode"],
            detail="OpenCode reports no configured provider credentials.",
        )
    return unknown_agent_auth(
        "opencode",
        "OpenCode did not report a credential count.",
        source="opencode-auth-list",
    )


def agent_auth_status(
    agent: str,
    *,
    status: str,
    source: str = "",
    provider: str = "",
    credential_type: str = "unknown",
    credential_name: str = "",
    method: str = "",
    action: str = "",
    detail: str = "",
    provider_count: int = 0,
) -> dict[str, object]:
    return normalize_agent_auth(
        agent,
        {
            "status": status,
            "source": source,
            "checked_at": utc_timestamp(),
            "provider": provider,
            "credential_type": credential_type,
            "credential_name": credential_name,
            "method": method,
            "action": action,
            "detail": detail,
            "provider_count": provider_count,
        },
    )


def normalize_agent_auth(agent: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return unknown_agent_auth(agent)
    status = str(value.get("status") or "unknown").strip().lower()
    if status not in AGENT_AUTH_STATUSES:
        status = "unknown"
    allowed_action = AUTH_ACTIONS.get(agent, "")
    action = clean_auth_text(value.get("action"), max_chars=100)
    if action != allowed_action:
        action = ""
    authenticated: bool | None = None
    if status == "authenticated":
        authenticated = True
    elif status == "not_authenticated":
        authenticated = False
    credential_type = str(value.get("credential_type") or "unknown").strip().lower()
    if credential_type not in AGENT_CREDENTIAL_TYPES:
        credential_type = "unknown"
    credential_name = (
        clean_environment_name(value.get("credential_name"))
        if credential_type == "environment"
        else ""
    )
    return {
        "status": status,
        "authenticated": authenticated,
        "source": clean_auth_text(value.get("source"), max_chars=80),
        "checked_at": clean_auth_text(value.get("checked_at"), max_chars=80),
        "provider": clean_auth_text(value.get("provider"), max_chars=80),
        "credential_type": credential_type,
        "credential_name": credential_name,
        "method": clean_auth_text(value.get("method"), max_chars=80),
        "action": action,
        "detail": clean_auth_text(value.get("detail"), max_chars=300),
        "provider_count": max(0, min(100, safe_int(value.get("provider_count")))),
    }


def unknown_agent_auth(
    agent: str,
    detail: str = (
        "Access state was not reported by this Node; update StarAgent there if it remains unknown."
    ),
    *,
    source: str = "",
) -> dict[str, object]:
    return agent_auth_status(agent, status="unknown", source=source, detail=detail)


def unavailable_agent_auth(agent: str, detail: str) -> dict[str, object]:
    return agent_auth_status(agent, status="unavailable", detail=detail)


def auth_error(agent: str, detail: object, *, source: str = "") -> dict[str, object]:
    return agent_auth_status(agent, status="error", source=source, detail=clean_auth_text(detail))


def auth_environment(agent: str = "") -> dict[str, str]:
    environment = harness_process_environment(agent) if agent else os.environ.copy()
    return {
        **environment,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def auth_process_argv(executable: str, *args: str, env: dict[str, str]) -> list[str]:
    command = [executable, *args]
    if os.name == "nt":
        return windows_process_argv(command, env, require_executable=True)
    return command


def first_auth_line(*values: str) -> str:
    lines = []
    for value in values:
        lines.extend(line.strip() for line in strip_ansi(value or "").splitlines() if line.strip())
    preferred = [line for line in lines if not line.lower().startswith("warning:")]
    return clean_auth_text((preferred or lines)[-1] if lines else "")


def clean_auth_text(value: Any, *, max_chars: int = 240) -> str:
    text = " ".join(strip_ansi(str(value or "")).replace("\x00", "").split())
    return f"{text[:max_chars]}…" if len(text) > max_chars else text


def clean_environment_name(value: object) -> str:
    name = str(value or "").strip()
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else ""


def safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
