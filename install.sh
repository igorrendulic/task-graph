#!/usr/bin/env bash

set -euo pipefail

readonly DEFAULT_REF="main"
readonly REPOSITORY="igorrendulic/task-graph"

usage() {
  cat >&2 <<'USAGE'
Usage: ./install.sh [--ref <tag-or-commit>] [--force]

Install the Task Graph skill into ${CODEX_HOME:-$HOME/.codex}/skills/task-graph.
Use --force to replace an existing installation.
USAGE
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

path_exists() {
  [ -e "$1" ] || [ -L "$1" ]
}

ref="$DEFAULT_REF"
force=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref)
      [ "$#" -ge 2 ] || { usage; fail "--ref requires a tag, branch, or commit"; }
      [ -n "$2" ] || { usage; fail "--ref requires a tag, branch, or commit"; }
      case "$2" in
        --*) usage; fail "--ref requires a tag, branch, or commit" ;;
      esac
      ref="$2"
      shift 2
      ;;
    --force)
      force=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown argument: $1"
      ;;
  esac
done

for tool in curl tar mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "required command not found: $tool"
done

codex_home="${CODEX_HOME:-$HOME/.codex}"
target_parent="$codex_home/skills"
target="$target_parent/task-graph"

if path_exists "$target" && [ "$force" -ne 1 ]; then
  fail "Task Graph is already installed at $target; rerun with --force to replace it"
fi

temporary_root="$(mktemp -d)"
candidate=""
backup=""

cleanup() {
  status=$?
  if [ -n "$backup" ] && path_exists "$backup" && ! path_exists "$target"; then
    mv "$backup" "$target" || true
  fi
  [ -n "$candidate" ] && [ -e "$candidate" ] && rm -rf "$candidate"
  [ -n "$backup" ] && path_exists "$backup" && rm -rf "$backup"
  rm -rf "$temporary_root"
  exit "$status"
}
trap cleanup EXIT

archive="$temporary_root/task-graph.tar.gz"
extracted="$temporary_root/extracted"
mkdir "$extracted"
archive_url="https://github.com/$REPOSITORY/archive/$ref.tar.gz"

curl --fail --location --silent --show-error -o "$archive" "$archive_url"
tar -xzf "$archive" -C "$extracted"

staged_skill=""
for directory in "$extracted"/*; do
  if [ -f "$directory/SKILL.md" ]; then
    staged_skill="$directory"
    break
  fi
done

[ -n "$staged_skill" ] || fail "archive does not contain a skill directory with SKILL.md"
for required in scripts references agents; do
  [ -d "$staged_skill/$required" ] || fail "archive is missing required directory: $required"
done

mkdir -p "$target_parent"
candidate="$(mktemp -d "$target_parent/.task-graph-new.XXXXXX")"
rmdir "$candidate"
mv "$staged_skill" "$candidate"

if path_exists "$target"; then
  backup="$(mktemp -d "$target_parent/.task-graph-backup.XXXXXX")"
  rmdir "$backup"
  mv "$target" "$backup"
fi
mv "$candidate" "$target"
candidate=""

printf 'Installed Task Graph at %s (ref: %s)\n' "$target" "$ref"
