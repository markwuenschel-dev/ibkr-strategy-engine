#!/usr/bin/env bash
#
# collab-kit installer.
#
#   ./install.sh                 install with defaults
#   ./install.sh --dry-run       show exactly what would happen, change nothing
#   ./install.sh --uninstall     undo what this installer did (never touches your data)
#
# What it does:
#   1. links tools/handoff, tools/collab-handoff, bin/newproject, bin/restart into $PREFIX
#   2. writes a marker-delimited COLLAB_HOME block into your shell rc
#   3. copies skills/collab/ to $SKILLS_DIR/collab/ (the Claude Code `/collab` front door)
#   4. bootstraps $COLLAB_HOME (outbox/, inbox/live/, logs/, collabs.json)
#
# Portability: bash 3.2+, no GNU coreutils assumptions (no `readlink -f`, no `realpath`,
# no `sed -i`). Runs on Linux, macOS, and Git Bash on Windows.

set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

RC_BEGIN='# >>> collab-kit >>>'
RC_END='# <<< collab-kit <<<'
SHIM_MARKER='collab-kit shim ->'
BAK_SUFFIX='.collab-kit.bak'

# "<path relative to KIT_DIR>" and the command name it is installed as.
LINK_SRCS=('tools/handoff' 'tools/collab-handoff' 'bin/newproject' 'bin/restart')
LINK_NAMES=('handoff' 'collab-handoff' 'newproject' 'restart')

# ---------------------------------------------------------------------------
# self-location: resolve this script's real directory, following symlinks,
# from any cwd, without readlink -f / realpath (BSD readlink has no -f).
# ---------------------------------------------------------------------------

