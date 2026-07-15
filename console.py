"""Persistent terminal control panel for Accessor."""

from __future__ import annotations

import dataclasses
from collections import deque
from datetime import datetime
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable
from pathlib import Path

from config import ProjectConfig, Settings
from i18n import t
from permissions import ROLE_REFRESH_LOG, RoleRefresher, check_project_credentials, run_project_refresh
from scheduler import LOCK_CONFLICT_EXIT_CODE, RefreshScheduler
from sshuttle import network_prepare_error, prepare_network_before_proxy


LOG = logging.getLogger("accessor.console")
ACTIVITY_LOG = Path("/tmp/accessor-activity.log")
TERMINAL_APPLICATIONS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code",
}


class AccessorConsole:
    """Small text menu that keeps status visible while the scheduler runs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.selected_names = list(settings.default_projects)
        self.scheduler: RefreshScheduler | None = None
        self.scheduler_thread: threading.Thread | None = None
        self._accessor_log_level: int | None = None
        self._status_lock = threading.Lock()
        self._status_version = 0
        self._drawn_status_version = -1
        self._screen: object | None = None
        self._ui_active = False
        self._ui_mode = "status"
        self._ui_message = ""
        self._status_check_thread: threading.Thread | None = None
        self._status_check_lock = threading.Lock()
        self._app: object | None = None
        self._password_waiter: tuple[threading.Event, list[str]] | None = None
        self._enable_in_progress = False
        self.activity: deque[str] = deque(maxlen=8)
        self.role_status = {role.name: t("status.unchecked") for role in settings.roles}
        self.project_status = {project.name: t("status.unchecked") for project in settings.projects}
        self.proxy_status = t("status.unchecked")

    def _update_refresh_status(
        self, kind: str, name: str, status: str, action: str = ""
    ) -> None:
        """Receive actual results from the sole background refresh job."""
        with self._status_lock:
            if kind == "role":
                self.role_status[name] = status
            elif kind == "project":
                self.project_status[name] = status
            elif kind == "proxy":
                self.proxy_status = status
            elif kind == "job":
                self._ui_message = status
            if action:
                target = (
                    t("activity.proxy") if kind == "proxy" else t("activity.job") if kind == "job" else name
                )
                timestamp = datetime.now().strftime("%H:%M:%S")
                entry = t("activity.entry", time=timestamp, target=target, status=status, action=action)
                self.activity.append(entry)
                try:
                    with ACTIVITY_LOG.open("a", encoding="utf-8") as log_file:
                        log_file.write(f"{entry}\n")
                except OSError:
                    pass
            self._status_version += 1
        self._invalidate_ui()

    def _notify_proxy_failure(self) -> None:
        """Raise a one-time, dismissible alert in the current Accessor window."""
        with self._status_lock:
            self._ui_mode = "proxy_alert"
            self._ui_message = t("alert.proxy_failed")
            self._status_version += 1
        self._invalidate_ui()

    def _invalidate_ui(self) -> None:
        """Ask prompt_toolkit's UI loop to redraw from any worker thread."""
        app = self._app
        if app is not None:
            app.invalidate()

    def _current_status_version(self) -> int:
        with self._status_lock:
            return self._status_version

    @property
    def active(self) -> bool:
        return self.scheduler_thread is not None and self.scheduler_thread.is_alive()

    def _external_proxy_pids(self) -> tuple[int, ...]:
        """Exclude the proxy child that this console already owns."""
        process = self.scheduler.sshuttle.process if self.scheduler else None
        owned_pid = process.pid if process and self.scheduler.sshuttle.is_alive() else None
        return tuple(
            process_id
            for process_id in self.scheduler.sshuttle.find_external_proxy_pids()
            if process_id != owned_pid
        ) if self.scheduler else self._new_proxy_probe()

    @staticmethod
    def _new_proxy_probe() -> tuple[int, ...]:
        from sshuttle import SshuttleProcess

        return SshuttleProcess.find_external_proxy_pids()

    def _proxy_running(self) -> bool:
        return bool((self.scheduler and self.scheduler.sshuttle.is_alive()) or self._external_proxy_pids())

    def _probe_proxy_status(self) -> str:
        """Return proxy liveness without starting, stopping, or prompting."""
        from sshuttle import SshuttleProcess

        healthy, total = SshuttleProcess.check_health(self.settings.proxy_health_urls)
        if total and healthy == 0:
            return t("proxy.unavailable", total=total)
        health = t("proxy.health", healthy=healthy, total=total) if total else ""
        if total and healthy < total:
            health = t("proxy.partial_health", healthy=healthy, total=total)
        if self.scheduler and self.scheduler.sshuttle.is_alive():
            return t("proxy.managed", health=health)
        external_pids = self._external_proxy_pids()
        if external_pids:
            return t("proxy.external_process", pids=", ".join(map(str, external_pids)), health=health)
        if total and healthy:
            # sshuttle's privileged helper may not be visible in a normal
            # process listing, but a private endpoint response proves that the
            # route is live.
            return t("proxy.health_unrecognized", health=health)
        if self._has_pf_proxy_anchor():
            # PF anchors can survive an interrupted sshuttle process. They are
            # useful evidence for diagnosis, but do not prove an SSH tunnel is
            # usable and must never cause a false green status.
            return t("proxy.stale_pf")
        return t("proxy.stopped")

    @staticmethod
    def _proxy_is_usable(status: str) -> bool:
        """Only a managed/live process is enough to call the proxy available."""
        return status.startswith(t("proxy.running_prefix"))

    @staticmethod
    def _has_pf_proxy_anchor() -> bool:
        from sshuttle import SshuttleProcess

        return SshuttleProcess.has_pf_sshuttle_anchor()

    def _clear(self) -> None:
        print("\033[2J\033[H", end="")

    def show_status(self) -> None:
        with self._status_lock:
            role_status = dict(self.role_status)
            project_status = dict(self.project_status)
            proxy = self.proxy_status
            status_version = self._status_version
        # Rendering must be a pure cache read. Calling curl/AWS/PF here made a
        # single UI redraw perform several network calls and froze the terminal
        # whenever a tunnel was unhealthy. The explicit check action and the
        # sole refresh job update this value instead.
        lines = [t("console.title"), "", f"Demand Proxy: {proxy}"]
        if self.scheduler and self.scheduler.sshuttle.log_path:
            lines.append(t("console.logs", path=self.scheduler.sshuttle.log_path))
            lines.append(t("console.credential_log"))
        lines.extend(("", t("console.roles")))
        for role in self.settings.roles:
            lines.append(f"  {role.name}: {role_status[role.name]}")
        lines.extend(("", t("console.projects")))
        for project in self.settings.projects:
            enabled = t("status.auto_refresh") if project.name in self.selected_names and self.active else t("status.not_started")
            selected = "*" if project.name in self.selected_names else " "
            lines.append(f" {selected} {project.name}: {project_status[project.name]} ({enabled})")
        lines.append("")
        if self._ui_message:
            lines.append(self._ui_message)
        if self._ui_mode == "confirm":
            lines.append(t("console.confirm"))
        elif self._ui_mode == "projects":
            lines.append(t("console.choose_projects"))
        else:
            lines.append(t("console.menu"))
        if self._ui_active and self._screen is not None:
            self._draw_curses(lines)
        else:
            self._clear()
            print("\n".join(lines))
        self._drawn_status_version = status_version

    def _draw_curses(self, lines: list[str]) -> None:
        """Render a complete status frame; no background thread writes stdout."""
        screen = self._screen
        if screen is None:
            return
        screen.erase()
        height, width = screen.getmaxyx()
        for row, line in enumerate(lines[: max(0, height - 1)]):
            try:
                screen.addnstr(row, 0, line, max(0, width - 1))
            except Exception:  # terminal resize can race with one draw call
                break
        screen.refresh()

    def _read_line(self, prompt: str) -> str:
        """Read normal terminal input; curses is suspended by the caller first."""
        return input(prompt)

    def _selected_projects(self) -> list[ProjectConfig]:
        known = self.settings.projects_by_name
        return [known[name] for name in self.selected_names if name in known]

    def _choose_projects(self) -> None:
        print(f"\n{t('console.projects_header')}")
        for index, project in enumerate(self.settings.projects, start=1):
            mark = "*" if project.name in self.selected_names else " "
            print(f"  {index}. [{mark}] {project.name}")
        raw = self._read_line(t("console.project_input")).strip()
        if not raw:
            self.selected_names = [project.name for project in self.settings.projects]
            return
        try:
            indexes = {int(value.strip()) for value in raw.split(",")}
            selected = [self.settings.projects[index - 1].name for index in indexes]
        except (ValueError, IndexError):
            print(t("console.invalid_project_input"))
            self._read_line(t("console.press_enter"))
            return
        if selected:
            self.selected_names = selected

    def _refresh_display_status(self, project_names: set[str] | None = None) -> list[str]:
        """Perform one full read-only status pass for the display.

        This is used once before the first menu is drawn and by menu action 1.
        It deliberately does not invoke Granted, write credentials, or touch
        sshuttle: it only reports what is currently usable.
        """
        read_only = dataclasses.replace(self.settings, auto_request=False)
        refresher = RoleRefresher(read_only)
        failed: list[str] = []
        for role in self.settings.roles:
            ready = refresher.check(role)
            self._update_refresh_status(
                "role", role.name, t("status.valid") if ready else t("status.invalid"), t("action.check")
            )
            if not ready:
                failed.append(role.name)
        projects = (
            self.settings.projects
            if project_names is None
            else tuple(project for project in self.settings.projects if project.name in project_names)
        )
        for project in projects:
            ready, detail = check_project_credentials(project)
            self._update_refresh_status(
                "project", project.name,
                t("status.valid") if ready else f"{t('status.invalid')} ({detail})", t("action.check")
            )
            if not ready:
                failed.append(project.name)
        proxy_status = self._probe_proxy_status()
        self._update_refresh_status("proxy", "demand", proxy_status, t("action.check"))
        if not self._proxy_is_usable(proxy_status):
            failed.append("Demand Proxy")
        return failed

    def check(self) -> None:
        """Check only; any repair remains an explicit user decision."""
        failed = self._refresh_display_status()
        self.show_status()
        if failed:
            answer = self._read_line(t("console.check_now")).strip().lower()
            if answer in {"y", "yes"}:
                self.enable_or_refresh()
        else:
            self._read_line(t("console.all_normal"))

    def _start_status_refresh(
        self, ask_to_enable: bool = False, project_names: set[str] | None = None
    ) -> None:
        """Run all slow read-only probes off the UI/rendering thread."""
        with self._status_check_lock:
            if self._status_check_thread and self._status_check_thread.is_alive():
                return

            def refresh() -> None:
                failed = self._refresh_display_status(project_names)
                with self._status_lock:
                    if self._ui_active and ask_to_enable and failed:
                        self._ui_mode = "confirm"
                        self._ui_message = ""
                    elif self._ui_active:
                        self._ui_mode = "status"
                        self._ui_message = t("console.check_complete_normal") if not failed else t("console.check_complete")
                    self._status_version += 1

            with self._status_lock:
                self._ui_message = t("console.checking")
                self._status_version += 1
            self._update_refresh_status("job", "status", t("console.check_started"), t("action.check"))
            self._status_check_thread = threading.Thread(
                target=refresh, name="accessor-refresh", daemon=True
            )
            self._status_check_thread.start()
        self._invalidate_ui()

    def enable_or_refresh(
        self,
        choose_projects: bool = True,
        password_provider: Callable[[], str] | None = None,
        background_role_output: bool = False,
    ) -> None:
        if choose_projects:
            self._choose_projects()
        projects = self._selected_projects()
        if not projects:
            return
        # Opening/refreshing is an explicit foreground action. It is the only
        # place where an expired entitlement may prompt through `assume --wait`.
        role_refresher = RoleRefresher(
            self.settings,
            request_log_path=ROLE_REFRESH_LOG if background_role_output else None,
        )
        unavailable_required = []
        required_roles = {
            project.depends_on_role for project in projects if project.depends_on_role
        }
        for role in self.settings.roles:
            ready = role_refresher.refresh(role)
            self._update_refresh_status("role", role.name, t("status.valid") if ready else t("status.invalid"))
            if not ready and role.name in required_roles:
                unavailable_required.append(role.name)
        # Build-only roles are useful for build/writelock, but they must not
        # prevent a local-staging proxy or unrelated project credentials from
        # starting. Only dependencies selected by the configured projects block.
        if unavailable_required:
            message = t("console.required_roles_unavailable", roles=", ".join(unavailable_required))
            if self._ui_active:
                self._ui_message = message
            else:
                print(message)
                self._read_line(t("console.press_enter"))
            return
        if self.active:
            for project in projects:
                self._update_refresh_status("project", project.name, t("status.refreshing"))
                ready = run_project_refresh(self.settings, project)
                self._update_refresh_status("project", project.name, t("status.valid") if ready else t("status.refresh_failed"))
            return

        # The Demand Proxy is a shared connection. The configured service target
        # is only used for its AWS mapping, not as a project owner.
        connector = self.settings.projects_by_name.get(self.settings.default_proxy or "")
        if connector is None:
            if self._ui_active:
                self._ui_message = t("console.proxy_missing")
            else:
                print(t("console.proxy_missing"))
                self._read_line(t("console.press_enter"))
            return
        # A PF anchor alone may be stale after sshuttle has hung or crashed.
        # Only a process is considered an externally managed, reusable proxy.
        external_proxy = self._proxy_is_usable(self._probe_proxy_status())
        network_prepared = False
        if not external_proxy and connector.name not in {project.name for project in projects}:
            projects.append(connector)
        if (
            not external_proxy
            and self.settings.prepare_network_before_proxy
            and not prepare_network_before_proxy(password_provider=password_provider)
        ):
            if self._ui_active:
                detail = network_prepare_error() or t("console.network_error_unknown")
                self._ui_message = t("console.network_prepare_failed", detail=detail)
            else:
                detail = network_prepare_error() or t("console.network_error_unknown")
                self._read_line(f"{t('console.network_prepare_failed', detail=detail)}，{t('console.press_enter')}")
            return
        network_prepared = not external_proxy and self.settings.prepare_network_before_proxy
        # This is the only background job. It automatically renews both AWS
        # roles when their checks fail, then verifies them again. Granted output
        # is written to /tmp/accessor-role-refresh.log by the scheduler, so it
        # cannot render over the interactive menu.
        for project in projects:
            self._update_refresh_status("project", project.name, t("status.waiting_initial"))
        self.scheduler = RefreshScheduler(
            self.settings,
            projects,
            connector,
            status_reporter=self._update_refresh_status,
            manage_proxy=not external_proxy,
            network_prepared=network_prepared,
            # Only prompt_toolkit can receive a password safely from this
            # background scheduler thread. Its field remains in this window.
            sudo_password_provider=self._prompt_password if self._app is not None else None,
            proxy_failure_notifier=self._notify_proxy_failure,
        )
        accessor_logger = logging.getLogger("accessor")
        self._accessor_log_level = accessor_logger.level
        accessor_logger.setLevel(logging.CRITICAL)
        self.scheduler_thread = threading.Thread(
            target=self._run_scheduler_with_retry, name="accessor-refresh", daemon=True
        )
        self.scheduler_thread.start()

    def _run_scheduler_with_retry(self) -> None:
        """Keep the one refresh worker alive if a transient task crashes."""
        scheduler = self.scheduler
        if scheduler is None:
            return
        while not scheduler.stop_requested:
            result = scheduler.run()
            if scheduler.stop_requested:
                return
            if result == LOCK_CONFLICT_EXIT_CODE:
                message = scheduler.lock_conflict_message or t("console.lock_conflict")
                self._update_refresh_status("job", "refresh", t("console.job_not_started", message=message), t("action.stop"))
                if self._accessor_log_level is not None:
                    logging.getLogger("accessor").setLevel(self._accessor_log_level)
                    self._accessor_log_level = None
                return
            self._update_refresh_status(
                "job", "refresh", t("console.job_retry", code=result), t("action.restart")
            )
            time.sleep(60)

    def close(self, show_message: bool = True) -> None:
        """Stop all background refreshes and the shared proxy, preserving credentials."""
        if not self.active or self.scheduler is None:
            if self._ui_active:
                self._ui_message = t("console.no_refresh_job")
            elif show_message:
                print(t("console.no_refresh_job"))
                self._read_line(t("console.press_enter"))
            return
        self.scheduler.request_stop()
        self.scheduler_thread.join(timeout=20)
        self.scheduler = None
        self.scheduler_thread = None
        if self._accessor_log_level is not None:
            logging.getLogger("accessor").setLevel(self._accessor_log_level)
            self._accessor_log_level = None
        for name in self.selected_names:
            self._update_refresh_status("project", name, t("console.auto_refresh_closed"))

    def _curses_sudo_password(self) -> str:
        """Collect a sudo password in-place without printing it or leaving the UI."""
        screen = self._screen
        if screen is None:
            return self._read_line("Accessor sudo password: ")
        import curses

        password: list[str] = []
        screen.nodelay(False)
        try:
            while True:
                self.show_status()
                height, _width = screen.getmaxyx()
                prompt = t("console.sudo_prompt")
                screen.addstr(max(0, height - 1), 0, prompt)
                screen.refresh()
                key = screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(password)
                if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                    if password:
                        password.pop()
                elif isinstance(key, str) and key.isprintable():
                    password.append(key)
        finally:
            screen.nodelay(True)

    def _toggle_ui_project(self, key: int) -> None:
        index = key - ord("1")
        if not 0 <= index < len(self.settings.projects):
            return
        name = self.settings.projects[index].name
        if name in self.selected_names:
            self.selected_names.remove(name)
        else:
            self.selected_names.append(name)
        self._drawn_status_version = -1

    def _enable_from_dynamic_ui(self) -> None:
        """Run an enable operation without allowing log handlers to corrupt curses."""
        accessor_logger = logging.getLogger("accessor")
        was_active = self.active
        previous_level = accessor_logger.level
        accessor_logger.setLevel(logging.CRITICAL)
        try:
            self.enable_or_refresh(
                choose_projects=False,
                password_provider=self._curses_sudo_password,
                background_role_output=True,
            )
        finally:
            # Once the scheduler is active it intentionally keeps background
            # output quiet. If startup failed before it began, restore exactly
            # the level that the interactive UI inherited.
            if not was_active and self.active:
                self._accessor_log_level = previous_level
            elif not self.active:
                accessor_logger.setLevel(previous_level)

    def _handle_dynamic_key(self, key: int) -> int | None:
        if self._ui_mode == "confirm":
            if key in (ord("y"), ord("Y")):
                self._ui_mode = "projects"
            elif key in (ord("n"), ord("N"), 27):
                self._ui_mode = "status"
            self._drawn_status_version = -1
            return None
        if self._ui_mode == "proxy_alert":
            self._ui_mode = "status"
            self._ui_message = ""
            self._drawn_status_version = -1
            return None
        if self._ui_mode == "projects":
            if ord("1") <= key <= ord("9"):
                self._toggle_ui_project(key)
            elif key in (10, 13):
                if self.selected_names:
                    self._ui_mode = "status"
                    self._ui_message = t("console.enabling")
                    self._enable_from_dynamic_ui()
                else:
                    self._ui_message = t("console.select_one_project")
            elif key == 27:
                self._ui_mode = "status"
            self._drawn_status_version = -1
            return None
        if key == ord("1"):
            self._start_status_refresh(ask_to_enable=True)
        elif key == ord("2"):
            self._ui_mode = "projects"
        elif key == ord("3"):
            self.close(show_message=False)
        elif key in (ord("q"), ord("Q")):
            self.close(show_message=False)
            return 0
        self._drawn_status_version = -1
        return None

    def _run_dynamic_ui(self, screen: object) -> int:
        """One main-thread event loop: rendering and keyboard are independent."""
        screen.nodelay(True)
        self._screen = screen
        self._ui_active = True
        try:
            while True:
                if self._drawn_status_version != self._current_status_version():
                    self.show_status()
                key = screen.getch()
                if key == -1:
                    time.sleep(0.05)
                    continue
                result = self._handle_dynamic_key(key)
                if result is not None:
                    return result
        finally:
            self._ui_active = False
            self._screen = None

    def _prompt_text(self) -> str:
        """Build a view from cached state only; never run network work here."""
        with self._status_lock:
            roles = dict(self.role_status)
            projects = dict(self.project_status)
            proxy = self.proxy_status
            message = self._ui_message
            mode = self._ui_mode
            activity = tuple(self.activity)
        lines = [t("console.title"), "", f"Demand Proxy: {proxy}", "", t("console.roles")]
        lines.extend(f"  {role.name}: {roles[role.name]}" for role in self.settings.roles)
        lines.extend(("", t("console.projects")))
        for index, project in enumerate(self.settings.projects, start=1):
            mark = "*" if project.name in self.selected_names else " "
            lines.append(f" {index}. [{mark}] {project.name}: {projects[project.name]}")
        lines.extend(("", t("console.activity")))
        lines.extend(f"  {entry}" for entry in activity[-5:])
        lines.append("")
        if message:
            lines.append(message)
        if mode == "confirm":
            lines.append(t("console.confirm_input"))
        elif mode == "proxy_alert":
            lines.append(t("console.proxy_alert"))
        elif mode in {"projects", "check_projects"}:
            lines.append(t("console.project_hint"))
        elif mode == "password":
            lines.append(t("console.password_hint"))
        elif mode == "running":
            lines.append(t("console.running_hint"))
        else:
            lines.append(t("console.menu"))
        return "\n".join(lines)

    def _prompt_password(self) -> str:
        """Block the worker until the prompt_toolkit UI supplies a hidden password."""
        self._activate_terminal_window()
        ready, value = threading.Event(), []
        with self._status_lock:
            self._password_waiter = (ready, value)
            self._ui_mode = "password"
            self._ui_message = ""
            self._status_version += 1
        self._invalidate_ui()
        ready.wait(timeout=300)
        return value[0] if value else ""

    @staticmethod
    def _activate_terminal_window() -> None:
        """Bring the current terminal app forward before requesting sudo input.

        macOS may ask once for Automation permission.  This only activates the
        app window; it cannot resume a shell job that the user started with
        ``&`` and that zsh has suspended for terminal input.
        """
        if sys.platform != "darwin":
            return
        application = TERMINAL_APPLICATIONS.get(os.environ.get("TERM_PROGRAM", ""))
        if application is None:
            return
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{application}" to activate'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            LOG.debug("Unable to activate terminal for sudo input", exc_info=True)

    def _start_enable_from_prompt(self) -> None:
        """Keep Granted, curl and sudo work out of prompt_toolkit's UI loop."""
        def enable() -> None:
            accessor_logger = logging.getLogger("accessor")
            previous_level = accessor_logger.level
            was_active = self.active
            accessor_logger.setLevel(logging.CRITICAL)
            try:
                self.enable_or_refresh(
                    choose_projects=False,
                    password_provider=self._prompt_password,
                    background_role_output=True,
                )
            finally:
                with self._status_lock:
                    self._enable_in_progress = False
                if not was_active and self.active:
                    self._accessor_log_level = previous_level
                elif not self.active:
                    accessor_logger.setLevel(previous_level)
                with self._status_lock:
                    if self._ui_mode != "password":
                        self._ui_mode = "status"
                        if self._ui_message == t("console.enabling"):
                            self._ui_message = t("console.enable_complete")
                    self._status_version += 1
                self._invalidate_ui()

        with self._status_lock:
            if self._enable_in_progress:
                self._ui_message = t("console.enable_running")
                self._status_version += 1
                self._invalidate_ui()
                return
            self._enable_in_progress = True
            self._ui_mode = "running"
            self._ui_message = t("console.enabling")
            self._status_version += 1
        threading.Thread(target=enable, name="accessor-action", daemon=True).start()
        self._invalidate_ui()

    def _handle_prompt_command(self, text: str, input_area: object) -> None:
        """Handle one submitted command on the UI thread without blocking it."""
        command = text.strip().lower()
        with self._status_lock:
            mode = self._ui_mode
            waiter = self._password_waiter
        if mode == "password":
            if waiter is not None:
                waiter[1].append(text)
                waiter[0].set()
            with self._status_lock:
                self._password_waiter = None
                self._ui_mode = "running"
                self._status_version += 1
            self._invalidate_ui()
            return
        if mode == "proxy_alert":
            with self._status_lock:
                self._ui_mode = "status"
                self._ui_message = ""
                self._status_version += 1
            self._invalidate_ui()
            return
        if mode == "running":
            # Do not accept another “2” while the first enable worker is
            # still obtaining roles or starting the scheduler.  Previously
            # this could create a second scheduler thread in the same process.
            if command == "3":
                threading.Thread(target=self.close, kwargs={"show_message": False}, daemon=True).start()
            elif command in {"q", "quit", "exit"}:
                if self.scheduler is not None:
                    self.scheduler.request_stop()
                app = self._app
                if app is not None:
                    app.exit()
            else:
                with self._status_lock:
                    self._ui_message = t("console.enable_running")
                    self._status_version += 1
                self._invalidate_ui()
            return
        if mode == "confirm":
            with self._status_lock:
                self._ui_mode = "projects" if command in {"y", "yes"} else "status"
                self._ui_message = ""
                self._status_version += 1
            self._invalidate_ui()
            return
        if mode in {"projects", "check_projects"}:
            if command:
                try:
                    selected = {
                        self.settings.projects[int(item.strip()) - 1].name
                        for item in command.split(",")
                    }
                except (ValueError, IndexError):
                    with self._status_lock:
                        self._ui_message = t("console.invalid_project_number")
                        self._status_version += 1
                    self._invalidate_ui()
                    return
                if selected:
                    self.selected_names = [
                        project.name for project in self.settings.projects if project.name in selected
                    ]
            else:
                # Empty means "all" for both check and enable/refresh. This
                # makes an explicit project number an opt-in narrowing action.
                self.selected_names = [project.name for project in self.settings.projects]
            if mode == "check_projects":
                with self._status_lock:
                    self._ui_mode = "status"
                    self._status_version += 1
                self._start_status_refresh(
                    ask_to_enable=True, project_names=set(self.selected_names)
                )
            else:
                self._start_enable_from_prompt()
            return
        if command == "1":
            with self._status_lock:
                self._ui_mode = "check_projects"
                self._ui_message = ""
                self._status_version += 1
            self._invalidate_ui()
        elif command == "2":
            with self._status_lock:
                self._ui_mode = "projects"
                self._ui_message = ""
                self._status_version += 1
            self._invalidate_ui()
        elif command == "3":
            threading.Thread(target=self.close, kwargs={"show_message": False}, daemon=True).start()
        elif command in {"q", "quit", "exit"}:
            if self.scheduler is not None:
                self.scheduler.request_stop()
            app = self._app
            if app is not None:
                app.exit()

    def _run_prompt_toolkit(self) -> int:
        """Thread-safe interactive UI: workers publish state, UI invalidates."""
        from prompt_toolkit.application import Application
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import TextArea

        input_area = TextArea(
            multiline=False,
            password=Condition(lambda: self._ui_mode == "password"),
            prompt=lambda: t("console.input"),
            accept_handler=lambda buffer: self._accept_prompt(buffer, input_area),
        )
        status = Window(content=FormattedTextControl(self._prompt_text), wrap_lines=False)
        app = Application(layout=Layout(HSplit([status, input_area])), full_screen=True)
        self._app = app
        self._ui_active = True
        # Do not perform network/AWS checks merely by opening the console.
        # Use menu option 1 when an explicit status check is wanted.
        # self._start_status_refresh()
        try:
            app.run()
        finally:
            self._app = None
            self._ui_active = False
        return 0

    def _accept_prompt(self, buffer: object, input_area: object) -> bool:
        text = buffer.text
        buffer.text = ""
        self._handle_prompt_command(text, input_area)
        return True

    def run(self) -> int:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return self._run_prompt_toolkit()
        # Startup is intentionally passive; explicit option 1 performs checks.
        # self._start_status_refresh()
        while True:
            self.show_status()
            choice = self._read_line(t("console.choice")).strip().lower()
            if choice == "1":
                self.check()
            elif choice == "2":
                self.enable_or_refresh()
            elif choice == "3":
                self.close()
            elif choice in {"q", "quit", "exit"}:
                self.close()
                return 0


def run_console(settings: Settings) -> int:
    return AccessorConsole(settings).run()
