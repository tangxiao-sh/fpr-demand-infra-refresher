"""Configuration models and local validation for the Accessor CLI."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any, Sequence


class ConfigError(ValueError):
    """A configuration error that can be shown directly to a CLI user."""


@dataclasses.dataclass(frozen=True)
class RoleConfig:
    """One Granted/AWS profile that must be kept usable."""

    name: str
    profile: str
    refresh_seconds: int
    retry_seconds: int
    request_on_failure: bool
    credential_aliases: tuple[str, ...] = ()
    credential_source_profile: str | None = None

    @property
    def check_command(self) -> tuple[str, ...]:
        """Calling AWS CLI invokes Granted's configured credential_process."""
        return (
            "aws",
            "sts",
            "get-caller-identity",
            "--profile",
            self.profile,
            "--output",
            "json",
        )


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    """Information needed to reuse one project's existing proxy script."""

    name: str
    description: str
    directory: Path
    script: Path
    python: str
    arguments: tuple[str, ...]
    depends_on_role: str | None
    credential_refresh_seconds: int
    credential_retry_seconds: int
    restart_delay_seconds: int
    shutdown_grace_seconds: int

    @property
    def script_path(self) -> Path:
        """Resolve a relative script path inside the project checkout."""
        return self.script if self.script.is_absolute() else self.directory / self.script

    @property
    def proxy_command(self) -> tuple[str, ...]:
        """Run the project script normally, which starts sshuttle once."""
        return (self.python, str(self.script_path), *self.arguments)


@dataclasses.dataclass(frozen=True)
class Settings:
    """Global configuration plus every project selectable by the CLI."""

    config_path: Path
    auto_request: bool
    request_command: tuple[str, ...]
    command_timeout_seconds: int
    post_request_delay_seconds: int
    prepare_network_before_proxy: bool
    sshuttle_check_seconds: int
    lock_file: Path
    default_projects: tuple[str, ...]
    default_proxy: str | None
    roles: tuple[RoleConfig, ...]
    projects: tuple[ProjectConfig, ...]
    # Private endpoints that prove sshuttle forwarding works end to end.
    proxy_health_urls: tuple[str, ...] = ()
    # A project-owned credential script is isolated in a worker.  Its shorter
    # timeout prevents one stalled AWS call from starving every other project.
    project_command_timeout_seconds: int = 90

    @property
    def projects_by_name(self) -> dict[str, ProjectConfig]:
        return {project.name: project for project in self.projects}


def _positive_int(value: Any, field: str, default: int) -> int:
    """Read an interval while rejecting bool values, which Python treats as ints."""
    parsed = default if value is None else value
    if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return parsed


def _string_list(value: Any, field: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    """Read a TOML array of strings; empty arrays are valid for arguments."""
    parsed = default if value is None else value
    if not isinstance(parsed, (list, tuple)) or not all(
        isinstance(item, str) and item for item in parsed
    ):
        raise ConfigError(f"{field} must be an array of non-empty strings")
    return tuple(parsed)


def _read_roles(document: dict[str, Any]) -> tuple[RoleConfig, ...]:
    entries = document.get("roles", [])
    if not isinstance(entries, list) or not entries:
        raise ConfigError("at least one [[roles]] entry is required")

    roles: list[RoleConfig] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        field = f"roles[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{field} must be a table")
        name, profile = entry.get("name"), entry.get("profile")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{field}.name must be a non-empty string")
        if name in names:
            raise ConfigError(f"duplicate role name: {name}")
        if not isinstance(profile, str) or not profile:
            raise ConfigError(f"{field}.profile must be a non-empty string")
        request_on_failure = entry.get("request_on_failure", True)
        if not isinstance(request_on_failure, bool):
            raise ConfigError(f"{field}.request_on_failure must be true or false")
        aliases = _string_list(entry.get("credential_aliases"), f"{field}.credential_aliases")
        if profile in aliases or len(set(aliases)) != len(aliases):
            raise ConfigError(f"{field}.credential_aliases must be unique and exclude the main profile")
        source_profile = entry.get("credential_source_profile")
        if source_profile is not None and (not isinstance(source_profile, str) or not source_profile):
            raise ConfigError(f"{field}.credential_source_profile must be a non-empty string")
        names.add(name)
        roles.append(
            RoleConfig(
                name=name,
                profile=profile,
                refresh_seconds=_positive_int(
                    entry.get("refresh_seconds"), f"{field}.refresh_seconds", 600
                ),
                retry_seconds=_positive_int(
                    entry.get("retry_seconds"), f"{field}.retry_seconds", 60
                ),
                request_on_failure=request_on_failure,
                credential_aliases=aliases,
                credential_source_profile=source_profile,
            )
        )
    return tuple(roles)


