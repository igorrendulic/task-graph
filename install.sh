#!/bin/sh
set -eu

SKILL_NAME="task-graph"
REQUIRED_SCRIPTS="kanban.py controller.py watcher.py"

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Install the ${SKILL_NAME} skill for Codex and/or Claude Code.

Options:
  --codex-only     Install only to Codex
  --claude-only    Install only to Claude Code
  --link           Symlink this repo instead of copying files
  --force          Replace an existing installed skill at the target path
  --dry-run        Print actions without changing files
  -h, --help       Show this help

Environment:
  CODEX_HOME       Defaults to \$HOME/.codex
  CLAUDE_HOME      Defaults to \$HOME/.claude
EOF
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

info() {
  printf '%s\n' "$1"
}

resolve_script_dir() {
  script=$0
  case "$script" in
    */*) ;;
    *) script=./$script ;;
  esac

  script_dir=$(CDPATH= cd "$(dirname "$script")" && pwd)
  printf '%s\n' "$script_dir"
}

is_safe_target() {
  target=$1
  case "$target" in
    */skills/"$SKILL_NAME") return 0 ;;
    *) return 1 ;;
  esac
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry-run:'
    for arg in "$@"; do
      printf ' %s' "$arg"
    done
    printf '\n'
  else
    "$@"
  fi
}

copy_payload() {
  dest=$1
  include_agents=$2
  tmp="${dest}.tmp.$$"

  run rm -rf "$tmp"
  run mkdir -p "$tmp/scripts"
  run cp "$SCRIPT_DIR/SKILL.md" "$tmp/SKILL.md"
  for script in $REQUIRED_SCRIPTS; do
    run cp "$SCRIPT_DIR/scripts/$script" "$tmp/scripts/$script"
  done

  if [ "$include_agents" -eq 1 ]; then
    run mkdir -p "$tmp/agents"
    run cp "$SCRIPT_DIR/agents/openai.yaml" "$tmp/agents/openai.yaml"
  fi

  if [ "$DRY_RUN" -eq 0 ]; then
    backup="${dest}.backup.$$"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
      rm -rf "$backup"
      mv "$dest" "$backup"
    fi
    if mv "$tmp" "$dest"; then
      rm -rf "$backup"
    else
      if [ -e "$backup" ] || [ -L "$backup" ]; then
        mv "$backup" "$dest"
      fi
      exit 1
    fi
  else
    info "dry-run: mv $tmp $dest"
  fi
}

link_payload() {
  dest=$1
  tmp="${dest}.tmp.$$"

  run rm -rf "$tmp"
  run ln -s "$SCRIPT_DIR" "$tmp"

  if [ "$DRY_RUN" -eq 0 ]; then
    backup="${dest}.backup.$$"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
      rm -rf "$backup"
      mv "$dest" "$backup"
    fi
    if mv "$tmp" "$dest"; then
      rm -rf "$backup"
    else
      if [ -e "$backup" ] || [ -L "$backup" ]; then
        mv "$backup" "$dest"
      fi
      exit 1
    fi
  else
    info "dry-run: mv $tmp $dest"
  fi
}

install_target() {
  label=$1
  dest=$2
  include_agents=$3

  is_safe_target "$dest" || die "Refusing unsafe ${label} target: $dest"

  info "Installing ${SKILL_NAME} for ${label}: $dest"

  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$FORCE" -ne 1 ]; then
      die "${label} skill already exists at $dest. Re-run with --force to replace it."
    fi
  fi

  [ -f "$SCRIPT_DIR/SKILL.md" ] || die "Missing $SCRIPT_DIR/SKILL.md"
  for script in $REQUIRED_SCRIPTS; do
    [ -f "$SCRIPT_DIR/scripts/$script" ] || die "Missing $SCRIPT_DIR/scripts/$script"
  done
  if [ "$include_agents" -eq 1 ]; then
    [ -f "$SCRIPT_DIR/agents/openai.yaml" ] || die "Missing $SCRIPT_DIR/agents/openai.yaml"
  fi

  run mkdir -p "$(dirname "$dest")"

  if [ "$LINK" -eq 1 ]; then
    link_payload "$dest"
  else
    copy_payload "$dest" "$include_agents"
  fi
}

INSTALL_CODEX=1
INSTALL_CLAUDE=1
LINK=0
FORCE=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --codex-only)
      INSTALL_CODEX=1
      INSTALL_CLAUDE=0
      ;;
    --claude-only)
      INSTALL_CODEX=0
      INSTALL_CLAUDE=1
      ;;
    --link)
      LINK=1
      ;;
    --force)
      FORCE=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

SCRIPT_DIR=$(resolve_script_dir)

CODEX_HOME=${CODEX_HOME:-"$HOME/.codex"}
CLAUDE_HOME=${CLAUDE_HOME:-"$HOME/.claude"}

if [ "$INSTALL_CODEX" -eq 1 ]; then
  install_target "Codex" "$CODEX_HOME/skills/$SKILL_NAME" 1
fi

if [ "$INSTALL_CLAUDE" -eq 1 ]; then
  install_target "Claude Code" "$CLAUDE_HOME/skills/$SKILL_NAME" 0
fi

info ""
info "Installed skill name: $SKILL_NAME"
info "Codex invocation: \$$SKILL_NAME"
