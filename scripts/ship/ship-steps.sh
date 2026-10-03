#!/bin/bash
# The fork's steps for the host ship queue (devtools ship-queue). The queue
# runs the base branch's copy of this file, never a branch's, so a branch
# cannot change how it ships. .tree-guard.conf names each step.
#
#   ship-steps.sh body BODYFILE   the body names an issue (Closes #N)
#   ship-steps.sh ci              build kitty and run its test suite
#   ship-steps.sh merge PR        merge the tested head with a merge commit
#   ship-steps.sh baseline REF    rewrite the size baseline for the files over
#                                 the limit, against upstream at REF
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
# Upstream tests that fail on Ubuntu's own fonts, not on fork code: Ubuntu
# ships Ubuntu Mono as one variable font, the test expects separate files.
# They fail on unmodified upstream on these hosts.
KNOWN_FAILURES='^FAIL: test_font_selection \(kitty_tests\.fonts\.Selection\.test_font_selection\) \((spec='"'"'ubuntu mono'"'"'|spec='"'"'family="ubuntu mono"'"'"')\)$'

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

run_ci() {
  local root lib log status
  root=$(sysroot) || return 1
  lib="$root/usr/lib/x86_64-linux-gnu"
  export PKG_CONFIG_PATH="$lib/pkgconfig:$root/usr/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export CPATH="$root/usr/include${CPATH:+:$CPATH}"
  export LIBRARY_PATH="$PWD/dependencies/linux-amd64/lib:$lib" LDFLAGS="-L$PWD/dependencies/linux-amd64/lib -L$lib"
  echo "=== ci: build $(date +%H:%M:%S)"
  timeout 1800 ./dev.sh build || { echo "ci FAILED: build"; return 1; }
  echo "=== ci: tests $(date +%H:%M:%S)"
  log=$(mktemp)
  timeout 1800 kitty/launcher/kitty +launch test.py 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "$status" != 0 ]; then
    # Pass only when the run ended normally and failed by exactly the known
    # failures: the summary counts failures and nothing else (no errors,
    # worker errors, unexpected successes or Go failures), and every FAIL line
    # is a known one.
    local failures known summary
    failures=$(grep -cE '^(FAIL|ERROR): ' "$log")
    known=$(grep -cE "$KNOWN_FAILURES" "$log")
    summary=$(grep -E '^(OK|FAILED)( |$)' "$log" | tail -1)
    if [ "$failures" = 0 ] || [ "$failures" != "$known" ] || [ "$summary" != "FAILED (failures=$known)" ] || grep -q '^WORKER ERROR' "$log"; then
      rm -f "$log"; echo "ci FAILED: tests"; return 1
    fi
    echo "ci: only the known upstream font failures"
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

# The size baseline: every source file the host ratchet covers that is over
# its limit, with its lines and why. Upstream's files are upstream's to keep;
# where the fork adds lines to one, the entry says how many. The fork's own
# source files are never listed: they stay under the limit. Run after a merge
# of upstream, or when a change adds lines to an upstream file.
BASELINE=scripts/ship/size-baseline.json
SOURCE='\.(ts|tsx|mts|cts|js|jsx|mjs|cjs|py|sh|bash|go|rs|rb|java|kt|kts|swift|c|cc|cpp|h|hpp|cs|php|scala|lua|dart|ex|exs|vue|svelte)$'
EXEMPT='(^|/)(tests?|__tests__|spec|e2e|fixtures?|__fixtures__|migrations|drills|runbooks?|vendor|third_party|node_modules|dist|build|generated)(/|$)|(^|/)test_[^/]*\.py$|_test\.'
write_baseline() { # UPSTREAM_REF
  local upstream=${1:?usage: ship-steps.sh baseline UPSTREAM_REF} f n was entries=() bad=0
  git rev-parse -q --verify "$upstream^{commit}" >/dev/null || { echo "baseline: no commit $upstream"; return 1; }
  while IFS= read -r f; do
    [[ "$f" =~ $SOURCE ]] && ! [[ "$f" =~ $EXEMPT ]] || continue
    n=$(wc -l < "$f"); [ "$n" -gt 500 ] || continue
    head -5 "$f" | grep -Eq '^[[:space:]]*(//|#|--|/\*)[[:space:]]*GENERATED from ' && continue
    if was=$(git show "$upstream:$f" 2>/dev/null | wc -l) && git cat-file -e "$upstream:$f" 2>/dev/null; then
      if [ "$n" -le "$was" ]; then entries+=("$f"$'\t'"$n"$'\t'"upstream kitty file")
      else entries+=("$f"$'\t'"$n"$'\t'"upstream kitty file; the fork adds $((n - was)) lines"); fi
    elif [[ "$f" == kitty_tests/* ]]; then entries+=("$f"$'\t'"$n"$'\t'"the fork's test suite")
    else echo "baseline: $f is the fork's own source and over 500 lines: split it"; bad=1; fi
  done < <(git ls-files)
  [ "$bad" = 0 ] || return 1
  printf '%s\n' "${entries[@]}" | python3 -c '
import json, sys
files = {}
for line in sys.stdin.read().splitlines():
    path, lines, reason = line.split("\t")
    files[path] = {"lines": int(lines), "reason": reason}
print(json.dumps({"files": files}, indent=1, sort_keys=True))' > "$BASELINE"
  echo "baseline: ${#entries[@]} files in $BASELINE"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    body) grep -qE '[Cc]loses #[0-9]+' "$2" 2>/dev/null ||
        { echo "names no issue: write 'Closes #N' before submitting"; exit 1; } ;;
    ci) run_ci ;;
    merge) run_merge "$2" ;;
    baseline) write_baseline "${2:-}" ;;
    *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
fi
