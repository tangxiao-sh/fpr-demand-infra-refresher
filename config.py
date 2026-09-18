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
    """AWS service credential target; it has no local checkout dependency."""

    name: str
    description: str
    depends_on_role: str | None
    credential_refresh_seconds: int
    credential_retry_seconds: int
    restart_delay_seconds: int
    shutdown_grace_seconds: int
    service_name: str = ""
    credential_profile: str = "LocalStagingJumpRole@tvlk-fpr-dev"
    proxy_service_name: str = ""
    # Both deployment modes are kept on each project.  The active legacy
    # fields above remain the compatibility view consumed by the runtime.
    dev_credential_profile: str = "LocalStagingJumpRole@tvlk-fpr-dev"
    dev_depends_on_role: str | None = None
    dev_proxy_group: str = "demand"
    legacy_credential_profile: str = "LocalStagingJumpRole@tvlk-fpr-stg"
    legacy_depends_on_role: str | None = "local-staging-jump"
    legacy_proxy_group: str = "fprpapi"
    ecs_cluster_name: str = ""
    ec2_cluster_tag: str | None = None
    discovery_tag: str = "service"
    discovery_value: str = ""
    session_name_mode: str = "granted_sso"
    session_name_prefix: str = "local_testing"


@dataclasses.dataclass(frozen=True)
class ProxyConfig:
    """Shared Demand Proxy settings extracted from the reference behavior."""

    mode: str = "blaze"
    profile: str = "LocalStagingJumpRole@tvlk-fpr-dev"
    service_name: str = "fprpapi"
    parameter_mapping: str = "/tvlk-secret/fprprxy/fpr/demand/proxy-instance-mapping"
    environment: str = "development"
    group: str = "demand"
    region: str = "ap-southeast-1"
    exclude_cidr: str = "172.17.0.0/16"
    subnets: tuple[str, ...] = (
        "172.16.0.0/12",
        "10.0.0.0/8",
        "192.168.0.0/16",
    )
    ssh_user: str = "ubuntu"


@dataclasses.dataclass(frozen=True)
class BuildArtifactsConfig:
    """Local Gradle and Docker access derived from the build-role session."""

    # Keep this disabled unless explicitly configured. Existing user configs
    # therefore retain their previous behavior when upgrading Accessor.
    enabled: bool = False
    role_name: str = "build-artifact-reader"
    profile: str = "beiartf"
    region: str = "ap-southeast-1"
    codeartifact_domain: str = ""
    codeartifact_domain_owner: str = ""
    gradle_property: str = "external_cache_codeartifact_token"
    gradle_properties_path: Path = Path("~/.gradle/gradle.properties")
    ecr_registry_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class SsoPopulateConfig:
    """Granted SSO profile discovery used when a configured profile is absent."""

    enabled: bool = False
    start_url: str = "https://tvlk.awsapps.com/start/"
    sso_region: str = "ap-southeast-1"
    profile_template: str = "{{ .RoleName }}@{{ .AccountName }}"


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
    proxy: ProxyConfig = ProxyConfig()
    previous_proxy: ProxyConfig = ProxyConfig(
        mode="ssm_mapping",
        profile="LocalStagingJumpRole@tvlk-fpr-stg",
        service_name="fprpapi",
    )
    previous_proxy_role_name: str = "local-staging-jump"
    previous_proxy_default_proxy: str | None = None
    previous_proxy_health_urls: tuple[str, ...] = ()
    previous_proxy_reuse_existing: bool = True
    build_artifacts: BuildArtifactsConfig = BuildArtifactsConfig()
    sso_populate: SsoPopulateConfig = SsoPopulateConfig()
    # Runtime switch for developers who still need the previous staging
    # proxy/role flow.  It is intentionally not persisted in TOML: one startup
    # can choose legacy behavior without changing the default dev/blaze setup.
    legacy_proxy_mode: bool = False
    reuse_existing_proxy: bool = False
    # Private endpoints that prove sshuttle forwarding works end to end.
    proxy_health_urls: tuple[str, ...] = ()
    # A service credential operation is isolated in a worker. Its shorter
    # timeout prevents one stalled AWS call from starving every other target.
    project_command_timeout_seconds: int = 90

    @property
    def projects_by_name(self) -> dict[str, ProjectConfig]:
        return {project.name: project for project in self.projects}


