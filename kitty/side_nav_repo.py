#!/usr/bin/env python
# License: GPLv3

# Finds the git repository that contains a directory, by reading the .git
# metadata directly. The side nav calls this for every tab on every redraw, so
# it must never run git or do anything slower than a few stat() calls.

import os
import time
from collections.abc import Callable
from typing import NamedTuple


class RepoInfo(NamedTuple):
    root: str  # the top of the working tree, the grouping key in the side nav
    name: str  # the name of the main repository, the same for all its worktrees
    branch: str  # the branch name, or a short commit hash when HEAD is detached
    is_worktree: bool = False


def _read_first_line(path: str) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.readline().strip()
    except OSError:
        return ''


def _git_dir_for(root: str) -> str:
    dot_git = os.path.join(root, '.git')
    if os.path.isdir(dot_git):
        return dot_git
    # A linked worktree or submodule has a .git file that points at the real git dir
    line = _read_first_line(dot_git)
    if line.startswith('gitdir:'):
        gitdir = line[len('gitdir:') :].strip()
        return os.path.normpath(os.path.join(root, gitdir))
    return ''


def _branch_from_head(gitdir: str) -> str:
    head = _read_first_line(os.path.join(gitdir, 'HEAD'))
    if head.startswith('ref:'):
        ref = head[len('ref:') :].strip()
        return ref.removeprefix('refs/heads/')
    return head[:8]


def _main_repo_name(root: str, gitdir: str) -> tuple[str, bool]:
    # A linked worktree's git dir has a commondir file pointing at the main
    # repository's .git, so its worktrees all group under the same name.
    common = _read_first_line(os.path.join(gitdir, 'commondir'))
    if common:
        common_dir = os.path.normpath(os.path.join(gitdir, common))
        if os.path.basename(common_dir) == '.git':
            return os.path.basename(os.path.dirname(common_dir)), True
        return os.path.basename(common_dir).removesuffix('.git'), True
    return os.path.basename(root), False


def find_repo(cwd: str) -> RepoInfo | None:
    if not cwd or not os.path.isabs(cwd):
        return None
    path = os.path.normpath(cwd)
    while True:
        if os.path.exists(os.path.join(path, '.git')):
            gitdir = _git_dir_for(path)
            if gitdir:
                name, is_worktree = _main_repo_name(path, gitdir)
                return RepoInfo(path, name, _branch_from_head(gitdir), is_worktree)
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


class RepoCache:
    """Remembers lookups for a short time. Agents animate their window titles
    many times a second, and every title change refreshes the side nav, which
    would otherwise walk the filesystem for every tab on every frame."""

    max_entries = 256

    def __init__(self, ttl: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self.entries: dict[str, tuple[float, RepoInfo | None]] = {}

    def __call__(self, cwd: str) -> RepoInfo | None:
        now = self.clock()
        if (entry := self.entries.get(cwd)) is not None and now - entry[0] < self.ttl:
            return entry[1]
        if len(self.entries) >= self.max_entries:
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < self.ttl}
            if len(self.entries) >= self.max_entries:
                self.entries.clear()
        ans = find_repo(cwd)
        self.entries[cwd] = (now, ans)
        return ans


def shorten_path(path: str, home: str = '') -> str:
    home = home or os.path.expanduser('~')
    if path == home:
        return '~'
    if home and path.startswith(home + os.sep):
        return '~' + path[len(home) :]
    return path