def _read_projects(
    document: dict[str, Any], role_names: set[str]
) -> tuple[ProjectConfig, ...]:
    entries = document.get("projects", [])
    if not isinstance(entries, list) or not entries:
        raise ConfigError("at least one [[projects]] entry is required")

    projects: list[ProjectConfig] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        field = f"projects[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{field} must be a table")
        name, directory = entry.get("name"), entry.get("directory")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{field}.name must be a non-empty string")
        if name in names:
            raise ConfigError(f"duplicate project name: {name}")
        if not isinstance(directory, str) or not directory:
            raise ConfigError(f"{field}.directory must be a non-empty string")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"{field}.description must be a string")
        script = entry.get("script", "scripts/python/establish_proxy_connection.py")
        python = entry.get("python", "python3")
        if not isinstance(script, str) or not script:
            raise ConfigError(f"{field}.script must be a non-empty string")
        if not isinstance(python, str) or not python:
            raise ConfigError(f"{field}.python must be a non-empty string")
        dependency = entry.get("depends_on_role")
        if dependency is not None and (
            not isinstance(dependency, str) or dependency not in role_names
        ):
            raise ConfigError(
                f"{field}.depends_on_role must refer to a configured role name"
            )
        names.add(name)
        projects.append(
            ProjectConfig(
                name=name,
                description=description,
                directory=Path(directory).expanduser().resolve(),
                script=Path(script).expanduser(),
                python=python,
                arguments=_string_list(entry.get("arguments"), f"{field}.arguments"),
                depends_on_role=dependency,
                credential_refresh_seconds=_positive_int(
                    entry.get("credential_refresh_seconds"),
                    f"{field}.credential_refresh_seconds",
                    2700,
                ),
                credential_retry_seconds=_positive_int(
                    entry.get("credential_retry_seconds"),
                    f"{field}.credential_retry_seconds",
                    60,
                ),
                restart_delay_seconds=_positive_int(
                    entry.get("restart_delay_seconds"),
                    f"{field}.restart_delay_seconds",
                    10,
                ),
                shutdown_grace_seconds=_positive_int(
                    entry.get("shutdown_grace_seconds"),
                    f"{field}.shutdown_grace_seconds",
                    15,
                ),
            )
        )
    return tuple(projects)