def proxy_for_project(settings: Settings, project: ProjectConfig) -> ProxyConfig:
    """Return the active proxy config for one selected project.

    The dev/blaze tunnel is shared, so it keeps the configured service name.
    The legacy/staging SSM mapping is service-specific; use the project's
    configured proxy service so fprmfdt, fprdapi, etc. resolve to their own
    historical Demand Proxy group instead of a hard-coded default.
    """
    active_project = project_for_mode(settings, project)
    if settings.proxy.mode == "ssm_mapping":
        return dataclasses.replace(
            settings.proxy,
            service_name=active_project.proxy_service_name or active_project.service_name,
        )
    return dataclasses.replace(settings.proxy, group=active_project.dev_proxy_group)


def project_for_mode(settings: Settings, project: ProjectConfig) -> ProjectConfig:
    """Project view selected by the current proxy/role mode.

    TOML stores both modes beside the project, while the rest of Accessor can
    continue using the small, active ``credential_profile``/
    ``depends_on_role`` fields.
    """
    if settings.proxy.mode == "ssm_mapping":
        return dataclasses.replace(
            project,
            credential_profile=project.legacy_credential_profile,
            depends_on_role=project.legacy_depends_on_role,
            proxy_service_name=project.legacy_proxy_group,
        )
    return dataclasses.replace(
        project,
        credential_profile=project.dev_credential_profile,
        depends_on_role=project.dev_depends_on_role,
    )


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
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{field}.name must be a non-empty string")
        if name in names:
            raise ConfigError(f"duplicate project name: {name}")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"{field}.description must be a string")
        service_name = entry.get("service_name", name)
        if not isinstance(service_name, str) or not service_name:
            raise ConfigError(f"{field}.service_name must be a non-empty string")
        proxy_service_name = entry.get("proxy_service_name", service_name)
        if not isinstance(proxy_service_name, str) or not proxy_service_name:
            raise ConfigError(f"{field}.proxy_service_name must be a non-empty string")
        credential_profile = entry.get("credential_profile", "LocalStagingJumpRole@tvlk-fpr-dev")
        if not isinstance(credential_profile, str) or not credential_profile:
            raise ConfigError(f"{field}.credential_profile must be a non-empty string")
        ecs_cluster_name = entry.get("ecs_cluster_name", "")
        ec2_cluster_tag = entry.get("ec2_cluster_tag")
        discovery_tag = entry.get("discovery_tag", "service")
        discovery_value = entry.get("discovery_value", service_name)
        session_name_mode = entry.get("session_name_mode", "granted_sso")
        session_name_prefix = entry.get("session_name_prefix", "local_testing")
        if not isinstance(ecs_cluster_name, str):
            raise ConfigError(f"{field}.ecs_cluster_name must be a string")
        if ec2_cluster_tag is not None and not isinstance(ec2_cluster_tag, str):
            raise ConfigError(f"{field}.ec2_cluster_tag must be a string")
        for value, name_hint in (
            (discovery_tag, "discovery_tag"),
            (discovery_value, "discovery_value"),
            (session_name_mode, "session_name_mode"),
            (session_name_prefix, "session_name_prefix"),
        ):
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{field}.{name_hint} must be a non-empty string")
        dependency = entry.get("depends_on_role")
        if dependency is not None and (
            not isinstance(dependency, str) or dependency not in role_names
        ):
            raise ConfigError(
                f"{field}.depends_on_role must refer to a configured role name"
            )
        dev_profile = entry.get("dev_credential_profile", credential_profile)
        legacy_profile = entry.get(
            "legacy_credential_profile", "LocalStagingJumpRole@tvlk-fpr-stg"
        )
        dev_dependency = entry.get("dev_depends_on_role", dependency)
        legacy_dependency = entry.get("legacy_depends_on_role", "local-staging-jump")
        dev_proxy_group = entry.get("dev_proxy_group", "demand")
        legacy_proxy_group = entry.get("legacy_proxy_group", proxy_service_name)
        for value, name_hint in (
            (dev_profile, "dev_credential_profile"),
            (legacy_profile, "legacy_credential_profile"),
            (dev_proxy_group, "dev_proxy_group"),
            (legacy_proxy_group, "legacy_proxy_group"),
        ):
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{field}.{name_hint} must be a non-empty string")
        for value, name_hint in (
            (dev_dependency, "dev_depends_on_role"),
            (legacy_dependency, "legacy_depends_on_role"),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ConfigError(f"{field}.{name_hint} must be a non-empty string or null")
        for key, value in (
            ("dev_depends_on_role", dev_dependency),
            ("legacy_depends_on_role", legacy_dependency),
        ):
            if key in entry and value is not None and value not in role_names:
                raise ConfigError(f"{field}.{key} must refer to a configured role name")
        names.add(name)
        projects.append(
            ProjectConfig(
                name=name,
                description=description,
                service_name=service_name,
                proxy_service_name=proxy_service_name,
                credential_profile=credential_profile,
                ecs_cluster_name=ecs_cluster_name,
                ec2_cluster_tag=ec2_cluster_tag,
                discovery_tag=discovery_tag,
                discovery_value=discovery_value,
                session_name_mode=session_name_mode,
                session_name_prefix=session_name_prefix,
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
                dev_credential_profile=dev_profile,
                dev_depends_on_role=dev_dependency,
                dev_proxy_group=dev_proxy_group,
                legacy_credential_profile=legacy_profile,
                legacy_depends_on_role=legacy_dependency,
                legacy_proxy_group=legacy_proxy_group,
            )
        )
    return tuple(projects)


def _read_build_artifacts(document: dict[str, Any]) -> BuildArtifactsConfig:
    """Read optional Gradle CodeArtifact and ECR settings for build access."""
    entry = document.get("build_artifacts", {})
    if not isinstance(entry, dict):
        raise ConfigError("[build_artifacts] must be a table")
    enabled = entry.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("build_artifacts.enabled must be true or false")
    defaults = BuildArtifactsConfig()
    default_values = {
        "role_name": defaults.role_name,
        "profile": defaults.profile,
        "region": defaults.region,
        "codeartifact_domain": defaults.codeartifact_domain,
        "codeartifact_domain_owner": defaults.codeartifact_domain_owner,
        "gradle_property": defaults.gradle_property,
        "gradle_properties_path": str(defaults.gradle_properties_path),
    }
    values = {field: entry.get(field, value) for field, value in default_values.items()}
    required_fields = set(values) - {"codeartifact_domain", "codeartifact_domain_owner"}
    if enabled:
        required_fields.update({"codeartifact_domain", "codeartifact_domain_owner"})
    for field, value in values.items():
        if field not in required_fields:
            continue
        if not isinstance(value, str) or not value:
            raise ConfigError(f"build_artifacts.{field} must be a non-empty string")
    registry_ids = _string_list(
        entry.get("ecr_registry_ids"), "build_artifacts.ecr_registry_ids"
    )
    return BuildArtifactsConfig(
        enabled=enabled,
        role_name=values["role_name"],
        profile=values["profile"],
        region=values["region"],
        codeartifact_domain=values["codeartifact_domain"],
        codeartifact_domain_owner=values["codeartifact_domain_owner"],
        gradle_property=values["gradle_property"],
        gradle_properties_path=Path(values["gradle_properties_path"]).expanduser(),
        ecr_registry_ids=registry_ids,
    )


def _read_sso_populate(document: dict[str, Any]) -> SsoPopulateConfig:
    """Read optional Granted SSO profile-population settings."""
    entry = document.get("sso_populate", {})
    if not isinstance(entry, dict):
        raise ConfigError("[sso_populate] must be a table")
    enabled = entry.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("sso_populate.enabled must be true or false")
    defaults = SsoPopulateConfig()
    values = {
        "start_url": entry.get("start_url", defaults.start_url),
        "sso_region": entry.get("sso_region", defaults.sso_region),
        "profile_template": entry.get("profile_template", defaults.profile_template),
    }
    for field, value in values.items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"sso_populate.{field} must be a non-empty string")
    return SsoPopulateConfig(enabled=enabled, **values)


