#!/usr/bin/env python
# The fontconfig the CI's test run uses (scripts/ship/ship-steps.sh ci). Run
# with the built kitty: kitty/launcher/kitty +launch scripts/ship/test_fonts.py
#
#   test_fonts.py families          each family fontconfig finds, one per line
#   test_fonts.py conf DIR FAMILIES  a fonts.conf: the host's fonts, plus the
#                                    fonts in DIR/fonts, with every host file
#                                    of a family in FAMILIES (a file, one per
#                                    line) left out
#
# kitty's font tests are written against upstream's CI font set. A host copy
# of one of its families (Ubuntu's variable Ubuntu Mono, for one) would be
# chosen in its place and fail the tests, so the set's own files stand alone.

import sys
from xml.sax.saxutils import escape

from kitty.fast_data_types import fc_list


def families() -> None:
    for name in sorted({str(fd['family']) for fd in fc_list()}):
        print(name)


def conf(root: str, families_file: str) -> None:
    with open(families_file) as f:
        wanted = {line.strip() for line in f if line.strip()}
    rejected = sorted({str(fd['path']) for fd in fc_list() if fd['family'] in wanted})
    print('<?xml version="1.0"?>')
    print('<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">')
    print('<fontconfig>')
    print('  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>')
    print(f'  <dir>{escape(root)}/fonts</dir>')
    print(f'  <cachedir>{escape(root)}/cache</cachedir>')
    print('  <selectfont><rejectfont>')
    for path in rejected:
        print(f'    <glob>{escape(path)}</glob>')
    print('  </rejectfont></selectfont>')
    print('</fontconfig>')


def main(argv: list[str]) -> int:
    if argv[1:2] == ['families']:
        families()
    elif argv[1:2] == ['conf'] and len(argv) == 4:
        conf(argv[2], argv[3])
    else:
        print(__doc__ or 'usage: test_fonts.py families | conf DIR FAMILIES', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
