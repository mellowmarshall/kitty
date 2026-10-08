#!/usr/bin/env python
# License: GPLv3

# Connects the side nav to a TabManager: it collects each tab's repository and
# activity, decides which tabs the tab bar shows for the selected project, and
# handles the side nav's mouse, scroll and timer events.

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .child import cached_process_data
from .fast_data_types import (
    GLFW_MOUSE_BUTTON_LEFT,
    GLFW_PRESS,
    GLFW_RELEASE,
    add_timer,
    current_focused_os_window_id,
    get_boss,
    get_options,
    mark_side_nav_dirty,
    monotonic,
    remove_timer,
    set_side_nav_cols,
    set_side_nav_hidden,
    side_nav_region,
)
from .side_nav import SideNav
from .side_nav_arrange import group_order, saved_collapsed, tab_order_moving
from .side_nav_model import SideNavTabInput, build_groups, group_key, locate, most_urgent_agent_state
from .side_nav_program import main_program, program_name
from .side_nav_repo import RepoCache
from .side_nav_width import lowest_width, resized, saved_width
from .utils import path_from_osc7_url

if TYPE_CHECKING:
    from .tabs import Tab, TabManager
    from .window import Window

# Longer reported paths are not real ones, and looking them up costs time
MAX_PATH = 4096
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