def _read_proxy(document: dict[str, Any], section: str, default: ProxyConfig) -> ProxyConfig:
    entry = document.get(section, {})
    if not isinstance(entry, dict):
        raise ConfigError(f"[{section}] must be a table")
    proxy = ProxyConfig(
        mode=entry.get("mode", default.mode),
        profile=entry.get("profile", default.profile),
        service_name=entry.get("service_name", default.service_name),
        parameter_mapping=entry.get("parameter_mapping", default.parameter_mapping),
        environment=entry.get("environment", default.environment),
        group=entry.get("group", default.group),
        region=entry.get("region", default.region),
        exclude_cidr=entry.get("exclude_cidr", default.exclude_cidr),
        subnets=_string_list(entry.get("subnets"), f"{section}.subnets", default.subnets),
        ssh_user=entry.get("ssh_user", default.ssh_user),
    )
    if proxy.mode not in {"blaze", "ssm_mapping"}:
        raise ConfigError(f"{section}.mode must be either 'blaze' or 'ssm_mapping'")
    for field_name in (
        "profile", "service_name", "parameter_mapping", "environment",
        "group", "region", "exclude_cidr", "ssh_user",
    ):
        if not isinstance(getattr(proxy, field_name), str) or not getattr(proxy, field_name):
            raise ConfigError(f"{section}.{field_name} must be a non-empty string")
    return proxy


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
    build_artifacts = _read_build_artifacts(document)
    sso_populate = _read_sso_populate(document)
    if build_artifacts.enabled and build_artifacts.role_name not in {role.name for role in roles}:
        raise ConfigError("build_artifacts.role_name must refer to a configured role")
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

    proxy = _read_proxy(document, "proxy", ProxyConfig())
    previous_proxy_default = ProxyConfig(
        mode="ssm_mapping",
        profile="LocalStagingJumpRole@tvlk-fpr-stg",
        service_name="fprpapi",
    )
    previous_proxy = _read_proxy(document, "previous_proxy", previous_proxy_default)
    previous_proxy_document = document.get("previous_proxy", {})
    previous_role_name = previous_proxy_document.get("role_name", "local-staging-jump")
    if not isinstance(previous_role_name, str) or not previous_role_name:
        raise ConfigError("previous_proxy.role_name must be a non-empty string")
    previous_default_proxy = previous_proxy_document.get("default_proxy", default_proxy)
    if previous_default_proxy is not None and (
        not isinstance(previous_default_proxy, str) or previous_default_proxy not in project_names
    ):
        raise ConfigError("previous_proxy.default_proxy must refer to a configured project")
    previous_reuse_existing = previous_proxy_document.get("reuse_existing_proxy", True)
    if not isinstance(previous_reuse_existing, bool):
        raise ConfigError("previous_proxy.reuse_existing_proxy must be true or false")

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
        proxy=proxy,
        previous_proxy=previous_proxy,
        previous_proxy_role_name=previous_role_name,
        previous_proxy_default_proxy=previous_default_proxy,
        previous_proxy_health_urls=_string_list(
            previous_proxy_document.get("health_urls"),
            "previous_proxy.health_urls",
        ),
        previous_proxy_reuse_existing=previous_reuse_existing,
        build_artifacts=build_artifacts,
        sso_populate=sso_populate,
        proxy_health_urls=_string_list(
            general.get("proxy_health_urls"), "general.proxy_health_urls"
        ),
        project_command_timeout_seconds=_positive_int(
            general.get("project_command_timeout_seconds"),
            "general.project_command_timeout_seconds",
            90,
        ),
    )