def load_settings(config_path: Path) -> Settings:
    """Load TOML and validate only static values; this does not access AWS."""
    path = config_path.expanduser().resolve()
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(f"config file does not exist: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error

    general = document.get("general", {})
    if not isinstance(general, dict):
        raise ConfigError("[general] must be a table")
    roles = _read_roles(document)
    projects = _read_projects(document, {role.name for role in roles})
    project_names = {project.name for project in projects}

    auto_request = general.get("auto_request", True)
    if not isinstance(auto_request, bool):
        raise ConfigError("general.auto_request must be true or false")
    request_command = _string_list(
        general.get("request_command"),
        "general.request_command",
        # ``assume`` normally exports credentials into its current shell only.
        # Accessor runs it in a short-lived child shell, so export the result to
        # the named AWS profile as well; subsequent AWS/Boto processes can then
        # use the refreshed credentials.
        ("assume", "--wait", "--export"),
    )
    if auto_request and not request_command:
        raise ConfigError("general.request_command cannot be empty when auto_request is true")
    prepare_network = general.get("prepare_network_before_proxy", True)
    if not isinstance(prepare_network, bool):
        raise ConfigError("general.prepare_network_before_proxy must be true or false")
    defaults = _string_list(
        general.get("default_projects"), "general.default_projects", (projects[0].name,)
    )
    unknown_defaults = sorted(set(defaults) - project_names)
    if unknown_defaults:
        raise ConfigError(
            f"general.default_projects contains unknown project(s): {', '.join(unknown_defaults)}"
        )
    default_proxy = general.get("default_proxy")
    if default_proxy is not None and (
        not isinstance(default_proxy, str) or default_proxy not in project_names
    ):
        raise ConfigError("general.default_proxy must refer to a configured project")
    lock_file = general.get("lock_file", f"/tmp/accessor-{os.getuid()}.lock")
    if not isinstance(lock_file, str) or not lock_file:
        raise ConfigError("general.lock_file must be a non-empty string")

    return Settings(
        config_path=path,
        auto_request=auto_request,
        request_command=request_command,
        command_timeout_seconds=_positive_int(
            general.get("command_timeout_seconds"),
            "general.command_timeout_seconds",
            300,
        ),
        post_request_delay_seconds=_positive_int(
            general.get("post_request_delay_seconds"),
            "general.post_request_delay_seconds",
            2,
        ),
        prepare_network_before_proxy=prepare_network,
        sshuttle_check_seconds=_positive_int(
            general.get("sshuttle_check_seconds"),
            "general.sshuttle_check_seconds",
            300,
        ),
        lock_file=Path(lock_file).expanduser(),
        default_projects=defaults,
        default_proxy=default_proxy,
        roles=roles,
        projects=projects,
        proxy_health_urls=_string_list(
            general.get("proxy_health_urls"), "general.proxy_health_urls"
        ),
        project_command_timeout_seconds=_positive_int(
            general.get("project_command_timeout_seconds"),
            "general.project_command_timeout_seconds",
            90,
        ),
    )


def select_projects(
    settings: Settings, requested_names: Sequence[str], select_all: bool
) -> tuple[ProjectConfig, ...]:
    """Resolve repeated --project flags to ordered, unique project objects."""
    if requested_names and select_all:
        raise ConfigError("use either --project or --all-projects, not both")
    names = [project.name for project in settings.projects] if select_all else list(requested_names)
    if not names:
        names = list(settings.default_projects)
    known, selected, seen = settings.projects_by_name, [], set()
    for name in names:
        if name not in known:
            raise ConfigError(f"unknown project: {name}; run `accessor projects` to list names")
        if name not in seen:
            selected.append(known[name])
            seen.add(name)
    return tuple(selected)


def select_proxy_project(
    settings: Settings,
    projects: Sequence[ProjectConfig],
    requested_name: str | None,
    disable_proxy: bool,
) -> ProjectConfig | None:
    """Choose one tunnel owner because project sshuttle routes overlap."""
    if disable_proxy and requested_name:
        raise ConfigError("use either --proxy or --no-proxy, not both")
    if disable_proxy:
        return None
    candidate = requested_name
    if candidate is None and len(projects) == 1:
        candidate = projects[0].name
    if candidate is None:
        candidate = settings.default_proxy
    if candidate is None:
        raise ConfigError("multiple projects selected; choose one with --proxy NAME")
    selected = {project.name: project for project in projects}
    if candidate in selected:
        return selected[candidate]
    # The configured default is the shared Demand Proxy connector. It is an
    # implementation detail for locating the jump host, not a requirement that
    # its own service credentials are selected for refresh.
    if candidate == settings.default_proxy:
        return settings.projects_by_name[candidate]
    raise ConfigError(f"unknown proxy connector: {candidate}")


def _command_exists(command: str) -> bool:
    return Path(command).expanduser().is_file() if os.sep in command else shutil.which(command) is not None


def validate_selection(
    settings: Settings, projects: Sequence[ProjectConfig], check_python_imports: bool
) -> list[str]:
    """Validate local paths and Python dependencies without touching AWS."""
    errors: list[str] = []
    commands = {"aws"}
    if settings.auto_request:
        commands.add(settings.request_command[0])
    if settings.proxy_health_urls:
        commands.add("curl")
    for project in projects:
        commands.add(project.python)
        if not project.directory.is_dir():
            errors.append(f"project {project.name}: directory does not exist: {project.directory}")
        if not project.script_path.is_file():
            errors.append(f"project {project.name}: proxy script does not exist: {project.script_path}")
    for command in sorted(commands):
        if not _command_exists(command):
            errors.append(f"command not found: {command}")

    # The project's own interpreter must import boto3 because it runs the
    # credential worker and the original proxy script.
    if check_python_imports:
        for project in projects:
            if not _command_exists(project.python):
                continue
            try:
                result = subprocess.run(
                    [project.python, "-c", "import boto3"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(f"project {project.name}: failed to check boto3: {error}")
                continue
            if result.returncode != 0:
                detail = (result.stderr or "").strip().splitlines()
                suffix = f": {detail[-1]}" if detail else ""
                errors.append(f"project {project.name}: {project.python} cannot import boto3{suffix}")
    return errors
