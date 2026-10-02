#!/usr/bin/env python
# License: GPLv3

# The side nav: a sidebar drawn by kitty itself, next to the normal tab bar,
# that lists every tab in the OS window grouped by the git repository of the
# tab's active window. It owns a cell Screen and a region carved out of the OS
# window in C (os_window_side_nav_region), exactly like the tab bar does.

import re
from typing import NamedTuple

from .fast_data_types import (
    DECAWM,
    Color,
    Region,
    Screen,
    cell_size_for_window,
    get_options,
    set_side_nav_render_data,
    side_nav_region,
    wcswidth,
)
from .side_nav_model import SideNavGroup, SideNavRow, SideNavTab, most_urgent_agent_state, rows_for_groups
from .tab_bar import as_rgb, truncate_line
from .utils import color_as_int

control_chars = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def region_key(r: Region) -> tuple[int, int, int, int]:
    return r.left, r.top, r.right, r.bottom


def names_program(title: str, program: str) -> bool:
    return re.search(rf'(?<![\w-]){re.escape(program.lower())}(?![\w-])', title.lower()) is not None


def fit(text: str, width: int) -> str:
    if width < 1:
        return ''
    return text if wcswidth(text) <= width else truncate_line(text, width)


class SideNavColors(NamedTuple):
    bg: int
    fg: int
    dim_fg: int
    header_fg: int
    active_bg: int
    active_fg: int
    accent: int
    attention: int
    activity: int
    agent: dict[str, tuple[str, int]]


def colors_from_options() -> SideNavColors:
    opts = get_options()

    def c(x: Color) -> int:
        return as_rgb(color_as_int(x))

    return SideNavColors(
        bg=c(opts.side_nav_background or opts.tab_bar_background or opts.background),
        fg=c(opts.inactive_tab_foreground),
        dim_fg=c(opts.color8),
        header_fg=c(opts.foreground),
        active_bg=c(opts.active_tab_background),
        active_fg=c(opts.active_tab_foreground),
        accent=c(opts.active_tab_background),
        attention=c(opts.color9),
        activity=c(opts.color6),
        agent={
            'blocked': ('●', c(opts.color9)),
            'waiting': ('●', c(opts.color11)),
            'working': ('◐', c(opts.color12)),
            'done': ('✓', c(opts.color10)),
        },
    )


