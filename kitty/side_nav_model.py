#!/usr/bin/env python
# License: GPLv3

# What the side nav shows, without drawing it: tabs grouped by git repository,
# and the rows those groups take.

import os
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, NamedTuple

from .side_nav_repo import RepoInfo, find_repo, shorten_path

# The states a window can show: programs report them in the user variable
# that side_nav_state_var names, and output without a hook shows as working
# and then done. Ordered from most to least urgent, so a tab shows its most
# urgent window.
AGENT_STATE_PRIORITY = ('blocked', 'waiting', 'working', 'done')


class SideNavTabInput(NamedTuple):
    tab_id: int
    title: str
    is_active: bool
    needs_attention: bool
    has_activity: bool
    agent_state: str
    cwd: str  # where the active window works, which groups the tab
    program: str = ''  # the program in the foreground of the active window, '' for a shell


class SideNavTab(NamedTuple):
    tab_id: int
    index: int  # the 1-based position in the tab bar while this project is selected
    title: str
    is_active: bool
    needs_attention: bool
    has_activity: bool
    agent_state: str
    program: str = ''
    branch: str = ''  # the branch the program works on, '' for a shell


class SideNavGroup(NamedTuple):
    key: str  # the main repository's git dir, or '' for tabs that are not in a repository
    name: str
    is_active: bool
    tabs: tuple[SideNavTab, ...]


class SideNavRow(NamedTuple):
    kind: Literal['group', 'tab', 'branch', 'blank']
    tab_id: int  # the tab a click on this row focuses, 0 for none
    group: SideNavGroup | None = None
    tab: SideNavTab | None = None


def most_urgent_agent_state(states: Iterable[str]) -> str:
    present = {s.lower() for s in states if s}
    for state in AGENT_STATE_PRIORITY:
        if state in present:
            return state
    return ''


def build_groups(entries: Iterable[SideNavTabInput], repo_for: Callable[[str], RepoInfo | None] = find_repo) -> tuple[SideNavGroup, ...]:
    # Groups keep the order in which their first tab appears in the tab bar,
    # so the side nav reads in the same order as the tabs.
    order: list[str] = []
    repos: dict[str, RepoInfo | None] = {}
    members: dict[str, list[SideNavTab]] = {}
    repo_cache: dict[str, RepoInfo | None] = {}
    for e in entries:
        if e.cwd not in repo_cache:
            repo_cache[e.cwd] = repo_for(e.cwd)
        repo = repo_cache[e.cwd]
        # All worktrees of a repository share one group; each program shows the
        # branch it works on, since one branch for the group is wrong for some.
        key = group_key(repo)
        if key not in members:
            order.append(key)
            members[key] = []
            repos[key] = repo
        branch = repo.branch if repo and e.program else ''
        members[key].append(
            SideNavTab(e.tab_id, len(members[key]) + 1, e.title, e.is_active, e.needs_attention, e.has_activity, e.agent_state, e.program, branch)
        )
    names = Counter(repo.name for repo in repos.values() if repo)
    groups = []
    for key in order:
        repo, tabs = repos[key], tuple(members[key])
        name = repo.name if repo else 'other'
        if repo and names[repo.name] > 1:
            # Repositories of the same name, told apart by where they are
            name = f'{name} · {shorten_path(os.path.dirname(main_checkout(repo)))}'
        groups.append(SideNavGroup(key, name, any(t.is_active for t in tabs), tabs))
    return tuple(groups)


def main_checkout(repo: RepoInfo) -> str:
    "The directory of the main repository: the parent of its .git, or a bare repository itself"
    main = repo.main or repo.root
    return os.path.dirname(main) if os.path.basename(main) == '.git' else main


def group_key(repo: RepoInfo | None) -> str:
    return (repo.main or repo.root) if repo else ''


def rows_for_groups(groups: Sequence[SideNavGroup]) -> tuple[SideNavRow, ...]:
    rows: list[SideNavRow] = []
    for i, g in enumerate(groups):
        first_tab = g.tabs[0].tab_id if g.tabs else 0
        if i:
            rows.append(SideNavRow('blank', 0))
        rows.append(SideNavRow('group', first_tab, g))
        for t in g.tabs:
            rows.append(SideNavRow('tab', t.tab_id, g, t))
            if t.branch:
                rows.append(SideNavRow('branch', t.tab_id, g, t))
    return tuple(rows)
