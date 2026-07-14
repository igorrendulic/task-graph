# Guarded Worker Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add FirstMate-inspired per-run delivery policy, enforced worker isolation, conservative tmux liveness, and safe delivery/teardown to Task Graph.

**Architecture:** `scripts/kanban.py` becomes the authority for immutable run-policy and runtime-boundary records. The skill remains the controller runbook, but delegates validation and state transitions to helper commands. Delivery is explicit and status-driven; `+yolo` removes only routine merge confirmation after green verification.

**Tech Stack:** Python 3 standard library, Git CLI, tmux, GitHub CLI for PR modes, Markdown documentation, `unittest`.

## Global Constraints

- A new run must declare exactly one delivery mode: `no-mistakes`, `direct-pr`, or `local-only`.
- `+yolo` is a per-run policy; it never permits red delivery, security-sensitive actions, irreversible actions, or discard.
- `launch-exec` must reject the controller checkout, a non-worktree directory, and a branch mismatch before writing a runtime record or creating a tmux session.
- Runtime records must retain the verified Git worktree root, branch, and base commit.
- Tmux liveness must be `RUNNING`, `IDLE_OR_DEAD`, or `UNKNOWN`; `UNKNOWN` never triggers an automatic relaunch.
- Automatic controller monitoring remains immediate standalone JSON probe then platform-native 60-second waits; no shell `sleep`, compound command, or `status --watch` polling.
- Teardown may remove only a clean worktree whose work is landed; `discard` is the only override for unlanded work.

---

### Task 1: Persist an explicit per-run delivery policy

**Files:**
- Modify: `scripts/kanban.py`
- Modify: `tests/test_kanban.py`

**Interfaces:**
- Produces `RunPolicy(mode: str, yolo: bool)` serialized at `.agent/<plan>/runs/<run-id>/policy.json`.
- `command_reserve(repo, plan, limit, run_id, delivery_mode, yolo)` writes policy before moving tasks.

- [ ] **Step 1: Write failing policy tests**

```python
def test_reserve_requires_and_persists_delivery_policy(self) -> None:
    write_task(self.repo, self.plan, "todo", "001-work.md", "Work")
    with self.assertRaisesRegex(SystemExit, "--delivery-mode"):
        KANBAN.command_reserve(self.repo, self.plan, 1, "run-a", None, False)
    KANBAN.command_reserve(self.repo, self.plan, 1, "run-a", "direct-pr", True)
    policy = json.loads((self.repo / ".agent" / self.plan / "runs" / "run-a" / "policy.json").read_text())
    self.assertEqual({"mode": "direct-pr", "yolo": True}, policy)

def test_reserve_rejects_unknown_or_conflicting_policy(self) -> None:
    with self.assertRaisesRegex(SystemExit, "delivery mode"):
        KANBAN.validate_run_policy("merge-everything", False)
```

- [ ] **Step 2: Run the two tests and confirm they fail because policy helpers and arguments do not exist**

Run: `python3 -m unittest tests.test_kanban.KanbanTest.test_reserve_requires_and_persists_delivery_policy tests.test_kanban.KanbanTest.test_reserve_rejects_unknown_or_conflicting_policy`

Expected: failure naming missing `validate_run_policy` or the old `command_reserve` signature.

- [ ] **Step 3: Add the policy model and CLI arguments**

```python
DELIVERY_MODES = frozenset({"no-mistakes", "direct-pr", "local-only"})

def validate_run_policy(mode: str | None, yolo: bool) -> dict[str, object]:
    if mode not in DELIVERY_MODES:
        raise SystemExit("reserve requires --delivery-mode no-mistakes|direct-pr|local-only")
    return {"mode": mode, "yolo": yolo}

def policy_path(repo: Path, plan: str, run_id: str) -> Path:
    return run_dir(repo, plan, run_id) / "policy.json"
```

Make `command_reserve` validate and atomically write the policy before calling `schedule_tasks`; add required `--delivery-mode` and optional `--yolo` parser arguments only for `reserve`.

- [ ] **Step 4: Re-run the policy tests**

Run: `python3 -m unittest tests.test_kanban.KanbanTest.test_reserve_requires_and_persists_delivery_policy tests.test_kanban.KanbanTest.test_reserve_rejects_unknown_or_conflicting_policy`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/kanban.py tests/test_kanban.py
git commit -m "feat: persist per-run delivery policy"
```

### Task 2: Enforce the worktree boundary and record Git identity

**Files:**
- Modify: `scripts/kanban.py`
- Modify: `tests/test_kanban.py`

**Interfaces:**
- Produces `verified_worktree(repo, worktree, branch) -> tuple[Path, str]` returning root and base SHA.
- Extends `new_runtime_record` with a required `base_commit: str` parameter and adds `base_commit` to `RUNTIME_FIELDS`.

- [ ] **Step 1: Write failing boundary tests**

```python
def test_launch_exec_rejects_controller_checkout_before_runtime_write(self) -> None:
    write_task(self.repo, self.plan, "in-progress", "001-work.md", "Work")
    with self.assertRaisesRegex(SystemExit, "controller checkout"):
        KANBAN.command_launch_exec(self.repo, self.plan, "run-a", "001-work.md", "main", self.repo)
    self.assertFalse((self.repo / ".agent" / self.plan / "runs" / "run-a" / "runtime").exists())

