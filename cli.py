#!/usr/bin/env python3
"""Command-line entry point for selecting projects and starting Accessor."""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path
import shlex
from typing import Sequence

from config import (
    ConfigError,
    ProjectConfig,
    Settings,
    load_settings,
    select_projects,
    select_proxy_project,
    validate_selection,
)
from console import run_console
from permissions import RoleRefresher, refresh_project_credentials, run_project_refresh
from scheduler import RefreshScheduler


DEFAULT_CONFIG = Path(__file__).with_name("accessor.toml")
LOG = logging.getLogger("accessor.cli")


def print_projects(settings: Settings) -> None:
    """List names a developer can pass to --project."""
    for project in settings.projects:
        default = " (default)" if project.name in settings.default_projects else ""
        description = f" — {project.description}" if project.description else ""
        print(f"{project.name}{default}{description}")
        print(f"  AWS service profile: {project.service_name}")


def print_dry_run(
    settings: Settings,
    projects: Sequence[ProjectConfig],
    proxy_project: ProjectConfig | None,
) -> None:
    """Show planned external work without touching AWS or sshuttle."""
    LOG.info("Configuration: %s", settings.config_path)
    LOG.info("Selected projects: %s", ", ".join(project.name for project in projects))
    for role in settings.roles:
        LOG.info("Role check: %s", shlex.join(role.check_command))
    for project in projects:
        LOG.info("Credential refresh: %s", project.name)
    LOG.info("sshuttle connector: %s", proxy_project.name if proxy_project else "disabled")
    if proxy_project and settings.prepare_network_before_proxy:
        LOG.info("Before sshuttle: terminal sudo cache/DNS/PF preparation")


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  accessor projects
  accessor run --project fprpapi
  accessor run -p fprpapi -p fprcinv --proxy fprpapi
  accessor run --all-projects --no-proxy
""",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("console", "projects", "check", "run", "refresh", "refresh-project"),
        default="console",
        help="console=interactive control panel; run=scripted mode; refresh=run once",
    )
    parser.add_argument(
        "-p", "--project", action="append", default=[], metavar="NAME",
        help="select a configured project; repeat to select several",
    )
    parser.add_argument("--all-projects", action="store_true", help="select every project")
    parser.add_argument("--proxy", metavar="NAME", help="selected tunnel owner")
    parser.add_argument("--no-proxy", action="store_true", help="refresh credentials only")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML file")
    parser.add_argument("--no-auto-request", action="store_true", help="do not call Granted request")
    parser.add_argument("--dry-run", action="store_true", help="print work without executing it")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    args = parse_arguments(argv)
    try:
        settings = load_settings(args.config)
        if args.no_auto_request:
            settings = dataclasses.replace(settings, auto_request=False)
        if args.action == "projects":
            print_projects(settings)
            return 0
        if args.action == "console":
            if args.project or args.all_projects or args.proxy or args.no_proxy:
                raise ConfigError("console mode does not accept project/proxy flags")
            return run_console(settings)
        projects = select_projects(settings, args.project, args.all_projects)
        if args.action == "refresh-project" and len(projects) != 1:
            raise ConfigError("refresh-project requires exactly one selected project")
        if args.action != "run" and (args.proxy or args.no_proxy):
            raise ConfigError("--proxy and --no-proxy are only valid with run")
        proxy_project = (
            select_proxy_project(settings, projects, args.proxy, args.no_proxy)
            if args.action == "run"
            else None
        )
    except ConfigError as error:
        LOG.error("%s", error)
        return 2

    errors = validate_selection(
        settings, projects, check_python_imports=args.action == "check" and not args.dry_run
    )
    if errors:
        for error in errors:
            LOG.error("%s", error)
        return 2
    if args.dry_run:
        print_dry_run(settings, projects, proxy_project)
        return 0
    if args.action == "check":
        LOG.info("Configuration and local dependencies are ready")
        print_dry_run(settings, projects, proxy_project)
        return 0
    if args.action == "refresh-project":
        try:
            LOG.info(
                "Service credentials refreshed for %s",
                refresh_project_credentials(settings, projects[0]),
            )
            return 0
        except Exception as error:  # project scripts have historical variants
            LOG.exception("Unable to refresh project %s: %s", projects[0].name, error)
            return 1
    if args.action == "refresh":
        role_ready = {role.name: RoleRefresher(settings).refresh(role) for role in settings.roles}
        results = []
        for project in projects:
            if project.depends_on_role and not role_ready[project.depends_on_role]:
                LOG.error("Project %s skipped: role %s is unavailable", project.name, project.depends_on_role)
                results.append(False)
            else:
                results.append(run_project_refresh(settings, project))
        return 0 if all(role_ready.values()) and all(results) else 1
    return RefreshScheduler(settings, projects, proxy_project).run()


if __name__ == "__main__":
    raise SystemExit(main())
