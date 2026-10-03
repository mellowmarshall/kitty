#!/usr/bin/env python
# License: GPLv3

# The side nav width the user chose by dragging its border or with the
# resize_side_nav action. One width applies to every OS window. It is saved as
# soon as it changes, not only when kitty quits, so that it survives a crash,
# and new OS windows start with it. It is saved with the side_nav_width value
# it replaced: editing that option makes the option apply again.

import json
import os

from .constants import cache_dir
from .utils import log_error

MIN_COLS = 8
QUALITIES = ('wider', 'narrower', 'reset')


def width_path() -> str:
    return os.path.join(cache_dir(), 'side-nav.json')


def lowest_width(option: int) -> int:
    "Never narrower than the option itself asks for"
    return min(MIN_COLS, option) if option > 0 else MIN_COLS


def saved_width(option: int) -> int:
    "The saved width in columns, zero when the side_nav_width option applies"
    try:
        with open(width_path(), 'rb') as f:
            data = json.loads(f.read())
        cols, saved_option = data.get('width', 0), data.get('option')
    except FileNotFoundError:
        return 0
    except Exception as err:
        log_error(f'Failed to read the side nav width with error: {err}')
        return 0
    if saved_option != option or not isinstance(cols, int) or cols < lowest_width(option):
        return 0
    return cols


def save_width(cols: int, option: int) -> None:
    from .config import atomic_save

    try:
        if cols:
            atomic_save(json.dumps({'width': cols, 'option': option}).encode('utf-8'), width_path())
        else:
            os.remove(width_path())
    except FileNotFoundError:
        pass
    except Exception as err:
        log_error(f'Failed to save the side nav width with error: {err}')


def resized(current: int, quality: str, increment: int, lowest: int = MIN_COLS) -> int:
    "The width after a resize_side_nav action, zero to follow side_nav_width again"
    if quality == 'reset':
        return 0
    step = abs(increment) or 1
    return max(lowest, current + (step if quality == 'wider' else -step))