def test_runtime_record_includes_verified_base_commit(self) -> None:
    task = KANBAN.Task("in-progress", Path("001-work.md"), "Work", (), "ship")
    record = KANBAN.new_runtime_record(
        task=task, plan="first-plan", run_id="run-a", branch="task-graph/first-plan/001-work",
        worktree=Path("/tmp/worktree"), brief=Path("/tmp/brief.md"), report=Path("/tmp/report.md"),
        log=Path("/tmp/task.log"), command=["codex", "exec"], base_commit="a" * 40,
    )
    self.assertEqual("a" * 40, record["base_commit"])
    self.assertTrue(KANBAN.is_valid_runtime_record(record))
```

- [ ] **Step 2: Run the tests and confirm the old launcher accepts the controller checkout and has no base field**

Run: `python3 -m unittest tests.test_kanban.KanbanTest.test_launch_exec_rejects_controller_checkout_before_runtime_write tests.test_kanban.KanbanTest.test_runtime_record_includes_verified_base_commit`

Expected: failure.

- [ ] **Step 3: Add a fail-closed verifier before `ensure_run_dirs`**

```python
def verified_worktree(repo: Path, worktree: Path, branch: str) -> tuple[Path, str]:
    root = Path(git_output(worktree, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != worktree.resolve():
        raise SystemExit("--worktree must be a Git worktree root")
    if root == repo.resolve():
        raise SystemExit("--worktree must not be the controller checkout")
    if git_output(root, "branch", "--show-current").strip() != branch:
        raise SystemExit("--worktree branch does not match --branch")
    return root, resolved_commit(root, "HEAD")
```

Call this immediately after the tmux availability check and before any run-directory or runtime-record mutation. Store `base_commit` in the record and update all test fixtures to include it.

- [ ] **Step 4: Re-run launcher and runtime-record tests**

Run: `python3 -m unittest tests.test_kanban -k "launch_exec or runtime_record"`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/kanban.py tests/test_kanban.py
git commit -m "feat: guard unattended worker worktrees"
```

### Task 3: Make tmux status process-aware and conservative

**Files:**
- Modify: `scripts/kanban.py`
- Modify: `tests/test_kanban.py`

**Interfaces:**
- Replaces boolean `tmux_alive` injection with `tmux_liveness(session) -> Literal["RUNNING", "IDLE_OR_DEAD", "UNKNOWN"]`.
- `status_entry` uses `RUNNING` only for positive harness-process evidence.

- [ ] **Step 1: Write failing status tests**

```python
def test_status_distinguishes_running_idle_and_unknown_tty_state(self) -> None:
    run = self.repo / ".agent" / self.plan / "runs" / "run-a"
    record = json.loads(self.write_runtime("run-a", "001-work.md").read_text())
    now = datetime.now(UTC)
    running = KANBAN.status_entry(plan=self.plan, run_id="run-a", task_name="001-work.md", run=run, record=record, tmux_liveness=lambda _: "RUNNING", stale_after=timedelta(minutes=30), now=now)
    idle = KANBAN.status_entry(plan=self.plan, run_id="run-a", task_name="001-work.md", run=run, record=record, tmux_liveness=lambda _: "IDLE_OR_DEAD", stale_after=timedelta(minutes=30), now=now)
    unknown = KANBAN.status_entry(plan=self.plan, run_id="run-a", task_name="001-work.md", run=run, record=record, tmux_liveness=lambda _: "UNKNOWN", stale_after=timedelta(minutes=30), now=now)
    self.assertEqual("RUNNING", running["state"])
    self.assertNotEqual("RUNNING", idle["state"])
    self.assertEqual("UNKNOWN", unknown["state"])
```

- [ ] **Step 2: Run the test and confirm the boolean-only status API fails**

Run: `python3 -m unittest tests.test_kanban.KanbanTest.test_status_distinguishes_running_idle_and_unknown_tty_state`

Expected: failure.

- [ ] **Step 3: Implement liveness classification without guessing**

Use `tmux has-session` first. If it is absent, return `IDLE_OR_DEAD`. If present, query `tmux display-message -p -t <session> '#{pane_current_command}'`; recognize `codex`, `claude`, `grok`, and `opencode` as `RUNNING`, bare shells as `IDLE_OR_DEAD`, and every other value or tmux read failure as `UNKNOWN`. Preserve terminal exit/report states as higher-priority evidence.

- [ ] **Step 4: Re-run all status tests**

Run: `python3 -m unittest tests.test_kanban -k status`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/kanban.py tests/test_kanban.py
git commit -m "feat: classify tmux worker liveness conservatively"
```

### Task 4: Add delivery-readiness evidence and guarded teardown

**Files:**
- Modify: `scripts/kanban.py`
- Modify: `tests/test_kanban.py`

**Interfaces:**
- Adds `delivery-ready` and `teardown` helper commands.
- `delivery-ready --run-id --task` reads `policy.json` and refuses unless task review and verification evidence are recorded; it prints the controller action that is permitted.
- `teardown --run-id --task [--discard]` removes only an owned, clean, landed worktree.

- [ ] **Step 1: Write failing delivery and teardown tests**

```python
def test_delivery_readiness_requires_green_evidence_even_when_yolo(self) -> None:
    policy = self.repo / ".agent" / self.plan / "runs" / "run-a" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"mode": "direct-pr", "yolo": True}), encoding="utf-8")
    with self.assertRaisesRegex(SystemExit, "verified review and tests"):
        KANBAN.command_delivery_ready(self.repo, self.plan, "run-a", "001-work.md")

