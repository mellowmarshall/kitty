#!/usr/bin/env python
# License: GPLv3

# Connects the side nav to a TabManager: it collects each tab's repository and
# activity, decides which tabs the tab bar shows for the selected project, and
# handles the side nav's mouse, scroll and timer events.

from collections.abc import Iterable
from typing import TYPE_CHECKING

from .child import cached_process_data
from .fast_data_types import (
    GLFW_MOUSE_BUTTON_LEFT,
    GLFW_PRESS,
    add_timer,
    current_focused_os_window_id,
    get_options,
    mark_side_nav_dirty,
    monotonic,
    remove_timer,
    set_side_nav_hidden,
    side_nav_region,
)
from .side_nav import SideNav, SideNavTabInput, build_groups, most_urgent_agent_state, program_name
from .side_nav_repo import RepoCache
from .utils import path_from_osc7_url

if TYPE_CHECKING:
    from .tabs import Tab, TabManager
    from .window import Window

PROC_CWD_TTL = 2.0  # seconds a cwd read from /proc is reused
# Refresh often enough that the working badge follows programs closely, an
# update with nothing changed does not redraw.
REFRESH_INTERVAL = 1.0
# Output in this many seconds marks a program as working.
WORKING_OUTPUT_WINDOW = 1.5
# Output this soon after the user typed or pasted is echo and redraw of that
# input, not the program working by itself.
ECHO_WINDOW = 1.0


# A single burst of output, such as a shell printing its prompt at startup or
# redrawing after a resize, is not work. Output must arrive again at least this
# much later before the program counts as working.
REPEAT_GAP = 0.3


def is_recent_output(output_ago: float) -> bool:
    return 0 <= output_ago <= WORKING_OUTPUT_WINDOW


def follows_stimulus(output_ago: float, stimulus_ago: float) -> bool:
    "True when the last output may be the program redrawing in response to a stimulus"
    return stimulus_ago >= 0 and stimulus_ago - output_ago < ECHO_WINDOW


class ActivityTracker:
    def __init__(self) -> None:
        # window id -> (time of the last output that counted, number of separate outputs in a row)
        self.streaks: dict[int, tuple[float, int]] = {}

    def is_working(self, window_id: int, output_ago: float, stimulus_ago: float, now: float) -> bool:
        "Arguments are seconds since the last output and the last stimulus, -1 for never"
        if not is_recent_output(output_ago):
            self.streaks.pop(window_id, None)
            return False
        count = self.streaks.get(window_id, (0.0, 0))[1]
        if follows_stimulus(output_ago, stimulus_ago):
            # The output may only be a redraw, so it neither starts work nor ends
            # it: a program that was working before a resize stays working. A
            # streak that had not yet become work is dropped, so that typing
            # cannot keep it alive until some unrelated output completes it.
            if count < 2:
                self.streaks.pop(window_id, None)
            return count >= 2
        output_at = now - output_ago
        last_at = self.streaks.get(window_id, (0.0, 0))[0]
        if not count or output_at - last_at >= REPEAT_GAP:
            count += 1
            self.streaks[window_id] = output_at, count
        return count >= 2

    def forget_all_but(self, live: set[int]) -> None:
        self.streaks = {k: v for k, v in self.streaks.items() if k in live}


