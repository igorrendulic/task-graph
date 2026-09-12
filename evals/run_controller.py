"""Opt-in, real-process integration evals for the Task Graph controller."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.task_graph_runtime import TaskGraphRuntimeError
from scripts.task_graph_tmux import TmuxClient


_ROOT = Path(__file__).resolve().parent.parent
_CLI = _ROOT / "scripts" / "task_graph_cli.py"
_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class EvalTask:
    task_id: str
    action: str
    depends_on: tuple[str, ...] = ()


@dataclass
class EvalRun:
    root: Path
    repository: Path
    control: Path
    worker: Path
    run_dir: Path
    session: str

    def state(self) -> dict:
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class EvalScenario:
    name: str
    description: str
    tasks: tuple[EvalTask, ...]
    max_workers: int
    assertion: Callable[[EvalRun], None]


def run_controller_evals() -> None:
    """Run deterministic controller lifecycle scenarios in temporary resources."""
    _require_executables()
    passed = 0
    for scenario in _EVAL_SCENARIOS:
        started_at = time.monotonic()
        print(f"RUN  {scenario.name} — {scenario.description}", flush=True)
        try:
            _run_scenario(
                scenario.name,
                scenario.tasks,
                scenario.max_workers,
                scenario.assertion,
            )
        except Exception:
            elapsed = time.monotonic() - started_at
            print(f"FAIL {scenario.name} ({elapsed:.1f}s)", flush=True)
            raise
        elapsed = time.monotonic() - started_at
        print(f"PASS {scenario.name} ({elapsed:.1f}s)", flush=True)
        passed += 1
    print(f"Summary: {passed} passed, 0 failed")


def _run_scenario(
    name: str,
    tasks: tuple[EvalTask, ...],
    max_workers: int,
    assertion: Callable[[EvalRun], None],
) -> None:
    root = Path(tempfile.mkdtemp(prefix=f"task-graph-controller-eval-{name}-"))
    run: EvalRun | None = None
    session: str | None = None
    failure: Exception | None = None
    diagnostics = ""
    cleanup_errors: list[str] = []
    try:
        repository = root / "repository"
        control = root / "control"
        control.mkdir()
        worker = _write_worker(root)
        _create_repository(repository, control, tasks)
        run_dir = _start(repository, worker, max_workers)
        session = _session_from_state(run_dir)
        if not session:
            raise TaskGraphRuntimeError("controller eval state has no session")
        run = EvalRun(root, repository, control, worker, run_dir, session)
        _wait_for(lambda: bool(_window_names(session)), "controller session")
        assertion(run)
    except Exception as exc:
        failure = exc
        diagnostics = _scenario_diagnostics(name, run, session)
    finally:
        if session:
            _tmux("kill-session", "-t", session, check=False)
            if _session_exists(session):
                cleanup_errors.append(f"did not remove tmux session {session}")
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            cleanup_errors.append(f"did not remove temporary directory {root}")
    if failure:
        cleanup = f"\nCleanup errors: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise TaskGraphRuntimeError(
            f"controller eval {name} failed: {failure}\nDiagnostics:\n{diagnostics}{cleanup}"
        ) from failure
    if cleanup_errors:
        raise TaskGraphRuntimeError(f"controller eval {name} cleanup failed: {'; '.join(cleanup_errors)}")


def _assert_parallel_success(run: EvalRun) -> None:
    _wait_for(
        lambda: _workers_are_ready(run, "001-alpha", "002-beta"),
        "two concurrent worker attempts",
    )
    state = run.state()
    _require(Path(state["integrationWorktree"]).is_dir(), "integration worktree was not created")
    _require(_branch_exists(run.repository, state["featureBranch"]), "feature branch was not created")
    _require(state["workerCommand"] == str(run.worker), "worker command was not persisted")
    _require(
        set(_window_names(run.session)) == {"controller", "001-alpha-attempt-1", "002-beta-attempt-1"},
        "parallel run did not have exactly controller plus two named worker windows",
    )
    _release(run)
    state = _wait_for(lambda: _terminal_state(run), "parallel integration")
    _require(all(task["status"] == "integrated" for task in state["tasks"].values()), "parallel tasks did not integrate")
    integration = Path(state["integrationWorktree"])
    for task_id in ("001-alpha", "002-beta"):
        attempt = state["tasks"][task_id]["attempts"][0]
        _require(not Path(attempt["worktree"]).exists(), f"successful {task_id} worktree remains")
        _require((integration / "outputs" / f"{task_id}.txt").is_file(), f"{task_id} change was not integrated")
        _require(Path(attempt["combinedLog"]).is_file(), f"{task_id} combined log is missing")


def _assert_serial_success(run: EvalRun) -> None:
    state = _wait_for(lambda: _terminal_state(run), "serial integration")
    first = state["tasks"]["001-first"]
    second = state["tasks"]["002-second"]
    _require(first["status"] == second["status"] == "integrated", "serial tasks did not integrate")
    launch_base = second["attempts"][0]["launchBaseSha"]
    _git(run.repository, "cat-file", "-e", f"{launch_base}:outputs/001-first.txt")
    integration = Path(state["integrationWorktree"])
    _require(
        "saw 001-first" in (integration / "outputs" / "002-second.txt").read_text(encoding="utf-8"),
        "dependent worker did not receive the integrated prerequisite in its launch base",
    )


def _assert_retry_success(run: EvalRun) -> None:
    state = _wait_for(lambda: _terminal_state(run), "retry completion")
    first = state["tasks"]["001-first"]
    _require(first["status"] == "integrated", "fail-once task did not recover")
    _require(len(first["attempts"]) == 2, "fail-once task did not make exactly two attempts")
    first_attempt, second_attempt = first["attempts"]
    _require(first_attempt["branch"] != second_attempt["branch"], "retry reused its branch")
    _require(first_attempt["worktree"] != second_attempt["worktree"], "retry reused its worktree")
    _require(Path(first_attempt["worktree"]).is_dir(), "failed retry worktree was not retained")
    _require(not Path(second_attempt["worktree"]).exists(), "successful retry worktree remains")
    _require(state["tasks"]["002-second"]["status"] == "integrated", "retry descendant did not run")


def _assert_terminal_failure(run: EvalRun) -> None:
    state = _wait_for(lambda: _terminal_state(run), "terminal failure")
    first = state["tasks"]["001-first"]
    second = state["tasks"]["002-second"]
    _require(first["status"] == "failed", "fail-twice task was not terminally failed")
    _require(second["status"] == "blocked", "failed task did not block its descendant")
    _require(len(first["attempts"]) == 2, "fail-twice task did not make exactly two attempts")
    for attempt in first["attempts"]:
        _require(Path(attempt["worktree"]).is_dir(), "failed worktree was not retained")
        _require(Path(attempt["exitFile"]).is_file(), "failed attempt exit sentinel is missing")


def _assert_resume(run: EvalRun) -> None:
    _wait_for(lambda: _workers_are_ready(run, "001-first"), "gated worker")
    before = run.state()["controller"]
    before_windows = _window_names(run.session)
    _resume(run)
    _require(_window_names(run.session) == before_windows, "live resume created a pane")
    _require(run.state()["controller"]["paneId"] == before["paneId"], "live resume replaced controller")

    _tmux("kill-pane", "-t", before["paneId"])
    _wait_for(lambda: not _controller_is_live(before), "dead controller")
    _resume(run)
    _wait_for(lambda: _window_names(run.session).count("controller-recovery") == 1, "recovery controller")
    recovered = run.state()["controller"]
    _require(
        recovered["paneId"] != before["paneId"],
        "dead-controller resume reused the original controller pane",
    )
    _release(run)
    state = _wait_for(lambda: _terminal_state(run), "recovered controller completion")
    _require(all(task["status"] == "integrated" for task in state["tasks"].values()), "recovery did not finish run")


def _assert_worker_resume(run: EvalRun) -> None:
    _wait_for(lambda: _workers_are_ready(run, "001-first"), "worker conversation and unfinished edits")
    before = run.state()
    attempt = before['tasks']['001-first']['attempts'][0]
    _tmux('kill-pane', '-t', before['controller']['paneId'])
    _wait_for(lambda: not _controller_is_live(before['controller']), 'stopped controller')
    _tmux('kill-pane', '-t', attempt['paneId'])
    _require(not Path(attempt['exitFile']).exists(), 'interrupted worker unexpectedly wrote a sentinel')
    _resume(run)
    state = _wait_for(lambda: _terminal_state(run), 'worker conversation continuation')
    task = state['tasks']['001-first']
    _require(task['status'] == 'integrated', 'resumed worker did not integrate')
    _require(len(task['attempts']) == 1, 'conversation recovery created a fresh task attempt')
    resumed = task['attempts'][0]
    _require(resumed['worktree'] == attempt['worktree'], 'conversation recovery replaced the worktree')
    _require(resumed['threadId'] == '0199a213-81c0-7800-8aa1-bbab2a035a53', 'wrong conversation resumed')
    _require(len(resumed['recoveries']) == 1, 'original invocation logs were not retained')
    _require(resumed['verification']['passed'], 'resumed commit was not verified')


def _assert_live_dashboard(run: EvalRun) -> None:
    _wait_for(lambda: (run.control / 'verifying').exists(), 'long verification command')
    _wait_for(lambda: _workers_are_ready(run, '002-live'), 'live worker during verification')
    pane = run.state()['controller']['paneId']
    _tmux('resize-window', '-t', pane, '-x', '140', '-y', '30')
    (run.control / 'activity').touch()

    def capture():
        return _tmux('capture-pane', '-p', '-t', pane).stdout

    def observed_activity():
        text = capture()
        live_row = next((line for line in text.splitlines() if line.startswith('● 002-live')), '')
        return text if text.startswith('Task Graph') and 'Dashboard live event' in live_row and 'Check 1/2' in text else None

    panel = _wait_for(observed_activity, 'live dashboard activity')
    _require('verifying' in panel and 'working' in panel, 'dashboard conflates worker and verification phases')
    _require('RECENT EVENTS' in panel, 'dashboard omits recent events')
    _require(sum(line.startswith('● 002-live') for line in panel.splitlines()) == 1,
             'resize left stale task rows in the terminal')
    _require(run.state()['tasks']['001-check']['status'] == 'running', 'verification gate finished early')
    print('Captured live dashboard during verification:\n' + panel.rstrip(), flush=True)
    _tmux('resize-window', '-t', pane, '-x', '60', '-y', '16')
    _wait_for(lambda: 'TASK / PHASE / AGE' in capture(), 'compact dashboard after resize')
    (run.control / 'verification-release').touch()
    _release(run)
    state = _wait_for(lambda: _terminal_state(run), 'dashboard scenario integration')
    _require(all(task['status'] == 'integrated' for task in state['tasks'].values()), 'dashboard scenario failed')


_EVAL_SCENARIOS = (
    EvalScenario(
        'worker-resume',
        'verifies an interrupted conversation resumes with unfinished edits',
        (EvalTask('001-first', 'resume-once'),), 1, _assert_worker_resume,
    ),
    EvalScenario(
        'verification-failure',
        'rejects successful worker exits when independent verification fails',
        (EvalTask('001-first', 'bad-content'), EvalTask('002-second', 'success', ('001-first',))),
        1, _assert_terminal_failure,
    ),
    EvalScenario(
        "parallel-success",
        "verifies two independent tasks execute concurrently and integrate",
        (EvalTask("001-alpha", "gate-success"), EvalTask("002-beta", "gate-success")),
        2,
        _assert_parallel_success,
    ),
    EvalScenario(
        "serial-success",
        "verifies a dependent task receives its integrated prerequisite",
        (
            EvalTask("001-first", "success"),
            EvalTask("002-second", "serial-success", ("001-first",)),
        ),
        2,
        _assert_serial_success,
    ),
    EvalScenario(
        "retry-success",
        "verifies a failed task retries and its dependent task completes",
        (
            EvalTask("001-first", "fail-once"),
            EvalTask("002-second", "success", ("001-first",)),
        ),
        1,
        _assert_retry_success,
    ),
    EvalScenario(
        "terminal-failure",
        "verifies exhausted retries fail and block dependent tasks",
        (
            EvalTask("001-first", "fail-twice"),
            EvalTask("002-second", "success", ("001-first",)),
        ),
        1,
        _assert_terminal_failure,
    ),
    EvalScenario(
        "resume",
        "verifies live and dead controllers resume safely",
        (
            EvalTask("001-first", "gate-success"),
            EvalTask("002-second", "success", ("001-first",)),
        ),
        1,
        _assert_resume,
    ),
    EvalScenario(
        'live-dashboard', 'observes live worker events during verification and terminal resize',
        (EvalTask('001-check', 'slow-verification'), EvalTask('002-live', 'gate-success')),
        2, _assert_live_dashboard,
    ),
)


def _create_repository(repository: Path, control: Path, tasks: tuple[EvalTask, ...]) -> None:
    plan = repository / ".agent" / "eval"
    todo = plan / "todo"
    todo.mkdir(parents=True)
    for task in tasks:
        dependencies = "None" if not task.depends_on else "\n".join(
            f"- {dependency}.md" for dependency in task.depends_on
        )
        (todo / f"{task.task_id}.md").write_text(
            "\n".join(
                [
                    f"# {task.task_id}",
                    "",
                    "## Dependencies",
                    "",
                    dependencies,
                    "",
                    f"# EVAL_ACTION: {task.action}",
                    f"# EVAL_CONTROL_DIR: {control}",
                    "# EVAL_EXPECT: 001-first",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    dag_tasks = []
    for task in tasks:
        commands = [[sys.executable, '-c', f"from pathlib import Path; assert {task.task_id!r} + ' completed' in Path('outputs/{task.task_id}.txt').read_text()"]]
        if task.action == 'slow-verification':
            check = control / 'verify.py'
            check.write_text(
                "import time\nfrom pathlib import Path\n"
                f"control = Path({str(control)!r})\n"
                "(control / 'verifying').touch()\n"
                "deadline = time.monotonic() + 15\n"
                "while not (control / 'verification-release').exists():\n"
                "    assert time.monotonic() < deadline, 'verification gate timed out'\n"
                "    time.sleep(.05)\n"
            )
            commands.insert(0, [sys.executable, str(check)])
        dag_tasks.append(
            {
                "id": task.task_id,
                "taskFile": f"{task.task_id}.md",
                "title": task.task_id,
                "instructions": f"Controller eval action: {task.action}",
                "predictedPaths": [f"outputs/{task.task_id}.txt"],
                "verification": {"commands": commands},
                "predictedSymbols": [],
                "dependsOn": list(task.depends_on),
                "parallelSafe": not task.depends_on,
                "schedulingRationale": "deterministic controller eval fixture",
            }
        )
    (plan / "dag.json").write_text(
        json.dumps({"schemaVersion": 1, "planSlug": "eval", "tasks": dag_tasks}, indent=2),
        encoding="utf-8",
    )
    (repository / "base.txt").write_text("controller eval baseline\n", encoding="utf-8")
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Task Graph controller eval")
    _git(repository, "config", "user.email", "task-graph-controller-eval@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "controller eval baseline")


def _write_worker(root: Path) -> Path:
    worker = root / "scripted-worker.py"
    worker.write_text(
        f"#!{sys.executable}\n"
        + '''import json
import re
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.argv[-1]
worktree = Path(sys.argv[sys.argv.index("-C") + 1])

def marker(name):
    match = re.search(r"^# " + name + r": (.+)$", prompt, re.MULTILINE)
    if match is None:
        raise SystemExit("missing " + name)
    return match.group(1).strip()

task_id = re.search(r"Task Graph task ([^\\s.]+)", prompt).group(1)
action = marker("EVAL_ACTION")
control = Path(marker("EVAL_CONTROL_DIR"))
expected = marker("EVAL_EXPECT")
control.mkdir(parents=True, exist_ok=True)

if action == "resume-once":
    thread_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
    output = worktree / "outputs" / (task_id + ".txt")
    if "resume" in sys.argv:
        assert sys.argv[sys.argv.index("resume") + 1] == thread_id
        assert output.read_text() == "unfinished edits"
    else:
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}), flush=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("unfinished edits")
        (control / ("ready-" + task_id)).write_text("ready\\n", encoding="utf-8")
        time.sleep(30)
        raise SystemExit("worker should have been interrupted")
elif action == "gate-success":
    (control / ("ready-" + task_id)).write_text("ready\\n", encoding="utf-8")
    deadline = time.monotonic() + 15
    activity_sent = False
    while not (control / "release").exists():
        if (control / "activity").exists() and not activity_sent:
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Dashboard live event"}}), flush=True)
            activity_sent = True
        if time.monotonic() >= deadline:
            raise SystemExit("gate timed out")
        time.sleep(0.05)
elif action == "fail-once":
    count_path = control / (task_id + ".attempts")
    count = int(count_path.read_text(encoding="utf-8")) + 1 if count_path.exists() else 1
    count_path.write_text(str(count), encoding="utf-8")
    if count == 1:
        raise SystemExit(17)
elif action == "fail-twice":
    raise SystemExit(18)

output = worktree / "outputs" / (task_id + ".txt")
output.parent.mkdir(parents=True, exist_ok=True)
contents = task_id + " completed\\n"
if action == "bad-content":
    contents = "incorrect result\\n"
if action == "serial-success":
    prerequisite = worktree / "outputs" / (expected + ".txt")
    if not prerequisite.is_file():
        raise SystemExit("missing integrated prerequisite")
    contents += "saw " + expected + "\\n"
output.write_text(contents, encoding="utf-8")
subprocess.run(["git", "add", "outputs"], cwd=worktree, check=True)
subprocess.run(["git", "commit", "--quiet", "-m", "worker " + task_id], cwd=worktree, check=True)
''',
        encoding="utf-8",
    )
    worker.chmod(0o755)
    return worker.resolve()


def _start(repository: Path, worker: Path, max_workers: int) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(_CLI),
            "start",
            "eval",
            "--max-workers",
            str(max_workers),
            "--worker-command",
            str(worker),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TaskGraphRuntimeError(_command_failure("controller eval start", result))
    runs = sorted((repository / ".agent" / "eval" / "runs").iterdir())
    if len(runs) != 1:
        raise TaskGraphRuntimeError("controller eval did not create exactly one run")
    return runs[0]


def _resume(run: EvalRun) -> None:
    result = subprocess.run(
        [sys.executable, str(_CLI), "resume", "eval", run.run_dir.name],
        cwd=run.repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TaskGraphRuntimeError(_command_failure("controller eval resume", result))


def _terminal_state(run: EvalRun) -> dict | None:
    state = run.state()
    statuses = [task["status"] for task in state["tasks"].values()]
    if all(status in {"integrated", "failed", "blocked"} for status in statuses):
        return state
    return None


def _workers_are_ready(run: EvalRun, *task_ids: str) -> bool:
    if not all((run.control / f"ready-{task_id}").is_file() for task_id in task_ids):
        return False
    state = run.state()
    return all(
        state["tasks"][task_id]["status"] == "running"
        and len(state["tasks"][task_id]["attempts"]) == 1
        for task_id in task_ids
    )


def _release(run: EvalRun) -> None:
    (run.control / "release").write_text("release\n", encoding="utf-8")


def _session_from_state(run_dir: Path) -> str | None:
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return None
    return json.loads(state_path.read_text(encoding="utf-8")).get("session")


def _controller_is_live(controller: dict) -> bool:
    """Match resume's saved pane/PID liveness contract."""
    pane_id, pid = controller.get("paneId"), controller.get("pid")
    return bool(pane_id and isinstance(pid, int) and TmuxClient().pane_is_live(pane_id, pid))