def test_teardown_refuses_unlanded_work_without_exact_discard(self) -> None:
    with self.assertRaisesRegex(SystemExit, "unlanded work"):
        KANBAN.command_teardown(self.repo, self.plan, "run-a", "001-work.md", discard=False)
```

- [ ] **Step 2: Run the tests and confirm the commands do not exist**

Run: `python3 -m unittest tests.test_kanban.KanbanTest.test_delivery_readiness_requires_green_evidence_even_when_yolo tests.test_kanban.KanbanTest.test_teardown_refuses_unlanded_work_without_exact_discard`

Expected: failure naming missing command helpers.

- [ ] **Step 3: Implement a guarded delivery state machine**

Create helpers that read the policy, runtime record, controller review note, and test result marker. `delivery-ready` prints one of `OPEN_PR`, `MERGE_GREEN_PR`, `RUN_NO_MISTAKES`, or `FAST_FORWARD_LOCAL` only after complete green evidence; yolo selects the merge/fast-forward action, otherwise it selects the ready-for-operator action. The controller, following SKILL.md, owns `gh`, no-mistakes, and Git mutations so the helper never receives network credentials or merge authority. Refuse every delivery with failed or absent evidence.

Add a teardown helper that first checks `git status --porcelain`, then verifies the recorded head is reachable from the landing branch or remote PR head. Without `--discard`, any dirty or unlanded result fails. With `--discard`, require the exact CLI value `--discard` and append an explicit discard record before `git worktree remove`.

- [ ] **Step 4: Re-run delivery tests with mocked GitHub and Git commands**

Run: `python3 -m unittest tests.test_kanban -k "delivery or teardown"`

Expected: PASS; test doubles must assert no `git worktree remove` call on refused paths.

- [ ] **Step 5: Commit**

```bash
git add scripts/kanban.py tests/test_kanban.py
git commit -m "feat: add guarded run delivery and teardown"
```

### Task 5: Document the policy and protect its contract

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `tests/test_skill_docs.py`

**Interfaces:**
- Documents the delivery modes, `+yolo` restrictions, worktree refusal, liveness semantics, and explicit discard rule.

- [ ] **Step 1: Write failing documentation-contract assertions**

```python
for document in (skill, readme):
    self.assertIn("no-mistakes", document)
    self.assertIn("direct-pr", document)
    self.assertIn("local-only", document)
    self.assertIn("+yolo", document)
    self.assertIn("explicit discard", document)
self.assertIn("must not be the controller checkout", skill)
self.assertIn("UNKNOWN", skill)
```

- [ ] **Step 2: Run the contract test and confirm it fails before documentation changes**

Run: `python3 -m unittest tests/test_skill_docs.py`

Expected: failure for missing delivery-policy contract.

- [ ] **Step 3: Update both documents**

Add the mode-selection step before reservation, explain `+yolo` as green-only routine delivery, and describe explicit discard. Keep the existing low-intrusion monitoring language unchanged: controller checks remain standalone, immediate once, then platform-native 60-second cadence.

- [ ] **Step 4: Run focused and complete verification**

Run: `python3 -m unittest tests/test_skill_docs.py && python3 -m unittest discover && git diff --check`

Expected: all tests pass and no whitespace errors.

- [ ] **Step 5: Commit**

```bash
git add SKILL.md README.md tests/test_skill_docs.py
git commit -m "docs: describe guarded worker delivery policy"
```

## Final verification

- [ ] Run `python3 -m unittest discover`.
- [ ] Run `git diff --check`.
- [ ] Confirm `reserve` rejects a missing delivery mode, `launch-exec` rejects the controller checkout before mutation, yolo refuses non-green delivery, and teardown refuses non-discarded unlanded work.
