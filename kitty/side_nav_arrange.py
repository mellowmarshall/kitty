#!/usr/bin/env python
# License: GPLv3

# Arranging the side nav's groups: the tab order that moves a group up or down,
# and which groups are collapsed. Collapsed groups apply to every OS window and
# are saved as soon as they change, so they survive a crash and a restart.

import json
import os
from collections.abc import Iterable, Sequence

from .constants import cache_dir
from .utils import log_error

MAX_SAVED = 256  # repositories remembered as collapsed


def group_order(tab_keys: Iterable[tuple[int, str]]) -> list[str]:
    "Group keys in the order of their first tab, the order the side nav shows"
    seen: dict[str, None] = {}
    for _, key in tab_keys:
        seen.setdefault(key, None)
    return list(seen)


def tab_order_moving(tab_keys: Sequence[tuple[int, str]], key: str, to_index: int) -> list[int]:
    """Tab ids in the order that puts the group `key` at position `to_index`
    among the groups. Each group's tabs end up next to each other, in the order
    they had, so a group moves as one block."""
    order = group_order(tab_keys)
    if key not in order:
        return [tab_id for tab_id, _ in tab_keys]
    order.remove(key)
    order.insert(max(0, min(to_index, len(order))), key)
    members: dict[str, list[int]] = {k: [] for k in order}
    for tab_id, k in tab_keys:
        members[k].append(tab_id)
    return [tab_id for k in order for tab_id in members[k]]


def collapsed_path() -> str:
    return os.path.join(cache_dir(), 'side-nav-collapsed.json')


def saved_collapsed() -> frozenset[str]:
    try:
        with open(collapsed_path(), 'rb') as f:
            keys = json.loads(f.read()).get('collapsed', [])
    except FileNotFoundError:
        return frozenset()
    except Exception as err:
        log_error(f'Failed to read the collapsed side nav groups with error: {err}')
        return frozenset()
    # '' is the group of tabs outside any repository
    return frozenset(k for k in keys if isinstance(k, str)) if isinstance(keys, list) else frozenset()


def save_collapsed(keys: Iterable[str]) -> None:
    from .config import atomic_save

    keys = sorted(keys)[:MAX_SAVED]
    try:
        if keys:
            atomic_save(json.dumps({'collapsed': keys}).encode('utf-8'), collapsed_path())
        else:
            os.remove(collapsed_path())
    except FileNotFoundError:
        pass
    except Exception as err:
        log_error(f'Failed to save the collapsed side nav groups with error: {err}')
