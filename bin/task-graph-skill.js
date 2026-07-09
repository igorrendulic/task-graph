#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SKILL_NAME = "task-graph";
const ROOT_DIR = path.resolve(__dirname, "..");

function usage() {
  console.log(`Usage: task-graph-skill install [options]

Install the ${SKILL_NAME} skill for Codex and/or Claude Code.

Options:
  --codex-only     Install only to Codex
  --claude-only    Install only to Claude Code
  --force          Replace an existing installed skill at the target path
  --dry-run        Print actions without changing files
  -h, --help       Show this help

Environment:
  CODEX_HOME       Defaults to $HOME/.codex
  CLAUDE_HOME      Defaults to $HOME/.claude`);
}

function die(message) {
  console.error(`Error: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = {
    installCodex: true,
    installClaude: true,
    force: false,
    dryRun: false,
  };

  const rest = [...argv];
  if (rest[0] === "install") {
    rest.shift();
  } else if (rest.length > 0 && rest[0] !== "-h" && rest[0] !== "--help") {
    die(`Unknown command: ${rest[0]}`);
  }

  for (const arg of rest) {
    switch (arg) {
      case "--codex-only":
        args.installCodex = true;
        args.installClaude = false;
        break;
      case "--claude-only":
        args.installCodex = false;
        args.installClaude = true;
        break;
      case "--force":
        args.force = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "-h":
      case "--help":
        usage();
        process.exit(0);
        break;
      default:
        die(`Unknown option: ${arg}`);
    }
  }

  return args;
}

function homeDir() {
  return os.homedir();
}

function codexHome() {
  return process.env.CODEX_HOME || path.join(homeDir(), ".codex");
}

function claudeHome() {
  return process.env.CLAUDE_HOME || path.join(homeDir(), ".claude");
}

function isSafeTarget(target) {
  const normalized = path.normalize(target);
  return (
    path.basename(normalized) === SKILL_NAME &&
    path.basename(path.dirname(normalized)) === "skills"
  );
}

function existsOrSymlink(target) {
  try {
    fs.lstatSync(target);
    return true;
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

function run(label, action, options) {
  if (options.dryRun) {
    console.log(`dry-run: ${label}`);
  } else {
    action();
  }
}

function assertPayloadExists() {
  for (const relativePath of ["SKILL.md", "scripts/kanban.py"]) {
    const payloadPath = path.join(ROOT_DIR, relativePath);
    if (!fs.existsSync(payloadPath)) {
      die(`Missing ${payloadPath}`);
    }
  }
}

function copyPayload(dest, includeAgents, options) {
  const tmp = `${dest}.tmp.${process.pid}`;

  run(
    `rm -rf ${tmp}`,
    () => fs.rmSync(tmp, { recursive: true, force: true }),
    options,
  );
  run(
    `mkdir -p ${path.join(tmp, "scripts")}`,
    () => fs.mkdirSync(path.join(tmp, "scripts"), { recursive: true }),
    options,
  );
  run(
    `cp ${path.join(ROOT_DIR, "SKILL.md")} ${path.join(tmp, "SKILL.md")}`,
    () => fs.copyFileSync(path.join(ROOT_DIR, "SKILL.md"), path.join(tmp, "SKILL.md")),
    options,
  );
  run(
    `cp ${path.join(ROOT_DIR, "scripts", "kanban.py")} ${path.join(tmp, "scripts", "kanban.py")}`,
    () => fs.copyFileSync(
      path.join(ROOT_DIR, "scripts", "kanban.py"),
      path.join(tmp, "scripts", "kanban.py"),
    ),
    options,
  );

  if (includeAgents && fs.existsSync(path.join(ROOT_DIR, "agents", "openai.yaml"))) {
    run(
      `mkdir -p ${path.join(tmp, "agents")}`,
      () => fs.mkdirSync(path.join(tmp, "agents"), { recursive: true }),
      options,
    );
    run(
      `cp ${path.join(ROOT_DIR, "agents", "openai.yaml")} ${path.join(tmp, "agents", "openai.yaml")}`,
      () => fs.copyFileSync(
        path.join(ROOT_DIR, "agents", "openai.yaml"),
        path.join(tmp, "agents", "openai.yaml"),
      ),
      options,
    );
  }

  if (options.dryRun) {
    console.log(`dry-run: mv ${tmp} ${dest}`);
    return;
  }

  fs.rmSync(dest, { recursive: true, force: true });
  fs.renameSync(tmp, dest);
}

function installTarget(label, dest, includeAgents, options) {
  if (!isSafeTarget(dest)) {
    die(`Refusing unsafe ${label} target: ${dest}`);
  }

  console.log(`Installing ${SKILL_NAME} for ${label}: ${dest}`);

  if (existsOrSymlink(dest)) {
    if (!options.force) {
      die(`${label} skill already exists at ${dest}. Re-run with --force to replace it.`);
    }
    run(`rm -rf ${dest}`, () => fs.rmSync(dest, { recursive: true, force: true }), options);
  }

  run(
    `mkdir -p ${path.dirname(dest)}`,
    () => fs.mkdirSync(path.dirname(dest), { recursive: true }),
    options,
  );
  copyPayload(dest, includeAgents, options);
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  assertPayloadExists();

  if (options.installCodex) {
    installTarget("Codex", path.join(codexHome(), "skills", SKILL_NAME), true, options);
  }

  if (options.installClaude) {
    installTarget("Claude Code", path.join(claudeHome(), "skills", SKILL_NAME), false, options);
  }

  console.log("");
  console.log(`Installed skill name: ${SKILL_NAME}`);
  console.log(`Codex invocation: $${SKILL_NAME}`);
}

main();
