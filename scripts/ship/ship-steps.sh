#!/bin/bash
# The fork's steps for the host ship queue (devtools ship-queue). The queue
# runs the base branch's copy of this file, never a branch's, so a branch
# cannot change how it ships. .tree-guard.conf names each step.
#
#   ship-steps.sh body BODYFILE   the body names an issue (Closes #N)
#   ship-steps.sh ci              playbook check --ship, then build kitty and
#                                 run its test suite
#   ship-steps.sh merge PR        merge the tested head with a merge commit
#
# The fork carries upstream kitty plus its own branches. Merges keep their
# commits (no squash), so the next merge of upstream into the fork finds
# the same ancestry and conflicts only where the fork changed code.
set -u

REPO_SLUG=mellowmarshall/kitty
# -dev packages the build needs from the system; kitty's prebuilt dependency
# bundle (./dev.sh deps) has the rest. Extracted without root into a sysroot,
# so any host with the matching runtime libraries can build.
DEV_DEBS=(
  libx11-dev libx11-xcb-dev libxcb1-dev libxcb-xkb-dev libxau-dev libxdmcp-dev x11proto-dev
  libxkbcommon-dev libxkbcommon-x11-dev libxcursor-dev libxrandr-dev libxinerama-dev libxi-dev
  libxext-dev libxfixes-dev libxrender-dev libwayland-dev wayland-protocols libdbus-1-dev
  libfontconfig-dev libfreetype-dev libexpat1-dev libgl-dev libegl-dev libglvnd-dev libglx-dev
  libbrotli-dev libpng-dev zlib1g-dev libbz2-dev
)
# The fonts kitty's font tests are written against: upstream's CI font set
# (.github/workflows/ci.py FONTS_URL), pinned by its sha256.
TEST_FONTS_URL=https://download.calibre-ebook.com/ci/fonts.tar.xz
TEST_FONTS_SHA256=885bbd33b1a93d6e3493c137b995eea356eddd5392a0e3c9c0efaf3fb7b9fb06