def _branch_exists(repository: Path, branch: str) -> bool:
    return _git_result(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _window_names(session: str) -> list[str]:
    result = _tmux("list-windows", "-t", session, "-F", "#{window_name}", check=False)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _session_exists(session: str) -> bool:
    return _tmux("has-session", "-t", session, check=False).returncode == 0


def _wait_for(predicate: Callable[[], object], description: str) -> object:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TaskGraphRuntimeError(f"timed out waiting for {description}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaskGraphRuntimeError(f"controller eval assertion failed: {message}")


def _require_executables() -> None:
    missing = [command for command in ("git", "tmux") if shutil.which(command) is None]
    if missing:
        raise TaskGraphRuntimeError("controller eval requires: " + ", ".join(missing))


def _scenario_diagnostics(name: str, run: EvalRun | None, session: str | None) -> str:
    """Return a compact, best-effort snapshot before scenario cleanup."""
    lines = [f"scenario={name}", f"session={session or '<unavailable>'}"]
    if session:
        lines.append(_command_result("tmux has-session", _tmux("has-session", "-t", session, check=False)))
        lines.append(
            _command_result(
                "tmux list-windows",
                _tmux(
                    "list-windows",
                    "-t",
                    session,
                    "-F",
                    "#{window_id} #{window_name} #{pane_id} #{pane_pid} #{pane_dead} #{pane_current_command}",
                    check=False,
                ),
            )
        )
    if run is None:
        lines.append("controller=<run was not created>")
    else:
        try:
            lines.append("controller=" + json.dumps(run.state().get("controller", {}), sort_keys=True))
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"controller=<state unavailable: {exc}>")
    return "\n".join(lines)


def _command_failure(description: str, result: subprocess.CompletedProcess[str]) -> str:
    return _command_result(description, result)


def _command_result(description: str, result: subprocess.CompletedProcess[str]) -> str:
    output = result.stdout.strip()
    error = result.stderr.strip()
    details = ", ".join(
        part
        for part in (
            f"returncode={result.returncode}",
            f"stdout={output!r}" if output else "",
            f"stderr={error!r}" if error else "",
        )
        if part
    )
    return f"{description}: {details}"


def _git(repository: Path, *args: str) -> str:
    result = _git_result(repository, *args)
    if result.returncode != 0:
        raise TaskGraphRuntimeError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
    return result.stdout.strip()


def _git_result(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repository, capture_output=True, text=True, check=False)


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise TaskGraphRuntimeError(result.stderr.strip() or "tmux command failed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Task Graph controller integration evals")
    parser.parse_args()
    try:
        run_controller_evals()
    except TaskGraphRuntimeError as exc:
        print(f"task-graph eval: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