resolve_dir() {
    local src=$1
    local dir target hops=0

    while [[ -L "$src" ]]; do
        hops=$((hops + 1))
        if [[ "$hops" -gt 64 ]]; then
            printf 'install.sh: symlink loop resolving %s\n' "$1" >&2
            return 1
        fi
        dir=$( cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd -P ) || dir=$(dirname "$src")
        target=$(readlink "$src")
        case "$target" in
            /* | [A-Za-z]:[\\/]*) src=$target ;;
            *) src="$dir/$target" ;;
        esac
    done

    dir=$( cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd -P ) || dir=$(dirname "$src")
    printf '%s\n' "$dir"
}

KIT_DIR=$(resolve_dir "${BASH_SOURCE[0]}")

# ---------------------------------------------------------------------------
# options (defaults)
# ---------------------------------------------------------------------------

PREFIX="${HOME}/bin"
COLLAB_HOME=''          # empty => default to KIT_DIR
RC_FILE=''              # empty => auto-detect from $SHELL
SKILLS_DIR="${HOME}/.claude/skills"
DO_RC=1
DO_SKILL=1
FORCE=0
DRY_RUN=0
QUIET=0
UNINSTALL=0

# runtime state
SYMLINKS_OK=1
NEED_PATH=0
FAILURES=()
RESULTS=()
RC_BACKED_UP=0

C_RESET=''
C_BOLD=''
C_RED=''
C_GREEN=''
C_YELLOW=''
C_BLUE=''

# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

setup_colors() {
    if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
        C_RESET=$'\033[0m'
        C_BOLD=$'\033[1m'
        C_RED=$'\033[31m'
        C_GREEN=$'\033[32m'
        C_YELLOW=$'\033[33m'
        C_BLUE=$'\033[36m'
    fi
}

say() {
    [[ "$QUIET" -eq 1 ]] && return 0
    printf '%s\n' "$*"
}

step() {
    [[ "$QUIET" -eq 1 ]] && return 0
    printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$*"
}

ok() {
    [[ "$QUIET" -eq 1 ]] && return 0
    printf '    %sok%s   %s\n' "$C_GREEN" "$C_RESET" "$*"
}

warn() {
    printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

# fail: record a non-fatal problem; the run continues and exits non-zero at the end.
fail() {
    FAILURES+=("$*")
    printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
}

die() {
    printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
    exit 1
}

# dry: always printed, even under --quiet -- the printed plan IS the output of --dry-run.
dry() {
    printf '%sDRY-RUN%s %s\n' "$C_BLUE" "$C_RESET" "$*"
}

usage_error() {
    printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
    printf 'Run "install.sh --help" for usage.\n' >&2
    exit 2
}

# ---------------------------------------------------------------------------
# the single mutation gate: nothing changes disk except through run/run_as
# ---------------------------------------------------------------------------

# fmt_cmd: render a command line for display, quoting anything that needs it.
fmt_cmd() {
    local out='' a esc
    local sq="'"
    for a in "$@"; do
        if [[ -z "$a" ]] || [[ "$a" =~ [^A-Za-z0-9_@%+=:,./-] ]]; then
            esc=${a//$sq/$sq\\$sq$sq}
            out="$out $sq$esc$sq"
        else
            out="$out $a"
        fi
    done
    printf '%s' "${out# }"
}

# run <cmd> [args...] -- execute, or echo under --dry-run.
run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        dry "$(fmt_cmd "$@")"
        return 0
    fi
    "$@"
}

# run_as <description> <cmd> [args...] -- same gate, but with a human-readable
# description for actions whose literal command line is unhelpful (file writes).
run_as() {
    local desc=$1
    shift
    if [[ "$DRY_RUN" -eq 1 ]]; then
        dry "$desc"
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

# abspath: best-effort absolute, symlink-free path; works for non-existent paths.
abspath() {
    local p=$1
    local d
    local b
    local root

    if [[ -d "$p" ]]; then
        ( cd -P "$p" >/dev/null 2>&1 && pwd -P ) || printf '%s\n' "$p"
        return 0
    fi

    d=$(dirname "$p")
    b=$(basename "$p")
    if [[ -d "$d" ]]; then
        root=$( cd -P "$d" >/dev/null 2>&1 && pwd -P ) || root=$d
        printf '%s/%s\n' "${root%/}" "$b"
    else
        printf '%s\n' "$p"
    fi
}

# path_contains <dir> -- is <dir> (as written, or resolved) an entry of $PATH?
path_contains() {
    local needle=$1
    local needle_abs
    local parts
    local p
    local resolved

    needle_abs=$(abspath "$needle")
    IFS=':' read -r -a parts <<<"${PATH:-}"

    for p in ${parts[@]+"${parts[@]}"}; do
        [[ -n "$p" ]] || continue
        if [[ "$p" == "$needle" ]] || [[ "$p" == "$needle_abs" ]]; then
            return 0
        fi
        if [[ -d "$p" ]]; then
            resolved=$(abspath "$p")
            if [[ "$resolved" == "$needle_abs" ]]; then
                return 0
            fi
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
collab-kit installer

Usage: install.sh [options]

Options:
  --prefix <dir>        Where to link the executables      (default: $HOME/bin)
  --collab-home <dir>   Value written for COLLAB_HOME      (default: the kit directory)
  --rc <file>           Shell rc file to edit              (default: auto-detect from $SHELL)
  --no-rc               Do not touch any shell rc file
  --skills-dir <dir>    Where the /collab skill goes       (default: $HOME/.claude/skills)
  --no-skill            Do not install the /collab skill
  --force               Replace links / skill that exist but point elsewhere
  --dry-run             Print every action prefixed DRY-RUN and change nothing
  --uninstall           Remove the links, the managed rc block, and the installed skill
                        ($COLLAB_HOME and its data are never touched)
  -q, --quiet           Only warnings and errors
  -h, --help            This message

Installs:
  handoff, collab-handoff, newproject, restart  ->  $PREFIX
  skills/collab/                                ->  $SKILLS_DIR/collab/
  COLLAB_HOME + PATH block                      ->  your shell rc (marker-delimited)
  outbox/, inbox/live/, logs/, collabs.json     ->  $COLLAB_HOME

Examples:
  ./install.sh
  ./install.sh --dry-run
  ./install.sh --prefix ~/.local/bin --collab-home ~/collabs
  ./install.sh --uninstall
EOF
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

need_value() {
    # need_value <flag> <count-of-remaining-args>
    [[ "$2" -ge 2 ]] || usage_error "$1 requires a value"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prefix)       need_value "$1" "$#"; PREFIX=$2; shift 2 ;;
            --prefix=*)     PREFIX=${1#*=}; shift ;;
            --collab-home)  need_value "$1" "$#"; COLLAB_HOME=$2; shift 2 ;;
            --collab-home=*) COLLAB_HOME=${1#*=}; shift ;;
            --rc)           need_value "$1" "$#"; RC_FILE=$2; shift 2 ;;
            --rc=*)         RC_FILE=${1#*=}; shift ;;
            --no-rc)        DO_RC=0; shift ;;
            --skills-dir)   need_value "$1" "$#"; SKILLS_DIR=$2; shift 2 ;;
            --skills-dir=*) SKILLS_DIR=${1#*=}; shift ;;
            --no-skill)     DO_SKILL=0; shift ;;
            --force)        FORCE=1; shift ;;
            --dry-run)      DRY_RUN=1; shift ;;
            --uninstall)    UNINSTALL=1; shift ;;
            -q|--quiet)     QUIET=1; shift ;;
            -h|--help)      usage; exit 0 ;;
            --)             shift; break ;;
            -*)             usage_error "unknown option: $1" ;;
            *)              usage_error "unexpected argument: $1" ;;
        esac
    done

    if [[ $# -gt 0 ]]; then
        usage_error "unexpected argument: $1"
    fi

    [[ -n "$PREFIX" ]] || usage_error "--prefix requires a non-empty value"
    [[ -n "$SKILLS_DIR" ]] || usage_error "--skills-dir requires a non-empty value"
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

preflight() {
    step 'Preflight'
    local missing
    local pyver=''
    missing=()

    if command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)'; then
            pyver=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) || pyver='?'
            ok "python3 $pyver"
        else
            pyver=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) || pyver='unknown'
            missing+=("python3 is $pyver, but collab-kit needs >= 3.9 -- install a newer Python 3 and make sure it is first on PATH")
        fi
    else
        missing+=('python3 not found on PATH -- install Python 3.9+ (macOS: brew install python3 | Debian/Ubuntu: sudo apt install python3 | Windows: winget install Python.Python.3)')
    fi

    if command -v git >/dev/null 2>&1; then
        ok "git $(git --version 2>/dev/null | head -n 1)"
    else
        missing+=('git not found on PATH -- install git (macOS: xcode-select --install | Debian/Ubuntu: sudo apt install git | Windows: winget install Git.Git)')
    fi

    if [[ "${#missing[@]}" -gt 0 ]]; then
        local m
        for m in ${missing[@]+"${missing[@]}"}; do
            printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$m" >&2
        done
        die 'preflight failed; nothing was changed.'
    fi

    if path_contains "$PREFIX"; then
        NEED_PATH=0
        ok "$PREFIX is on PATH"
    else
        NEED_PATH=1
        warn "$PREFIX is not on your PATH. Add this line to your shell rc:"
        if rc_is_fish; then
            printf '    set -gx PATH "%s" $PATH\n' "$PREFIX" >&2
        else
            printf '    export PATH="%s:$PATH"\n' "$PREFIX" >&2
        fi
        if [[ "$DO_RC" -eq 1 ]]; then
            warn "(this installer will add it to $RC_FILE for you)"
        fi
    fi
}

# verify_kit: every file we intend to link must be present. Abort before any
# mutation rather than half-installing.
verify_kit() {
    step 'Verifying kit integrity'
    local missing
    local i
    local src
    missing=()

    i=0
    while [[ "$i" -lt "${#LINK_SRCS[@]}" ]]; do
        src="$KIT_DIR/${LINK_SRCS[$i]}"
        if [[ ! -f "$src" ]]; then
            missing+=("${LINK_SRCS[$i]}")
        fi
        i=$((i + 1))
    done

    if [[ "${#missing[@]}" -gt 0 ]]; then
        local m
        printf '%serror:%s kit at %s is incomplete; these required files are missing:\n' \
            "$C_RED" "$C_RESET" "$KIT_DIR" >&2
        for m in ${missing[@]+"${missing[@]}"}; do
            printf '    %s/%s\n' "$KIT_DIR" "$m" >&2
        done
        die 'refusing to half-install. Re-clone the kit or run install.sh from inside it.'
    fi

    if [[ ! -d "$KIT_DIR/tools/collabkit" ]]; then
        warn "$KIT_DIR/tools/collabkit is missing -- the handoff CLIs import it and will fail at runtime."
    fi

    ok "all ${#LINK_SRCS[@]} link sources present under $KIT_DIR"
}

# ---------------------------------------------------------------------------
# symlink support probe (Git Bash may silently copy instead of linking)
# ---------------------------------------------------------------------------

probe_symlinks() {
    local dir=$1
    local target="$dir/.collab-kit-probe-target.$$"
    local link="$dir/.collab-kit-probe-link.$$"
    local result=1

    : >"$target" 2>/dev/null || return 1
    if ln -s "$target" "$link" 2>/dev/null; then
        if [[ -L "$link" ]]; then
            result=0
        fi
    fi
    rm -f "$link" "$target" 2>/dev/null || true
    return "$result"
}

detect_symlink_support() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        dry "probe symlink support in $PREFIX (assuming symlinks work)"
        SYMLINKS_OK=1
        return 0
    fi
    if probe_symlinks "$PREFIX"; then
        SYMLINKS_OK=1
    else
        SYMLINKS_OK=0
        warn "symlinks are not usable in $PREFIX (common on Git Bash / Windows without developer mode)."
        warn 'falling back to tiny exec-wrapper shim scripts; they behave the same and stay pointed at this kit.'
    fi
}

# ---------------------------------------------------------------------------
# linking
# ---------------------------------------------------------------------------

write_shim() {
    local dest=$1 target=$2
    local tmp="$dest.collab-kit.tmp.$$"
    {
        printf '#!/usr/bin/env bash\n'
        printf '# %s %s\n' "$SHIM_MARKER" "$target"
        # shellcheck disable=SC2016  # "$@" must reach the file literally
        printf 'exec "%s" "$@"\n' "$target"
    } >"$tmp"
    chmod +x "$tmp"
    mv -f "$tmp" "$dest"
}

# shim_points_to <file> <target> -- is <file> a shim of ours aimed at <target>?
shim_points_to() {
    local file=$1 target=$2
    local line
    local recorded
    [[ -f "$file" ]] || return 1
    while IFS= read -r line; do
        case "$line" in
            "# $SHIM_MARKER "*)
                recorded=${line#"# $SHIM_MARKER "}
                if [[ "$recorded" == "$target" ]] || same_file "$recorded" "$target"; then
                    return 0
                fi
                ;;
            *) : ;;
        esac
    done < <(head -n 5 "$file" 2>/dev/null || true)
    return 1
}

# same_file <a> <b> -- do these two paths name the same underlying file?
# Compared by inode identity, not by string: path spellings legitimately differ
# (Git Bash reports /tmp/... for a link created as /c/Users/.../Temp/..., and
# /a/b/../c vs /a/c are the same file). Symlinks are followed, which is what we
# want -- "does this link land on our file?".
same_file() {
    local a=$1 b=$2
    [[ -e "$a" ]] && [[ -e "$b" ]] && [[ "$a" -ef "$b" ]]
}

# link_target <symlink> -- absolute path a symlink resolves to (one hop, relative-aware)
link_target() {
    local link=$1
    local dir target
    dir=$(dirname "$link")
    target=$(readlink "$link") || return 1
    case "$target" in
        /* | [A-Za-z]:[\\/]*) : ;;
        *) target="$dir/$target" ;;
    esac
    abspath "$target"
}

link_one() {
    local rel=$1 name=$2
    local src="$KIT_DIR/$rel"
    local dest="$PREFIX/$name"
    local src_abs current

    src_abs=$(abspath "$src")
    run chmod +x "$src"

    if [[ -L "$dest" ]]; then
        current=$(link_target "$dest") || current='<unreadable>'
        if [[ "$current" == "$src_abs" ]] || same_file "$dest" "$src"; then
            ok "$name -> already linked"
            RESULTS+=("$name  ok (already linked)")
            return 0
        fi
        if [[ "$FORCE" -eq 1 ]]; then
            run rm -f "$dest"
        else
            fail "$name: $dest is a symlink to $current (not this kit) -- re-run with --force to replace it"
            RESULTS+=("$name  SKIPPED (occupied by $current)")
            return 0
        fi
    elif [[ -e "$dest" ]]; then
        if shim_points_to "$dest" "$src_abs"; then
            ok "$name -> already linked (shim)"
            RESULTS+=("$name  ok (already linked, shim)")
            return 0
        fi
        if [[ "$FORCE" -eq 1 ]]; then
            run rm -f "$dest"
        else
            fail "$name: $dest already exists and is not from this kit -- re-run with --force to replace it"
            RESULTS+=("$name  SKIPPED (occupied by an existing file)")
            return 0
        fi
    fi

    if [[ "$SYMLINKS_OK" -eq 1 ]]; then
        run ln -s "$src_abs" "$dest"
        ok "$name -> $src_abs"
        RESULTS+=("$name  linked -> $src_abs")
    else
        run_as "write shim $dest -> $src_abs" write_shim "$dest" "$src_abs"
        run chmod +x "$dest"
        ok "$name -> $src_abs (shim script, symlinks unavailable)"
        RESULTS+=("$name  shim -> $src_abs")
    fi
}

install_links() {
    step "Linking executables into $PREFIX"
    run mkdir -p "$PREFIX"
    detect_symlink_support

    local i=0
    while [[ "$i" -lt "${#LINK_SRCS[@]}" ]]; do
        link_one "${LINK_SRCS[$i]}" "${LINK_NAMES[$i]}"
        i=$((i + 1))
    done
}

# ---------------------------------------------------------------------------
# shell rc block
# ---------------------------------------------------------------------------

detect_rc() {
    local shell_name
    shell_name=$(basename "${SHELL:-}" 2>/dev/null) || shell_name=''
    shell_name=${shell_name%.exe}

    case "$shell_name" in
        zsh)
            printf '%s\n' "$HOME/.zshrc"
            ;;
        fish)
            printf '%s\n' "$HOME/.config/fish/config.fish"
            ;;
        bash|sh|'')
            if [[ -f "$HOME/.bashrc" ]]; then
                printf '%s\n' "$HOME/.bashrc"
            elif [[ "$(uname -s 2>/dev/null)" == 'Darwin' ]]; then
                printf '%s\n' "$HOME/.bash_profile"
            else
                printf '%s\n' "$HOME/.bashrc"
            fi
            ;;
        *)
            printf '%s\n' "$HOME/.profile"
            ;;
    esac
}

rc_is_fish() {
    [[ "$RC_FILE" == *.fish ]]
}

rc_block_text() {
    local body
    if rc_is_fish; then
        body="set -gx COLLAB_HOME \"$COLLAB_HOME\""
        if [[ "$NEED_PATH" -eq 1 ]]; then
            body="$body"$'\n'"set -gx PATH \"$PREFIX\" \$PATH"
        fi
    else
        body="export COLLAB_HOME=\"$COLLAB_HOME\""
        if [[ "$NEED_PATH" -eq 1 ]]; then
            body="$body"$'\n'"export PATH=\"$PREFIX:\$PATH\""
        fi
    fi
    printf '%s\n%s\n%s\n' "$RC_BEGIN" "$body" "$RC_END"
}

# rc_strip_block: stdin -> stdout with every managed block removed.
rc_strip_block() {
    local line
    local inside=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$RC_BEGIN" ]]; then
            inside=1
        elif [[ "$line" == "$RC_END" ]]; then
            inside=0
        elif [[ "$inside" -eq 0 ]]; then
            printf '%s\n' "$line"
        fi
    done
}

# rc_extract_block <rc>: print the managed block (markers included), if any.
rc_extract_block() {
    local rc=$1
    local line
    local inside=0
    [[ -f "$rc" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$RC_BEGIN" ]]; then
            inside=1
            printf '%s\n' "$line"
        elif [[ "$line" == "$RC_END" ]]; then
            if [[ "$inside" -eq 1 ]]; then
                printf '%s\n' "$line"
            fi
            inside=0
        elif [[ "$inside" -eq 1 ]]; then
            printf '%s\n' "$line"
        fi
    done <"$rc"
}

# rc_replace <rc> <block>: rewrite <rc> with the managed block replaced (never appended twice).
rc_replace() {
    local rc=$1 block=$2
    local body='' tmp
    tmp="$rc.collab-kit.tmp.$$"
    if [[ -f "$rc" ]]; then
        body=$(rc_strip_block <"$rc" || true)
    fi
    {
        if [[ -n "$body" ]]; then
            printf '%s\n\n' "$body"
        fi
        printf '%s\n' "$block"
    } >"$tmp"
    mv -f "$tmp" "$rc"
}

# rc_remove <rc>: rewrite <rc> with the managed block removed entirely.
rc_remove() {
    local rc=$1
    local body='' tmp
    tmp="$rc.collab-kit.tmp.$$"
    body=$(rc_strip_block <"$rc" || true)
    if [[ -n "$body" ]]; then
        printf '%s\n' "$body" >"$tmp"
    else
        : >"$tmp"
    fi
    mv -f "$tmp" "$rc"
}

rc_backup() {
    local rc=$1
    [[ -f "$rc" ]] || return 0
    [[ "$RC_BACKED_UP" -eq 1 ]] && return 0
    if [[ -f "$rc$BAK_SUFFIX" ]]; then
        RC_BACKED_UP=1
        return 0
    fi
    run cp "$rc" "$rc$BAK_SUFFIX"
    RC_BACKED_UP=1
    if [[ "$DRY_RUN" -eq 0 ]]; then
        say "    backup: $rc$BAK_SUFFIX"
    fi
}

install_rc() {
    step "Updating shell rc: $RC_FILE"

    # Keep the PATH line stable across re-runs: if a previous run already put a
    # PATH line in the managed block, keep it even though PATH now looks fine.
    if [[ "$NEED_PATH" -eq 0 ]]; then
        case "$(rc_extract_block "$RC_FILE")" in
            *PATH*) NEED_PATH=1 ;;
            *) : ;;
        esac
    fi

    local block existing
    block=$(rc_block_text)
    existing=$(rc_extract_block "$RC_FILE")

    if [[ -n "$existing" ]] && [[ "$existing" == "$block" ]]; then
        ok 'rc block already up to date'
        RESULTS+=("rc  ok (already up to date): $RC_FILE")
        return 0
    fi

    run mkdir -p "$(dirname "$RC_FILE")"
    rc_backup "$RC_FILE"

    if [[ -n "$existing" ]]; then
        run_as "replace the collab-kit block in $RC_FILE" rc_replace "$RC_FILE" "$block"
        ok "replaced the existing collab-kit block in $RC_FILE"
        RESULTS+=("rc  block replaced: $RC_FILE")
    else
        run_as "append the collab-kit block to $RC_FILE" rc_replace "$RC_FILE" "$block"
        ok "wrote the collab-kit block to $RC_FILE"
        RESULTS+=("rc  block written: $RC_FILE")
    fi

    if [[ "$DRY_RUN" -eq 1 ]] || [[ "$QUIET" -eq 0 ]]; then
        local line
        while IFS= read -r line; do
            say "      $line"
        done <<<"$block"
    fi
}

# ---------------------------------------------------------------------------
# /collab skill
# ---------------------------------------------------------------------------

dirs_identical() {
    local a=$1 b=$2
    command -v diff >/dev/null 2>&1 || return 1
    diff -r "$a" "$b" >/dev/null 2>&1
}

install_skill() {
    local src="$KIT_DIR/skills/collab"
    local dest="$SKILLS_DIR/collab"

    step 'Installing the /collab skill'

    if [[ ! -f "$src/SKILL.md" ]]; then
        say "    skipped: $src/SKILL.md not present in this kit"
        RESULTS+=('skill  skipped (not present in this kit)')
        return 0
    fi

    if [[ -e "$dest" ]]; then
        if dirs_identical "$src" "$dest"; then
            ok "already installed: $dest"
            RESULTS+=("skill  ok (already installed): $dest")
            return 0
        fi
        if [[ "$FORCE" -eq 1 ]]; then
            run rm -rf "$dest"
        else
            fail "skill: $dest already exists and differs from this kit's copy -- re-run with --force to overwrite it"
            RESULTS+=("skill  SKIPPED (existing $dest differs)")
            return 0
        fi
    fi

    run mkdir -p "$dest"
    # copy, not symlink: Claude Code reads the directory directly and Git Bash
    # symlinks are unreliable.
    run cp -R "$src/." "$dest/"
    ok "copied $src -> $dest"
    RESULTS+=("skill  installed: $dest")
}

# ---------------------------------------------------------------------------
# $COLLAB_HOME bootstrap
# ---------------------------------------------------------------------------

write_default_registry() {
    local dest=$1
    local tmp="$dest.collab-kit.tmp.$$"
    printf '{"version": 1, "collabs": {}}\n' >"$tmp"
    mv -f "$tmp" "$dest"
}

bootstrap_home() {
    step "Bootstrapping COLLAB_HOME: $COLLAB_HOME"

    run mkdir -p "$COLLAB_HOME"
    run mkdir -p "$COLLAB_HOME/outbox"
    run mkdir -p "$COLLAB_HOME/inbox/live"
    run mkdir -p "$COLLAB_HOME/logs"
    ok 'outbox/, inbox/live/, logs/ ready'

    local registry="$COLLAB_HOME/collabs.json"

    if [[ -f "$registry" ]]; then
        ok 'collabs.json already exists (left untouched)'
        RESULTS+=('registry  ok (existing collabs.json left untouched)')
        return 0
    fi

    # Always seed EMPTY, never from collabs.json.example. The example documents
    # the file format and contains a fictional "demo" entry pointing at
    # /absolute/path/to/... -- copying it makes the very first `handoff status`
    # report a MISSING collab the user never created.
    run_as "write $registry with {\"version\": 1, \"collabs\": {}}" write_default_registry "$registry"
    ok 'wrote an empty collabs.json'
    RESULTS+=('registry  created empty collabs.json')
}

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------

uninstall_links() {
    step "Removing links from $PREFIX"
    local i=0
    local name
    local dest
    local src
    local src_abs
    local current

    while [[ "$i" -lt "${#LINK_SRCS[@]}" ]]; do
        name="${LINK_NAMES[$i]}"
        dest="$PREFIX/$name"
        src="$KIT_DIR/${LINK_SRCS[$i]}"
        src_abs=$(abspath "$src")
        i=$((i + 1))

        if [[ -L "$dest" ]]; then
            current=$(link_target "$dest") || current='<unreadable>'
            if [[ "$current" == "$src_abs" ]] || same_file "$dest" "$src" || [[ "$FORCE" -eq 1 ]]; then
                run rm -f "$dest"
                ok "removed $dest"
                RESULTS+=("$name  removed")
            else
                warn "$dest points at $current (not this kit) -- left alone; use --force to remove it anyway"
                RESULTS+=("$name  left alone (points elsewhere)")
            fi
        elif [[ -e "$dest" ]]; then
            if shim_points_to "$dest" "$src_abs" || [[ "$FORCE" -eq 1 ]]; then
                run rm -f "$dest"
                ok "removed $dest"
                RESULTS+=("$name  removed")
            else
                warn "$dest is not a collab-kit shim -- left alone; use --force to remove it anyway"
                RESULTS+=("$name  left alone (not ours)")
            fi
        else
            say "    not present: $dest"
            RESULTS+=("$name  not present")
        fi
    done
}

uninstall_rc() {
    step "Removing the managed block from $RC_FILE"

    if [[ ! -f "$RC_FILE" ]]; then
        say "    not present: $RC_FILE"
        RESULTS+=("rc  not present: $RC_FILE")
        return 0
    fi

    if [[ -z "$(rc_extract_block "$RC_FILE")" ]]; then
        ok 'no collab-kit block found'
        RESULTS+=('rc  no block found')
        return 0
    fi

    rc_backup "$RC_FILE"
    run_as "remove the collab-kit block from $RC_FILE" rc_remove "$RC_FILE"
    ok "removed the collab-kit block from $RC_FILE"
    RESULTS+=("rc  block removed: $RC_FILE")
}

uninstall_skill() {
    local src="$KIT_DIR/skills/collab"
    local dest="$SKILLS_DIR/collab"

    step 'Removing the /collab skill'

    if [[ ! -e "$dest" ]]; then
        say "    not present: $dest"
        RESULTS+=('skill  not present')
        return 0
    fi

    if [[ "$FORCE" -eq 1 ]] || { [[ -d "$src" ]] && dirs_identical "$src" "$dest"; }; then
        run rm -rf "$dest"
        ok "removed $dest"
        RESULTS+=("skill  removed: $dest")
    else
        warn "$dest differs from this kit's copy (or the kit copy is gone) -- left alone; re-run with --uninstall --force to remove it"
        RESULTS+=("skill  left alone (differs from this kit)")
    fi
}

do_uninstall() {
    step 'collab-kit uninstall'
    say "    kit:         $KIT_DIR"
    say "    prefix:      $PREFIX"
    say "    collab-home: $COLLAB_HOME (data -- never touched)"
    say ''

    uninstall_links

    if [[ "$DO_RC" -eq 1 ]]; then
        uninstall_rc
    else
        step 'Shell rc'
        say '    skipped (--no-rc)'
    fi

    if [[ "$DO_SKILL" -eq 1 ]]; then
        uninstall_skill
    else
        step '/collab skill'
        say '    skipped (--no-skill)'
    fi

    say ''
    step 'Uninstalled'
    print_results
    say ''
    say "Your data in $COLLAB_HOME was not touched (collabs.json, collab dirs, outbox/, inbox/, logs/)."
    say 'Open a new shell (or re-source your rc) to drop COLLAB_HOME from the environment.'
}

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

print_results() {
    local r
    for r in ${RESULTS[@]+"${RESULTS[@]}"}; do
        say "    $r"
    done
}

print_summary() {
    say ''
    step 'Summary'
    print_results
    say ''
    say "    KIT_DIR      $KIT_DIR"
    say "    COLLAB_HOME  $COLLAB_HOME"
    say "    PREFIX       $PREFIX"
    if [[ "$DO_RC" -eq 1 ]]; then
        say "    RC           $RC_FILE"
    else
        say '    RC           (skipped: --no-rc)'
    fi
    if [[ "$DO_SKILL" -eq 1 ]]; then
        say "    SKILLS_DIR   $SKILLS_DIR"
    else
        say '    SKILLS_DIR   (skipped: --no-skill)'
    fi

    say ''
    step 'Next steps'
    if [[ "$DO_RC" -eq 1 ]]; then
        say "    1. source $RC_FILE          # or open a new shell"
        say '    2. newproject <name> --repo <git-url> --reviewer claude|grok'
    else
        if rc_is_fish; then
            say "    1. set -gx COLLAB_HOME \"$COLLAB_HOME\"; set -gx PATH \"$PREFIX\" \$PATH"
        else
            say "    1. export COLLAB_HOME=\"$COLLAB_HOME\"; export PATH=\"$PREFIX:\$PATH\""
        fi
        say '    2. newproject <name> --repo <git-url> --reviewer claude|grok'
    fi

    say ''
    say 'Verify it yourself:'
    say '    handoff status'

    if [[ "$DRY_RUN" -eq 1 ]]; then
        say ''
        say 'This was a --dry-run: nothing on disk was changed.'
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    setup_colors
    parse_args "$@"

    [[ -n "$COLLAB_HOME" ]] || COLLAB_HOME="$KIT_DIR"
    [[ -n "$RC_FILE" ]] || RC_FILE=$(detect_rc)

    PREFIX=$(abspath "$PREFIX")
    COLLAB_HOME=$(abspath "$COLLAB_HOME")
    SKILLS_DIR=$(abspath "$SKILLS_DIR")

    if [[ "$UNINSTALL" -eq 1 ]]; then
        do_uninstall
    else
        step 'collab-kit install'
        say "    kit:         $KIT_DIR"
        say "    collab-home: $COLLAB_HOME"
        say "    prefix:      $PREFIX"
        say ''

        preflight
        verify_kit
        install_links

        if [[ "$DO_RC" -eq 1 ]]; then
            install_rc
        else
            step 'Shell rc'
            say '    skipped (--no-rc)'
            RESULTS+=('rc  skipped (--no-rc)')
        fi

        if [[ "$DO_SKILL" -eq 1 ]]; then
            install_skill
        else
            step '/collab skill'
            say '    skipped (--no-skill)'
            RESULTS+=('skill  skipped (--no-skill)')
        fi

        bootstrap_home
        print_summary
    fi

    if [[ "${#FAILURES[@]}" -gt 0 ]]; then
        local f
        printf '\n%s%d problem(s) need your attention:%s\n' "$C_RED" "${#FAILURES[@]}" "$C_RESET" >&2
        for f in ${FAILURES[@]+"${FAILURES[@]}"}; do
            printf '    - %s\n' "$f" >&2
        done
        exit 1
    fi

    exit 0
}

main "$@"