sysroot() { # -> path of a sysroot with DEV_DEBS, made once per host
  local root="${XDG_CACHE_HOME:-$HOME/.cache}/kitty-fork-ci/sysroot" want have
  # The package list and this function's rules both decide the sysroot
  want=$( { printf '%s\n' "${DEV_DEBS[@]}"; declare -f sysroot; } | sha256sum | cut -c1-16)
  have=$(cat "$root/.debs" 2>/dev/null)
  if [ "$want" != "$have" ]; then
    rm -rf "$root" && mkdir -p "$root/debs" || return 1
    (cd "$root/debs" && apt-get download "${DEV_DEBS[@]}" >/dev/null) || { echo "ci: apt-get download failed" >&2; return 1; }
    for d in "$root"/debs/*.deb; do dpkg-deb -x "$d" "$root" || return 1; done
    local lib="$root/usr/lib/x86_64-linux-gnu"
    # Every variable that names /usr (prefix, original_prefix, libdir...) points into the sysroot
    sed -i -E "s#^([A-Za-z_]+)=/usr(/|\$)#\1=$root/usr\2#" "$lib"/pkgconfig/*.pc "$root"/usr/share/pkgconfig/*.pc 2>/dev/null
    # A -dev package's .so link names a runtime library it does not ship: point it at the host's
    # by its soname (libfoo.so.1), which survives a host upgrade of the library
    local l target soname
    for l in "$lib"/*.so; do
      [ -e "$l" ] && continue
      target=$(basename "$(readlink "$l")")
      soname=$(printf '%s' "$target" | sed -E 's/^(.*\.so\.[0-9]+)(\..*)?$/\1/')
      if [ -e "/usr/lib/x86_64-linux-gnu/$soname" ]; then ln -sf "/usr/lib/x86_64-linux-gnu/$soname" "$l"; else rm -f "$l"; fi
    done
    echo "$want" > "$root/.debs"
  fi
  echo "$root"
}

test_fonts() { # -> path of the fontconfig file for the test run (needs the built kitty)
  local root="${XDG_CACHE_HOME:-$HOME/.cache}/kitty-fork-ci/test-fonts" kitty=kitty/launcher/kitty
  if [ "$(cat "$root/.sha256" 2>/dev/null)" != "$TEST_FONTS_SHA256" ]; then
    rm -rf "$root" && mkdir -p "$root/fonts" || return 1
    curl -fsSL --retry 3 -o "$root/fonts.tar.xz" "$TEST_FONTS_URL" || { echo "ci: test fonts download failed" >&2; return 1; }
    echo "$TEST_FONTS_SHA256  $root/fonts.tar.xz" | sha256sum -c --quiet - || { echo "ci: test fonts sha256 differs" >&2; return 1; }
    tar -xJf "$root/fonts.tar.xz" -C "$root/fonts" && rm -f "$root/fonts.tar.xz" || return 1
    printf '<?xml version="1.0"?>\n<fontconfig>\n  <dir>%s/fonts</dir>\n  <cachedir>%s/cache</cachedir>\n</fontconfig>\n' "$root" "$root" > "$root/set.conf"
    FONTCONFIG_FILE="$root/set.conf" "$kitty" +launch scripts/ship/test_fonts.py families > "$root/families" || return 1
    [ -s "$root/families" ] || { echo "ci: no family in the test fonts" >&2; return 1; }
    echo "$TEST_FONTS_SHA256" > "$root/.sha256"
  fi
  # Written each run: the host's fonts can change
  "$kitty" +launch scripts/ship/test_fonts.py conf "$root" "$root/families" > "$root/fonts.conf" || return 1
  echo "$root/fonts.conf"
}

run_ci() {
  local root lib log status fonts
  # The playbook's contracts (playbook.json) first: seconds, where the build takes minutes
  echo "=== ci: playbook check --ship $(date +%H:%M:%S)"
  playbook check --ship || { echo "ci FAILED: playbook check"; return 1; }
  root=$(sysroot) || return 1
  lib="$root/usr/lib/x86_64-linux-gnu"
  export PKG_CONFIG_PATH="$lib/pkgconfig:$root/usr/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export CPATH="$root/usr/include${CPATH:+:$CPATH}"
  export LIBRARY_PATH="$PWD/dependencies/linux-amd64/lib:$lib" LDFLAGS="-L$PWD/dependencies/linux-amd64/lib -L$lib"
  echo "=== ci: build $(date +%H:%M:%S)"
  timeout 1800 ./dev.sh build || { echo "ci FAILED: build"; return 1; }
  echo "=== ci: tests $(date +%H:%M:%S)"
  fonts=$(test_fonts) || { echo "ci FAILED: test fonts"; return 1; }
  log=$(mktemp)
  FONTCONFIG_FILE="$fonts" timeout 1800 kitty/launcher/kitty +launch test.py 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "$status" != 0 ] || grep -qE '^(FAIL|ERROR): |^WORKER ERROR' "$log"; then
    rm -f "$log"; echo "ci FAILED: tests"; return 1
  fi
  rm -f "$log"
  echo "ci passed"
}

# Exit 0 merged; 75 the base moved under the tested head (one more round);
# 76 GitHub could not be read (run once more); anything else refuses, with the
# line in $SHIP_QUEUE_REASON_FILE.
run_merge() { # PR
  local out rc=0
  out=$(gh pr merge "$1" -R "$REPO_SLUG" --merge --match-head-commit "${SHIP_QUEUE_TIP:?the queue sets the tip}" 2>&1) || rc=$?
  echo "$out"
  [ "$rc" != 0 ] || return 0
  # The reason first, so a job that ends on a retry still says why
  [ -z "${SHIP_QUEUE_REASON_FILE:-}" ] || printf '%s\n' "$(printf '%s' "$out" | head -1)" > "$SHIP_QUEUE_REASON_FILE"
  case "$out" in
    *"not mergeable"*|*"is not up to date"*|*"merge conflict"*|*"Base branch was modified"*) return 75 ;;
    *"timeout"*|*"TLS"*|*"connection"*|*"could not resolve"*|*"Something went wrong"*|*"502"*|*"503"*) return 76 ;;
  esac
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    body) grep -qE '[Cc]loses #[0-9]+' "$2" 2>/dev/null ||
        { echo "names no issue: write 'Closes #N' before submitting"; exit 1; } ;;
    ci) run_ci ;;
    merge) run_merge "$2" ;;
    *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
fi