def use_previous_proxy(settings: Settings) -> Settings:
    """Return settings for the previous staging proxy and matching jump role.

    The normal configuration uses the shared dev/blaze proxy. Some services
    still need the older staging Demand Proxy, which depends on
    ``LocalStagingJumpRole@tvlk-fpr-stg`` and the SSM service-to-proxy mapping.
    This helper keeps that compatibility as an explicit startup choice instead
    of making every developer edit ``accessor.toml`` back and forth.
    """
    previous_role = RoleConfig(
        name=settings.previous_proxy_role_name,
        profile=settings.previous_proxy.profile,
        refresh_seconds=600,
        retry_seconds=60,
        request_on_failure=True,
    )
    roles = tuple(
        previous_role if role.name == "local-dev-jump" else role
        for role in settings.roles
        if role.name != previous_role.name
    )
    if previous_role.name not in {role.name for role in roles}:
        roles = (*roles, previous_role)
    projects = tuple(
        dataclasses.replace(
            project,
            credential_profile=previous_role.profile,
            depends_on_role=previous_role.name,
        )
        for project in settings.projects
    )
    return dataclasses.replace(
        settings,
        roles=roles,
        projects=projects,
        proxy=settings.previous_proxy,
        default_proxy=settings.previous_proxy_default_proxy,
        proxy_health_urls=settings.previous_proxy_health_urls,
        legacy_proxy_mode=True,
        reuse_existing_proxy=settings.previous_proxy_reuse_existing,
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
            selected.append(project_for_mode(settings, known[name]))
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
    """Validate Accessor's own commands without touching project checkouts."""
    errors: list[str] = []
    commands = {"aws"}
    if settings.auto_request:
        commands.add(settings.request_command[0])
    if settings.proxy_health_urls:
        commands.add("curl")
    commands.add("sshuttle")
    for command in sorted(commands):
        if not _command_exists(command):
            errors.append(f"command not found: {command}")

    # Accessor itself owns the boto3 implementation now; no project Python
    # interpreter or checkout is imported.
    if check_python_imports:
        try:
            result = subprocess.run(
                [os.environ.get("PYTHON", "python3"), "-c", "import boto3"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"Accessor: failed to check boto3: {error}")
        else:
            if result.returncode != 0:
                errors.append(f"Accessor: boto3 is unavailable: {result.stderr.strip()}")
    return errors