def parent_pid(pid: int) -> int:
    "-1 when unknown, such as on systems without /proc"
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            stat = f.read()
        # The command name may hold spaces or parentheses, the fields after it do not
        return int(stat[stat.rindex(b')') + 2 :].split()[1])
    except Exception:
        return -1


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
        self.programs: dict[int, str] = {}
        self.proc_cwds_at = 0.0
        self.scroll_pending = 0.0
        # Windows that worked since the user last looked at them, they show as done
        self.worked: set[int] = set()
        self.activity = ActivityTracker()
        self.last_group_keys: tuple[tuple[int, str], ...] = ()
        self.known_group_keys: dict[int, str] = {}
        self.timer = 0
        self.sync_timer()
        # The width asked for, zero for the side_nav_width option, and what was
        # asked for and shown when a border drag started (None with no drag)
        self.requested = saved_width(get_options().side_nav_width)
        self.drag_from: tuple[int, int] | None = None
        self.drag_moved = False
        set_side_nav_cols(self.os_window_id, self.requested)
        # Collapsed groups, the same in every OS window, and the group whose
        # header the left button went down on with the tab that header stands
        # for (None when it did not)
        self.collapsed = saved_collapsed()
        self.header_press: tuple[str, int] | None = None

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

    def work_dir(self, w: 'Window') -> str:
        """Where the window works: the directory its program reports in the user
        variable side_nav_cwd_var, such as an agent that works in another
        worktree than the one it started in, else the cwd of the window."""
        # Only while the program that set it is in the foreground: nothing
        # clears the variable when that program exits, and the shell or the
        # next program may be anywhere. A program on another host reports
        # paths of that host.
        if (
            (var := get_options().side_nav_cwd_var)
            and not w.child_is_remote
            and (reported := w.user_vars.get(var, ''))
            and w.side_nav_cwd_pgrp >= 0
            and w.side_nav_cwd_pgrp == w.child.foreground_pgrp
        ):
            if reported.startswith('file://'):
                reported = path_from_osc7_url(reported) or ''
            if os.path.isabs(reported) and len(reported) <= MAX_PATH:
                return reported
        return self.cwd(w)

    def program(self, w: 'Window') -> str:
        # Reading the foreground program scans /proc, and updates come several
        # times a second while titles animate, so reuse it like the cwd.
        if (name := self.programs.get(w.id)) is None:
            procs = [(p['pid'], parent_pid(p['pid']), p['cmdline']) for p in w.child.foreground_processes if p['cmdline']]
            if procs and all(ppid >= 0 for _, ppid, _ in procs):
                name = main_program(procs)
            else:
                name = program_name(w.child.foreground_cmdline)
            self.programs[w.id] = name
        return name

    def group_key(self, tab: 'Tab') -> str:
        w = tab.active_window
        if w is None:
            # A closing tab has lost its windows before the next tab is chosen,
            # it still belongs to the project it was in.
            return self.known_group_keys.get(tab.id, '')
        self.known_group_keys[tab.id] = key = group_key(locate(self.work_dir(w), self.cwd(w), self.repos)[0])
        return key

    def filter_tab_bar(self, tabs: Iterable['Tab']) -> Iterable['Tab']:
        "With a project selected in the side nav, the tab bar shows only its tabs"
        at = self.tm.active_tab
        if at is None or not get_options().side_nav_filter_tab_bar or not self.is_visible:
            return tabs
        key = self.group_key(at)
        return (t for t in tabs if t is at or self.group_key(t) == key)

    def window_state(self, w: 'Window', is_looked_at: bool, now: float) -> str:
        # A state reported by the program itself, for example by a hook, wins.
        if (var := get_options().side_nav_state_var) and (explicit := w.user_vars.get(var, '')):
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
            self.programs = {}
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
                cwd = self.work_dir(w) if w else ''
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
                        self.program(w) if w else '',
                        self.cwd(w) if w else '',
                    )
                )
                keys.append((t.id, self.group_key(t)))
        self.worked &= live
        tab_ids = {t_id for t_id, _ in keys}
        self.known_group_keys = {k: v for k, v in self.known_group_keys.items() if k in tab_ids}
        self.activity.forget_all_but(live)
        self.nav.ensure_laid_out()
        self.nav.update(build_groups(entries, self.repos), self.collapsed)
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

    @property
    def cols(self) -> int:
        "The width shown now, which the OS window size may hold below the one asked for"
        return side_nav_region(self.os_window_id).width // max(1, self.nav.cell_width)

    def apply_width(self, cols: int, only_active_tab: bool = False) -> bool:
        "Zero follows the side_nav_width option. True when the width shown changed."
        before = self.cols
        self.requested = cols
        set_side_nav_cols(self.os_window_id, cols)
        if self.cols == before:
            return False
        # The central area changed size, so tabs must relayout. While a border is
        # dragged only the visible tab does, the others once the drag ends.
        if not only_active_tab:
            self.tm.resize()
        else:
            if self.tm.tab_bar_hidden:
                self.nav.layout()
            else:
                self.tm.layout_tab_bar()
            if (tab := self.tm.active_tab) is not None:
                tab.relayout()
        self.tm.mark_tab_bar_dirty()
        return True

    def drag(self, cols: int, ended: bool) -> int | None:
        "Follow a border drag. Returns the width to apply everywhere and save, once it ends."
        cols = max(lowest_width(get_options().side_nav_width), cols)
        if self.drag_from is None:
            self.drag_from, self.drag_moved = (self.requested, self.cols), False
        if not ended:
            self.drag_moved |= self.apply_width(cols, only_active_tab=True)
            return None
        (requested, shown), self.drag_from = self.drag_from, None
        if cols == shown:
            # A click on the border, or a drag that came back to where it
            # began, changes nothing: keep what was asked for, unsaved
            self.apply_width(requested, only_active_tab=True)
        if self.drag_moved:
            # The other tabs did not follow the drag
            self.tm.resize()
        return cols if cols != shown else None

    def resized_width(self, quality: str, increment: int) -> int | None:
        "The width a resize_side_nav action asks for, None when there is nothing to resize"
        if quality == 'reset':
            return 0
        if not self.is_visible:
            return None
        return resized(self.cols, quality, increment, lowest_width(get_options().side_nav_width))

    def handle_mouse(self, x: float, y: float, button: int, modifiers: int, action: int) -> None:
        if button != GLFW_MOUSE_BUTTON_LEFT:
            return
        if action == GLFW_PRESS:
            self.header_press = None
            row = self.nav.row_at(y)
            if row is not None and row.kind == 'group' and row.group is not None:
                if self.nav.on_arrow(x):
                    get_boss().set_side_nav_group_collapsed(row.group.key, row.group.key not in self.collapsed)
                else:
                    # A click selects the project, a drag moves it: decided on release
                    self.header_press = row.group.key, row.tab_id
            elif row is not None and (tab := self.tm.tab_for_id(row.tab_id)) is not None:
                self.tm.set_active_tab(tab)
        elif action == GLFW_RELEASE and self.header_press is not None:
            (key, tab_id), self.header_press = self.header_press, None
            target = self.nav.group_at_or_after(y)
            order = group_order(self.tab_keys())
            if target == key or key not in order:
                if (tab := self.tm.tab_for_id(tab_id)) is not None:
                    self.tm.set_active_tab(tab)  # selects the project
            elif target is None:
                self.move_group(key, len(order) - 1)  # dropped below the last group
            elif target in order:
                self.move_group(key, order.index(target))

    def tab_keys(self) -> list[tuple[int, str]]:
        "The tabs the side nav lists, with their groups"
        return [(t.id, self.group_key(t)) for t in self.tm.tabs_matching_tab_bar_filter]

    def move_group(self, key: str, to_index: int) -> None:
        """Move the group to a position among the groups the side nav lists. Its
        tabs move as one block; tabs the side nav does not list stay where they are."""
        tm = self.tm
        keys = self.tab_keys()
        ids = tab_order_moving(keys, key, to_index)
        if ids == [tab_id for tab_id, _ in keys]:
            return
        at = tm.active_tab
        tm.apply_tab_ordering(ids)
        # Reordering leaves the active index on whichever tab now holds it
        if at is not None:
            tm._set_active_tab(tm.tabs.index(at), store_in_history=False)
        tm.layout_tab_bar()
        tm.mark_tab_bar_dirty()
        self.update()
        mark_side_nav_dirty(self.os_window_id)

    def move_active_group(self, delta: int) -> None:
        if (at := self.tm.active_tab) is None or not self.is_visible:
            return
        key = self.group_key(at)
        order = group_order(self.tab_keys())
        if 0 <= (to := order.index(key) + delta) < len(order):
            self.move_group(key, to)

    def set_collapsed(self, collapsed: frozenset[str]) -> None:
        self.collapsed = collapsed
        if self.is_visible:  # a hidden side nav catches up on its next update
            self.update()
            mark_side_nav_dirty(self.os_window_id)

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
        # A saved width replaces only the side_nav_width it was chosen over
        self.apply_width(saved_width(get_options().side_nav_width))

    def destroy(self) -> None:
        if self.timer:
            remove_timer(self.timer)
            self.timer = 0
        self.nav.destroy()
