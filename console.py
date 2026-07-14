"""Persistent terminal control panel for Accessor."""

from __future__ import annotations

import dataclasses
from collections import deque
from datetime import datetime
import logging
import sys
import threading
import time
from typing import Callable
from pathlib import Path

from config import ProjectConfig, Settings
from permissions import ROLE_REFRESH_LOG, RoleRefresher, check_project_credentials, run_project_refresh
from scheduler import LOCK_CONFLICT_EXIT_CODE, RefreshScheduler
from sshuttle import network_prepare_error, prepare_network_before_proxy


LOG = logging.getLogger("accessor.console")
ACTIVITY_LOG = Path("/tmp/accessor-activity.log")


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
        self.role_status = {role.name: "未检查" for role in settings.roles}
        self.project_status = {project.name: "未检查" for project in settings.projects}
        self.proxy_status = "未检查"

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
                    "Demand Proxy" if kind == "proxy" else "状态检查" if kind == "job" else name
                )
                timestamp = datetime.now().strftime("%H:%M:%S")
                entry = f"{timestamp}: 检查了 {target}，状态：{status}，行为：{action}"
                self.activity.append(entry)
                try:
                    with ACTIVITY_LOG.open("a", encoding="utf-8") as log_file:
                        log_file.write(f"{entry}\n")
                except OSError:
                    pass
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
            return f"不可用（健康检查 0/{total}）"
        health = f"（健康 {healthy}/{total}）" if total else ""
        if total and healthy < total:
            health = f"（部分健康 {healthy}/{total}）"
        if self.scheduler and self.scheduler.sshuttle.is_alive():
            return f"运行中（Accessor 管理）{health}"
        external_pids = self._external_proxy_pids()
        if external_pids:
            return f"运行中（外部进程：{', '.join(map(str, external_pids))}）{health}"
        if total and healthy:
            # sshuttle's privileged helper may not be visible in a normal
            # process listing, but a private endpoint response proves that the
            # route is live.
            return f"运行中（健康检查通过，未识别进程）{health}"
        if self._has_pf_proxy_anchor():
            # PF anchors can survive an interrupted sshuttle process. They are
            # useful evidence for diagnosis, but do not prove an SSH tunnel is
            # usable and must never cause a false green status.
            return "疑似残留 PF 路由（proxy 未验证）"
        return "已停止"

    @staticmethod
    def _proxy_is_usable(status: str) -> bool:
        """Only a managed/live process is enough to call the proxy available."""
        return status.startswith("运行中")

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
        lines = ["Accessor 控制台", "", f"Demand Proxy：{proxy}"]
        if self.scheduler and self.scheduler.sshuttle.log_path:
            lines.append(f"  日志：{self.scheduler.sshuttle.log_path}")
            lines.append("  凭证刷新日志：/tmp/accessor-credential-refresh.log")
        lines.extend(("", "权限："))
        for role in self.settings.roles:
            lines.append(f"  {role.name}: {role_status[role.name]}")
        lines.extend(("", "项目凭证："))
        for project in self.settings.projects:
            enabled = "自动刷新" if project.name in self.selected_names and self.active else "未开启"
            selected = "*" if project.name in self.selected_names else " "
            lines.append(f" {selected} {project.name}: {project_status[project.name]} ({enabled})")
        lines.append("")
        if self._ui_message:
            lines.append(self._ui_message)
        if self._ui_mode == "confirm":
            lines.append("发现异常。是否开启 / 刷新？ [y] 是  [n] 否")
        elif self._ui_mode == "projects":
            lines.append("选择项目：按 1-7 切换；Enter 确认；Esc 取消")
        else:
            lines.append("[1] 检查  [2] 开启 / 刷新  [3] 关闭  [q] 退出")
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
        print("\n可选项目：")
        for index, project in enumerate(self.settings.projects, start=1):
            mark = "*" if project.name in self.selected_names else " "
            print(f"  {index}. [{mark}] {project.name}")
        raw = self._read_line("输入项目序号（逗号分隔；直接回车选择全部项目）：").strip()
        if not raw:
            self.selected_names = [project.name for project in self.settings.projects]
            return
        try:
            indexes = {int(value.strip()) for value in raw.split(",")}
            selected = [self.settings.projects[index - 1].name for index in indexes]
        except (ValueError, IndexError):
            print("输入无效，保持当前选择。")
            self._read_line("按 Enter 返回")
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
                "role", role.name, "有效" if ready else "失效", "检查"
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
                "project", project.name, "有效" if ready else f"失效 ({detail})", "检查"
            )
            if not ready:
                failed.append(project.name)
        proxy_status = self._probe_proxy_status()
        self._update_refresh_status("proxy", "demand", proxy_status, "检查")
        if not self._proxy_is_usable(proxy_status):
            failed.append("Demand Proxy")
        return failed

    def check(self) -> None:
        """Check only; any repair remains an explicit user decision."""
        failed = self._refresh_display_status()
        self.show_status()
        if failed:
            answer = self._read_line("发现需要处理的项目，是否现在开启/刷新？ [y/N] ").strip().lower()
            if answer in {"y", "yes"}:
                self.enable_or_refresh()
        else:
            self._read_line("全部正常。按 Enter 返回")

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
                        self._ui_message = "检查完成，全部正常。" if not failed else "检查完成。"
                    self._status_version += 1

            with self._status_lock:
                self._ui_message = "正在检查角色、项目凭证与 Proxy…"
                self._status_version += 1
            self._update_refresh_status("job", "status", "开始检查", "检查")
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
            self._update_refresh_status("role", role.name, "有效" if ready else "失效")
            if not ready and role.name in required_roles:
                unavailable_required.append(role.name)
        # Build-only roles are useful for build/writelock, but they must not
        # prevent a local-staging proxy or unrelated project credentials from
        # starting. Only dependencies selected by the configured projects block.
        if unavailable_required:
            message = f"以下必要角色尚不可用：{', '.join(unavailable_required)}"
            if self._ui_active:
                self._ui_message = message
            else:
                print(message)
                self._read_line("按 Enter 返回")
            return
        if self.active:
            for project in projects:
                self._update_refresh_status("project", project.name, "刷新中")
                ready = run_project_refresh(self.settings, project)
                self._update_refresh_status("project", project.name, "有效" if ready else "刷新失败")
            return

        # The Demand Proxy is a shared connection. The configured service target
        # is only used for its AWS mapping, not as a project owner.
        connector = self.settings.projects_by_name.get(self.settings.default_proxy or "")
        if connector is None:
            if self._ui_active:
                self._ui_message = "未配置 Demand Proxy connector。"
            else:
                print("未配置 Demand Proxy connector。")
                self._read_line("按 Enter 返回")
            return
        # A PF anchor alone may be stale after sshuttle has hung or crashed.
        # Only a process is considered an externally managed, reusable proxy.
        external_proxy = self._proxy_is_usable(self._probe_proxy_status())
        if not external_proxy and connector.name not in {project.name for project in projects}:
            projects.append(connector)
        if (
            not external_proxy
            and self.settings.prepare_network_before_proxy
            and not prepare_network_before_proxy(password_provider=password_provider)
        ):
            if self._ui_active:
                detail = network_prepare_error() or "未返回具体错误"
                self._ui_message = f"网络准备失败：{detail}"
            else:
                detail = network_prepare_error() or "未返回具体错误"
                self._read_line(f"网络准备失败：{detail}，按 Enter 返回")
            return
        # This is the only background job. It automatically renews both AWS
        # roles when their checks fail, then verifies them again. Granted output
        # is written to /tmp/accessor-role-refresh.log by the scheduler, so it
        # cannot render over the interactive menu.
        scheduler_settings = dataclasses.replace(
            self.settings,
            prepare_network_before_proxy=False,
        )
        for project in projects:
            self._update_refresh_status("project", project.name, "等待首次刷新")
        self.scheduler = RefreshScheduler(
            scheduler_settings,
            projects,
            connector,
            status_reporter=self._update_refresh_status,
            manage_proxy=not external_proxy,
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
                message = scheduler.lock_conflict_message or "另一个 Accessor 实例正在运行"
                self._update_refresh_status("job", "refresh", f"未开启刷新：{message}", "停止")
                if self._accessor_log_level is not None:
                    logging.getLogger("accessor").setLevel(self._accessor_log_level)
                    self._accessor_log_level = None
                return
            self._update_refresh_status(
                "job", "refresh", f"刷新 job 异常退出（code {result}），60 秒后重试", "重试"
            )
            time.sleep(60)

    def close(self, show_message: bool = True) -> None:
        """Stop all background refreshes and the shared proxy, preserving credentials."""
        if not self.active or self.scheduler is None:
            if self._ui_active:
                self._ui_message = "当前没有开启的刷新任务。"
            elif show_message:
                print("当前没有开启的刷新任务。")
                self._read_line("按 Enter 返回")
            return
        self.scheduler.request_stop()
        self.scheduler_thread.join(timeout=20)
        self.scheduler = None
        self.scheduler_thread = None
        if self._accessor_log_level is not None:
            logging.getLogger("accessor").setLevel(self._accessor_log_level)
            self._accessor_log_level = None
        for name in self.selected_names:
            self._update_refresh_status("project", name, "已关闭自动刷新")

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
                prompt = "输入 sudo 密码后按 Enter（输入不会显示）："
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
        if self._ui_mode == "projects":
            if ord("1") <= key <= ord("9"):
                self._toggle_ui_project(key)
            elif key in (10, 13):
                if self.selected_names:
                    self._ui_mode = "status"
                    self._ui_message = "正在开启 / 刷新…"
                    self._enable_from_dynamic_ui()
                else:
                    self._ui_message = "请至少选择一个项目。"
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
        lines = ["Accessor", "", f"Demand Proxy: {proxy}", "", "权限："]
        lines.extend(f"  {role.name}: {roles[role.name]}" for role in self.settings.roles)
        lines.extend(("", "项目凭证："))
        for index, project in enumerate(self.settings.projects, start=1):
            mark = "*" if project.name in self.selected_names else " "
            lines.append(f" {index}. [{mark}] {project.name}: {projects[project.name]}")
        lines.extend(("", "最近活动："))
        lines.extend(f"  {entry}" for entry in activity[-5:])
        lines.append("")
        if message:
            lines.append(message)
        if mode == "confirm":
            lines.append("检测到异常。输入 y 开启/刷新，输入 n 返回。")
        elif mode in {"projects", "check_projects"}:
            lines.append("输入项目编号（如 1,3,7）后回车；直接回车选择全部项目。")
        elif mode == "password":
            lines.append("请输入 sudo 密码后回车（输入不会显示）。")
        elif mode == "running":
            lines.append("操作正在后台执行，界面仍可刷新状态。")
        else:
            lines.append("输入 1 检查，2 开启/刷新，3 关闭，q 退出。")
        return "\n".join(lines)

    def _prompt_password(self) -> str:
        """Block the worker until the prompt_toolkit UI supplies a hidden password."""
        ready, value = threading.Event(), []
        with self._status_lock:
            self._password_waiter = (ready, value)
            self._ui_mode = "password"
            self._ui_message = ""
            self._status_version += 1
        self._invalidate_ui()
        ready.wait(timeout=300)
        return value[0] if value else ""

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
                        if self._ui_message == "正在开启 / 刷新…":
                            self._ui_message = "开启 / 刷新已完成，后台刷新 job 运行中。"
                    self._status_version += 1
                self._invalidate_ui()

        with self._status_lock:
            if self._enable_in_progress:
                self._ui_message = "开启 / 刷新正在执行，请等待完成。"
                self._status_version += 1
                self._invalidate_ui()
                return
            self._enable_in_progress = True
            self._ui_mode = "running"
            self._ui_message = "正在开启 / 刷新…"
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
                    self._ui_message = "开启 / 刷新正在执行，请等待完成。"
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
                        self._ui_message = "项目编号无效。"
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
            prompt=lambda: "输入> ",
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
            choice = self._read_line("选择操作：").strip().lower()
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
