#!/usr/bin/env python
# License: GPLv3

# Names the program that runs in a window, from the command lines of its
# foreground process group.

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence

SHELLS = frozenset({'bash', 'zsh', 'fish', 'sh', 'dash', 'ksh', 'tcsh', 'nu', 'xonsh'})
INTERPRETERS = frozenset({
    'node', 'nodejs', 'python', 'pypy', 'bun', 'deno', 'ruby', 'perl', 'php', 'lua', 'luajit', 'tsx', 'ts-node',
})  # fmt: skip
# Commands that run another command named later on their command line.
WRAPPERS = frozenset({'sudo', 'doas', 'env', 'nice', 'ionice', 'nohup', 'time', 'stdbuf', 'chrt', 'taskset'})
# Interpreter options whose next argument is a value, not the script.
VALUE_OPTIONS = frozenset({'-W', '-X', '-r', '--require', '--import', '--loader', '-I', '--experimental-loader'})
# Script names that say nothing on their own; the directory names the program.
GENERIC_SCRIPTS = frozenset({'cli', 'index', 'main', '__main__', 'run', 'bin', 'app', 'start'})


def _base(path: str) -> str:
    name = path.rsplit('/', 1)[-1].lstrip('-')
    return re.sub(r'(?<=[a-z])[-.]?[0-9][0-9.]*$', '', name)  # python3.12, zsh-5.9


def program_name(cmdline: Sequence[str]) -> str:
    """A short name for the program a command line runs, '' for a shell.

    Interpreters name their script, so an npm-installed tool shows as its name
    rather than node; wrappers such as sudo or env name what they run."""
    args = list(cmdline)
    if len(args) == 1 and ' ' in args[0]:
        args = args[0].split()  # a process that rewrote its own title
    while args and _base(args[0]) in WRAPPERS:
        args = args[1:]
        while args and (args[0].startswith('-') or '=' in args[0]):
            args = args[1:]
    if not args:
        return ''
    base = _base(args[0])
    if base in SHELLS:
        return ''
    if base not in INTERPRETERS:
        return args[0].rsplit('/', 1)[-1].lstrip('-')
    rest = args[1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in ('-c', '-e', '--eval', '-p', '--print'):
            return base  # inline code, no script to name
        if arg == '-m' and index + 1 < len(rest):
            return rest[index + 1].split('.')[0]
        if arg in VALUE_OPTIONS:
            index += 2
            continue
        if arg.startswith('-'):
            index += 1
            continue
        parts = arg.split('/')
        script = re.sub(r'\.(m?js|cjs|ts|py|rb|pl|php|lua)$', '', parts[-1])
        if script in GENERIC_SCRIPTS and len(parts) > 1:
            # .../mytool/bin/index.js names mytool; skip generic directories too
            for part in reversed(parts[:-1]):
                if part and part not in GENERIC_SCRIPTS and part not in ('dist', 'lib', 'src', 'build', 'node_modules'):
                    return part
        return script
    return base


def main_program(processes: Iterable[tuple[int, int, Sequence[str]]]) -> str:
    """The program that names a window, from (pid, parent pid, command line) of
    its foreground process group: the topmost one that is not a shell. Helpers
    that program started, such as the MCP servers of an agent, do not."""
    procs = sorted(processes)
    pids = {pid for pid, _, _ in procs}
    roots: list[tuple[int, Sequence[str]]] = []
    children: defaultdict[int, list[tuple[int, Sequence[str]]]] = defaultdict(list)
    for pid, ppid, cmdline in procs:
        (children[ppid] if ppid in pids else roots).append((pid, cmdline))
    queue = deque(roots)
    while queue:
        pid, cmdline = queue.popleft()
        if name := program_name(cmdline):
            return name
        queue.extend(children[pid])
    return ''