class SideNavController:
    def __init__(self, tm: 'TabManager'):
        self.tm = tm
        self.os_window_id = tm.os_window_id
        self.nav = SideNav(self.os_window_id)
        self.repos = RepoCache()
        self.proc_cwds: dict[int, str] = {}
        self.proc_cwds_at = 0.0
        self.scroll_pending = 0.0
        # Windows that worked since the user last looked at them, they show as done
        self.worked: set[int] = set()
        self.activity = ActivityTracker()
        self.last_group_keys: tuple[tuple[int, str], ...] = ()
        self.known_group_keys: dict[int, str] = {}
        self.timer = 0
        self.sync_timer()

    def sync_timer(self) -> None:
        # cwds, branches and program activity change without any tab event, so poll for them
        if get_options().side_nav_width and not self.timer:
            self.timer = add_timer(self.on_timer, REFRESH_INTERVAL, True)
        elif not get_options().side_nav_width and self.timer:
            remove_timer(self.timer)
            self.timer = 0

    def on_timer(self, timer_id: int | None) -> None:
        # Sample here rather than only when rendering, so that work done while
        # the OS window is minimized or hidden still ends up shown as done.
        self.update()
        mark_side_nav_dirty(self.os_window_id)

    @property
    def is_visible(self) -> bool:
        return side_nav_region(self.os_window_id).width > 0

    def cwd(self, w: 'Window') -> str:
        # The shell's last reported cwd follows cd at once and costs nothing.
        if w.screen.last_reported_cwd and not w.child_is_remote:
            return path_from_osc7_url(w.screen.last_reported_cwd) or ''
        # Reading the cwd from /proc scans every process on the system, so
        # results are reused until the whole map expires. Expiring it as one
        # keeps it to one scan per period.
        if (cwd := self.proc_cwds.get(w.id)) is None:
            self.proc_cwds[w.id] = cwd = w.get_cwd_of_child() or ''
        return cwd

    def group_key(self, tab: 'Tab') -> str:
        w = tab.active_window
        if w is None:
            # A closing tab has lost its windows before the next tab is chosen,
            # it still belongs to the project it was in.
            return self.known_group_keys.get(tab.id, '')
        repo = self.repos(self.cwd(w))
        self.known_group_keys[tab.id] = key = repo.root if repo else ''
        return key

    def filter_tab_bar(self, tabs: Iterable['Tab']) -> Iterable['Tab']:
        "With a project selected in the side nav, the tab bar shows only its tabs"
        at = self.tm.active_tab
        if at is None or not get_options().side_nav_filter_tab_bar or not self.is_visible:
            return tabs
        key = self.group_key(at)
        return (t for t in tabs if t is at or self.group_key(t) == key)

    def window_state(self, w: 'Window', is_looked_at: bool, now: float) -> str:
        # A state reported by the program itself, for example by an agent hook, wins.
        if explicit := w.user_vars.get('agent_state', ''):
            return explicit
        output_ago, stimulus_ago = w.screen.io_times()
        # A resize makes shells and TUIs redraw, which is not work either
        resized_ago = now - w.last_resized_at if w.last_resized_at else -1
        if resized_ago >= 0 and (stimulus_ago < 0 or resized_ago < stimulus_ago):
            stimulus_ago = resized_ago
        if self.activity.is_working(w.id, output_ago, stimulus_ago, now):
            self.worked.add(w.id)
            return 'working'
        if w.id in self.worked:
            if is_looked_at:
                self.worked.discard(w.id)
                return ''
            return 'done'
        return ''

    def update(self) -> None:
        if (now := monotonic()) - self.proc_cwds_at >= PROC_CWD_TTL:
            self.proc_cwds = {}
            self.proc_cwds_at = now
        tm = self.tm
        at = tm.active_tab
        # Every window of the active tab is on screen, so all of them count as seen
        tab_in_view = at if current_focused_os_window_id() == self.os_window_id else None
        entries = []
        keys = []
        live: set[int] = set()
        with cached_process_data():
            for t in tm.tabs_matching_tab_bar_filter:
                td = t.data_for_tab_bar(t is at)
                w = t.active_window
                cwd = self.cwd(w) if w else ''
                states = []
                for x in t:
                    live.add(x.id)
                    states.append(self.window_state(x, t is tab_in_view, now))
                entries.append(
                    SideNavTabInput(
                        td.tab_id,
                        td.title,
                        td.is_active,
                        td.needs_attention,
                        td.has_activity_since_last_focus,
                        most_urgent_agent_state(states),
                        cwd,
                        program_name(w.child.foreground_cmdline) if w else '',
                    )
                )
                keys.append((t.id, self.group_key(t)))
        self.worked &= live
        tab_ids = {t_id for t_id, _ in keys}
        self.known_group_keys = {k: v for k, v in self.known_group_keys.items() if k in tab_ids}
        self.activity.forget_all_but(live)
        self.nav.ensure_laid_out()
        self.nav.update(build_groups(entries, self.repos))
        if (group_keys := tuple(keys)) != self.last_group_keys:
            # A tab moved to another project, so the filtered tab bar changes too
            self.last_group_keys = group_keys
            if get_options().side_nav_filter_tab_bar:
                tm.mark_tab_bar_dirty()

    def toggle(self) -> None:
        if set_side_nav_hidden(self.os_window_id, self.is_visible):
            # The central area changed size, so every tab must relayout
            self.tm.resize()
            self.tm.mark_tab_bar_dirty()

    def handle_mouse(self, x: float, y: float, button: int, modifiers: int, action: int) -> None:
        if button != GLFW_MOUSE_BUTTON_LEFT or action != GLFW_PRESS:
            return
        if (tab := self.tm.tab_for_id(self.nav.tab_id_at(y))) is not None:
            self.tm.set_active_tab(tab)

    def handle_scroll(self, offset: float, offset_type: int) -> None:
        # offset_type is GLFWOffsetType: 0 lines, 1 v120 (120 per wheel detent), 2 pixels
        if offset_type == 1:
            offset = offset / 120.0
        elif offset_type == 2:
            offset = offset / max(1, self.nav.cell_height)
        self.scroll_pending += offset
        if lines := int(self.scroll_pending):
            self.scroll_pending -= lines
            # A positive offset scrolls up, towards the first rows
            self.nav.scroll(-lines)
        mark_side_nav_dirty(self.os_window_id)

    def apply_options(self) -> None:
        self.nav.apply_options()
        self.sync_timer()

    def destroy(self) -> None:
        if self.timer:
            remove_timer(self.timer)
            self.timer = 0
        self.nav.destroy()