class SideNav:
    def __init__(self, os_window_id: int):
        self.os_window_id = os_window_id
        self.groups: tuple[SideNavGroup, ...] = ()
        self.rows: tuple[SideNavRow, ...] = ()
        self.scroll_offset = 0
        self.top = 0
        # Lines that are fully inside the region, scrolling uses these so the
        # active row never lands on the clipped last line.
        self.visible_lines = 1
        # The active tab, and whether it shows a branch row
        self.active_tab_id: tuple[int, bool] = (0, False)
        self.laid_out_region: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.cell_width, self.cell_height = cell_size_for_window(os_window_id)
        self.screen = Screen(None, 1, 10, 0, self.cell_width, self.cell_height)
        self.apply_options()

    def apply_options(self) -> None:
        self.colors = colors_from_options()
        self.screen.color_profile.default_bg = get_options().side_nav_background or get_options().tab_bar_background or get_options().background
        self.screen.color_profile.default_fg = get_options().inactive_tab_foreground
        self.render()

    def layout(self) -> bool:
        "Size the screen to the region C computed. Returns False when the side nav is not shown."
        r = side_nav_region(self.os_window_id)
        self.laid_out_region = region_key(r)
        cw, ch = cell_size_for_window(self.os_window_id)
        if r.width < cw or r.height < ch or not cw or not ch:
            return False
        self.cell_width, self.cell_height = cw, ch
        cols = r.width // cw
        # Round the line count up so the grid covers the whole region, the GPU
        # clips the part of the last line that falls outside the OS window.
        lines = (r.height + ch - 1) // ch
        s = self.screen
        if s.lines != lines or s.columns != cols:
            s.resize(lines, cols)
            s.reset_mode(DECAWM)
        self.top = r.top
        visible_lines = max(1, r.height // ch)
        if visible_lines != self.visible_lines:
            # A resize or font change can push the active row out of view, or
            # leave the bottom empty after the window grows.
            self.visible_lines = visible_lines
            self.keep_active_tab_visible()
        set_side_nav_render_data(self.os_window_id, s, r.left, r.top, r.left + cols * cw, r.top + lines * ch)
        self.render()
        return True

    def ensure_laid_out(self) -> None:
        # The region appears when the first tab is added or the side nav is
        # toggled on, which need not coincide with a tab bar relayout.
        if region_key(side_nav_region(self.os_window_id)) != self.laid_out_region:
            self.layout()

    def update(self, groups: tuple[SideNavGroup, ...]) -> None:
        if groups == self.groups:
            return
        self.groups = groups
        self.rows = rows_for_groups(groups)
        active = next(((t.tab_id, bool(t.branch)) for g in groups for t in g.tabs if t.is_active), (0, False))
        # Only follow the active tab when it changes, or gains or loses its
        # branch row, so that title and state updates do not undo a scroll
        # the user made.
        if active != self.active_tab_id:
            self.active_tab_id = active
            self.keep_active_tab_visible()
        else:
            self.clamp_scroll()
        self.render()

    def keep_active_tab_visible(self) -> None:
        lines = self.visible_lines
        # The active tab's rows: its own and, under it, its branch
        active = [i for i, row in enumerate(self.rows) if row.kind in ('tab', 'branch') and row.tab is not None and row.tab.is_active]
        if active:
            first, last = active[0], active[-1]
            if first < self.scroll_offset:
                self.scroll_offset = first
            elif last >= self.scroll_offset + lines:
                # its branch too, but never past the tab itself
                self.scroll_offset = min(first, last - lines + 1)
        self.clamp_scroll()

    def clamp_scroll(self) -> None:
        self.scroll_offset = max(0, min(self.scroll_offset, len(self.rows) - self.visible_lines))

    def scroll(self, lines: int) -> None:
        before = self.scroll_offset
        self.scroll_offset += lines
        self.clamp_scroll()
        if before != self.scroll_offset:
            self.render()

    def tab_id_at(self, y: float) -> int:
        line = int(y - self.top) // max(1, self.cell_height)
        idx = self.scroll_offset + line
        if 0 <= line < self.screen.lines and 0 <= idx < len(self.rows):
            return self.rows[idx].tab_id
        return 0

    def render(self) -> None:
        s = self.screen
        s.cursor.x = s.cursor.y = 0
        # Erasing fills with the cursor colors, which still hold whatever the
        # last row of the previous render used, such as the active tab color.
        s.cursor.fg = s.cursor.bg = 0
        s.cursor.bold = False
        s.erase_in_display(2, False)
        for line in range(s.lines):
            idx = self.scroll_offset + line
            if idx < len(self.rows):
                self.draw_row(line, self.rows[idx])

    def fill_line(self, line: int, bg: int) -> None:
        s = self.screen
        s.cursor.x, s.cursor.y = 0, line
        s.cursor.bg = bg
        s.draw(' ' * s.columns)
        s.cursor.x = 0

    def draw_text(self, text: str, fg: int, bg: int, bold: bool = False) -> None:
        s = self.screen
        s.cursor.fg, s.cursor.bg, s.cursor.bold = fg, bg, bold
        # Directory names and git metadata may contain control characters,
        # which Screen.draw would act on.
        s.draw(fit(control_chars.sub('', text), s.columns - s.cursor.x))

    def draw_row(self, line: int, row: SideNavRow) -> None:
        c, s = self.colors, self.screen
        width = s.columns
        if row.kind == 'blank' or row.group is None:
            return
        g = row.group
        if row.kind == 'group':
            self.fill_line(line, c.bg)
            self.draw_text('▌' if g.is_active else ' ', c.accent, c.bg)
            self.draw_text(g.name, c.header_fg, c.bg, bold=True)
            badge, badge_fg = self.group_badge(g)
            if badge:
                self.draw_badge(line, badge, badge_fg, c.bg)
        elif row.kind == 'branch' and row.tab is not None:
            # Under its tab, in the tab's colors, so that the two read as one item
            t = row.tab
            bg = c.active_bg if t.is_active else c.bg
            self.fill_line(line, bg)
            self.draw_text('    └ ', c.dim_fg if not t.is_active else c.active_fg, bg)
            self.draw_text(t.branch, c.dim_fg if not t.is_active else c.active_fg, bg)
        elif row.tab is not None:
            t = row.tab
            bg = c.active_bg if t.is_active else c.bg
            fg = c.active_fg if t.is_active else c.fg
            self.fill_line(line, bg)
            prefix = f'  {t.index} ' if t.index < 10 else f' {t.index} '
            badge, badge_fg = self.tab_badge(t)
            badge_width = wcswidth(badge) + 1 if badge else 0
            room = width - len(prefix) - badge_width
            # The running program sits at the right, unless the title already names it
            program = control_chars.sub('', t.program) if t.program and not names_program(t.title, t.program) else ''
            program_width = min(max(0, wcswidth(program)), max(0, room // 2 - 1)) if program else 0
            if program_width < 3:
                program_width = 0  # too narrow to say anything useful
            # one column of gap between the title and the program, one before the badge
            title_room = max(1, room - (program_width + 2 if program_width else 0))
            self.draw_text(prefix, c.dim_fg if not t.is_active else fg, bg)
            # Control characters go before fitting, so that they do not count as width
            self.draw_text(fit(control_chars.sub('', t.title), title_room), fg, bg, bold=t.is_active)
            if program_width:
                s.cursor.x, s.cursor.y = width - badge_width - program_width - 1, line
                self.draw_text(fit(program, program_width), c.dim_fg, bg)
            if badge:
                self.draw_badge(line, badge, badge_fg, bg)

    def draw_badge(self, line: int, badge: str, fg: int, bg: int) -> None:
        s = self.screen
        x = s.columns - wcswidth(badge) - 1
        if x > 0:
            s.cursor.x, s.cursor.y = x, line
            self.draw_text(badge, fg, bg, bold=True)

    def tab_badge(self, t: SideNavTab) -> tuple[str, int]:
        c = self.colors
        if t.agent_state in c.agent:
            return c.agent[t.agent_state]
        if t.needs_attention:
            return '!', c.attention
        if t.has_activity and not t.is_active:
            return '•', c.activity
        return '', 0

    def group_badge(self, g: SideNavGroup) -> tuple[str, int]:
        # A collapsed glance at the group: its most urgent agent, then any bell.
        state = most_urgent_agent_state(t.agent_state for t in g.tabs)
        if state in ('blocked', 'waiting'):
            return self.colors.agent[state]
        if any(t.needs_attention for t in g.tabs):
            return '!', self.colors.attention
        return '', 0

    def destroy(self) -> None:
        self.screen.reset_callbacks()
        del self.screen
