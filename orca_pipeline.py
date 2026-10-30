#!/usr/bin/env python3
"""Semi-automatic Orca dev pipeline, driven by a per-project JSON config.

Stages run in the coordinator's Orca workspace:
  1. plan       (default: Claude Code)  -> approval gate: you continue, revise, or quit
  2. implement  (default: OpenCode)
  3. review     (default: Codex), plus an optional E2E worker in parallel
  4. fix rounds (default: OpenCode) until review passes or max_fix_rounds is reached
  5. ship       (default: Codex + the ship skill) -> only after you approve it at the ship gate

The workspace card on the Orca board follows the stages (in-progress, in-review, completed),
and gets a comment plus the unread dot whenever the pipeline waits for you or stops.

Run it INSIDE an Orca terminal (it needs ORCA_TERMINAL_HANDLE), from the project root:
  python3 orca_pipeline.py --config projects/my-app.json --mode small --slug my-feature \
      --feature "Plain-language description of the feature"

Add --dry-run to print every worker spec without starting any worker.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
LOG_PATH = None
DRY_RUN = False

ANSWER_FILE = None  # .orca-pipeline/<slug>.answer: answers that do not come from the keyboard
NO_HUMAN_ANSWER = ("No human is available. Choose the simplest option that satisfies the spec "
                   "and the plan, note the assumption in your report, and continue.")

EVIDENCE_RULE = (
    "Report: in your worker report, list each test command you ran with its quoted runner "
    "summary line (for example 'Tests: 15, Assertions: 15, Failures: 3.'). For a bug fix, also "
    "run the new test once before the fix and quote that failing summary. Keep the report under "
    "3000 characters.")

DEFAULT_SKILL_PHRASES = {
    "claude": "Invoke the Skill tool for each of these skills, in order, before you start: {skills}.",
    "opencode": "Load each of these skills with your skill tool before you start: {skills}.",
    "codex": "Use these skills: {dollar_skills}.",
}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_PATH:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")


def orca(*args, check=True):
    cmd = [ORCA, *args, "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if LOG_PATH:
        with open(LOG_PATH, "a") as f:
            f.write(f"$ {' '.join(cmd[:4])} ...\n{proc.stdout[-4000:]}\n")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {"ok": False, "raw": proc.stdout + proc.stderr}
    if check and not data.get("ok"):
        log(f"orca {' '.join(args[:2])} failed: {json.dumps(data)[:800]}")
        sys.exit(1)
    return data


def resolve_base(cfg, path="."):
    """Base branch for task branches and PRs: (branch, note). note is "" when as configured.

    Order: ship.base_branch from the config, then staging for projects under ~/Sites (a
    staging-first branch convention), then origin's default branch, then main, then master. A candidate
    counts only when origin has it (refs/remotes/origin/<name>, as of the last fetch).
    """
    configured = cfg.get("ship", {}).get("base_branch")
    path = os.path.realpath(os.path.expanduser(path))
    refs = subprocess.run(["git", "-C", path, "for-each-ref", "--format=%(refname:short)",
                           "refs/remotes/origin"], capture_output=True, text=True).stdout.split()
    remote = {r.split("/", 1)[1] for r in refs if "/" in r}
    head = subprocess.run(["git", "-C", path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                          capture_output=True, text=True).stdout.strip().split("/", 1)[-1]
    under_sites = path.startswith(os.path.expanduser("~/Sites/"))
    candidates = [configured] + (["staging"] if under_sites else []) + [head, "main", "master"]
    for name in candidates:
        if name and name in remote:
            note = "" if name == configured else (
                f"ship.base_branch '{configured}' is not on origin; using '{name}'" if configured
                else f"no ship.base_branch in the config; using '{name}'")
            return name, note
    return configured or "main", "no base branch found on origin (fetch first?)"


def find_key(obj, key):
    """Depth-first search for the first non-empty value under `key`."""
    if isinstance(obj, dict):
        if obj.get(key) not in (None, ""):
            return obj[key]
        for v in obj.values():
            found = find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


def parse_payload(message):
    payload = message.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return payload


def skill_line(cfg, agent, skills):
    if not skills:
        return ""
    phrases = {**DEFAULT_SKILL_PHRASES, **cfg.get("skill_phrases", {})}
    template = phrases.get(agent, DEFAULT_SKILL_PHRASES["opencode"])
    return template.format(skills=", ".join(skills),
                           dollar_skills=", ".join(f"${s}" for s in skills))


def read_line(prompt, timeout=None, allow_file=True):
    """Read one answer line. Returns None when `timeout` seconds pass without an answer.

    Guards learned from real runs:
    - Input typed (or delivered twice by a terminal tool) while no question was open is
      dropped first, so it cannot answer the next gate.
    - The terminal is read in non-canonical mode: the OS limit of 1024 bytes per line in
      canonical mode silently dropped longer answers, Enter included, and stalled the run.
    - With allow_file, the answer may instead be written to ANSWER_FILE (any length), which
      a person or a tool can do without typing into this terminal.
    """
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        return input(prompt)
    import select
    import termios
    if allow_file and ANSWER_FILE and os.path.exists(ANSWER_FILE):
        os.remove(ANSWER_FILE)  # an answer left from an earlier question
    termios.tcflush(fd, termios.TCIFLUSH)
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO)  # we echo and edit ourselves
    new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
    print(prompt, end="", flush=True)
    deadline = time.time() + timeout if timeout else None
    buf = []
    termios.tcsetattr(fd, termios.TCSANOW, new)
    try:
        while True:
            if allow_file and ANSWER_FILE and os.path.exists(ANSWER_FILE):
                with open(ANSWER_FILE) as f:
                    text = f.read().strip()
                os.remove(ANSWER_FILE)
                print(f"[answer from {ANSWER_FILE}] {text[:200]}", flush=True)
                return text
            if deadline and time.time() >= deadline:
                print(" [no answer in time]", flush=True)
                return None
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                continue
            chunk = os.read(fd, 65536).decode("utf-8", errors="replace")
            if not chunk:
                raise EOFError
            for ch in chunk:
                if ch in "\r\n":
                    print(flush=True)
                    return "".join(buf)
                if ch in "\x7f\b":
                    if buf:
                        buf.pop()
                        print("\b \b", end="", flush=True)
                elif ch == "\x15":  # Ctrl+U clears the line
                    print("\b \b" * len(buf), end="", flush=True)
                    buf.clear()
                elif ch.isprintable() or ch == "\t":
                    buf.append(ch)
                    print(ch, end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)


def read_answer(timeout=None):
    """A worker-question answer: a line, @<path> to send a file's text, or None on timeout."""
    while True:
        answer = read_line(">>> Your answer: ", timeout)
        if answer is None:
            return None
        answer = answer.strip()
        if not answer.startswith("@"):
            return answer or "Proceed with your best judgment."
        path = os.path.expanduser(answer[1:].strip())
        try:
            with open(path) as f:
                text = f.read().strip()
        except OSError as e:
            print(f"Cannot read {path}: {e}. Type the answer or another @<path>.")
            continue
        if text:
            return text
        print(f"{path} is empty. Type the answer or another @<path>.")


def ask_human(prompt, choices):
    """Read a choice from the coordinator terminal. Returns (choice, extra text)."""
    while True:
        answer = read_line(f"\n>>> {prompt} [{'/'.join(choices)}]: ").strip()
        if not answer:
            continue
        choice, _, extra = answer.partition(" ")
        if choice.lower() in choices:
            return choice.lower(), extra.strip()
        print(f"Type one of: {', '.join(choices)}")


class Pipeline:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.me = os.environ.get("ORCA_TERMINAL_HANDLE", "dry-run")
        self.run_id = None
        self.slug = args.slug
        self.plan = f"{cfg.get('plan_dir', 'docs/plans')}/{self.slug}.md"
        self.review = f"{cfg.get('review_dir', 'docs/reviews')}/{self.slug}.md"
        self.e2e_report = f"{cfg.get('review_dir', 'docs/reviews')}/{self.slug}-e2e.md"
        self.mode = cfg["modes"][args.mode]
        self.agents = {"plan": "claude", "implement": "opencode", "review": "codex",
                       "e2e": "codex", "ship": "codex", **cfg.get("agents", {})}
        self.timeout = cfg.get("stage_timeout_min", 45) * 60
        self.baseline_path = f".orca-pipeline/{self.slug}.baseline.json"
        self.reports = {}  # dispatch -> body of its worker_done message
        self.last_impl_report = ""  # handed to the next review as evidence
        known = args.known_failures or cfg.get("known_failures") or "none"
        self.base_rules = (
            "No human is watching your terminal: do not wait for approval and do not open "
            "interactive prompts; for a blocking question use the orchestration ask command from "
            "your preamble. Never write real credentials, IP addresses, or personal data into "
            f"files. Tests that already fail before this change (out of scope): {known}.")
        self.common = f"Do not commit, push, or switch branches. {self.base_rules}"

    # ---- orca plumbing -------------------------------------------------
    def start(self, stage, title, spec):
        agent = self.agents[stage]
        if DRY_RUN:
            print(f"\n===== {title} ({agent}) =====\n{spec}\n")
            return f"dry-{title}"
        data = orca("orchestration", "worker-start", "--spec", spec, "--task-title", title,
                    "--worktree", "current", "--agent", agent, "--run", self.run_id,
                    "--from", self.me)
        dispatch = find_key(data.get("result") or {}, "dispatchId")
        log(f"started {agent} for '{title}': dispatch={dispatch}")
        return dispatch

    def answer_question(self, message):
        text = str(message.get("body") or message.get("subject"))
        log(f"question from worker: {text[:500]}")
        if self.cfg.get("answer_questions", "ask_me") == "ask_me":
            self.board(comment="worker question: waiting for your answer", unread=True)
            minutes = self.cfg.get("question_timeout_min", 15)
            print(f"\n--- Worker question ---\n{text}\n")
            print(f"Answer on one line (any length), type @<path> to send a file, or write the "
                  f"answer to {ANSWER_FILE}. Without an answer in {minutes} minutes the worker is "
                  f"told to use its best judgment.")
            body = read_answer(minutes * 60)
            if body is None:
                log(f"no answer in {minutes} minutes: worker told to use its best judgment")
                body = NO_HUMAN_ANSWER
        else:
            body = NO_HUMAN_ANSWER
        orca("orchestration", "reply", "--id", message.get("id"), "--run", self.run_id,
             "--from", self.me, "--body", body, check=False)

    def board(self, column=None, comment=None, unread=False):
        """Best-effort update of this workspace's Orca board card. Never stops the pipeline.

        `column` is a key of `board` in the config (in_progress, in_review, done), which maps
        to an Orca status id, so custom board columns work too.
        """
        board = {"enabled": True, "in_progress": "in-progress", "in_review": "in-review",
                 "done": "completed", **self.cfg.get("board", {})}
        if not board["enabled"]:
            return
        set_args = ["worktree", "set", "--worktree", "current"]
        if column:
            set_args += ["--workspace-status", board[column]]
        if comment:
            set_args += ["--comment", f"[{self.slug}] {comment}"]
        if unread:
            set_args.append("--unread")
        if DRY_RUN:
            print(f"[board] {' '.join(set_args[4:])}")
            return
        if not orca(*set_args, check=False).get("ok"):
            log(f"board update failed (ignored): {' '.join(set_args[4:])}")

    def wait_all(self, dispatches, timeout=None):
        """Wait until every dispatch in `dispatches` reports. Returns {dispatch: outcome}."""
        if DRY_RUN:
            return {d: "succeeded" for d in dispatches}
        pending = set(d for d in dispatches if d)
        results = {}
        deadline = time.time() + (timeout or self.timeout)
        ack = None  # acknowledged on the next check, which also returns the next batch
        while pending and time.time() < deadline:
            check_args = ["orchestration", "check", "--run", self.run_id, "--wait",
                          "--types", "worker_done,escalation,question", "--timeout-ms", "120000"]
            if ack:
                check_args += ["--ack", ack]
            data = orca(*check_args, check=False)
            result = data.get("result") or {}
            ack = result.get("deliveryId")
            messages = result.get("messages") or []
            for m in messages:
                mtype = m.get("type")
                if mtype == "question":
                    self.answer_question(m)
                    continue
                if mtype not in ("worker_done", "escalation"):
                    continue
                dispatch = parse_payload(m).get("dispatchId")
                if dispatch not in pending:
                    log(f"ignoring {mtype} for dispatch {dispatch}")
                    continue
                outcome = parse_payload(m).get("outcome") or (
                    "failed" if mtype == "escalation" else "unknown")
                results[dispatch] = outcome
                self.reports[dispatch] = str(m.get("body") or "")
                pending.discard(dispatch)
                log(f"{mtype} {dispatch}: outcome={outcome} | {str(m.get('body'))[:600]}")
            if not messages:
                log(f"still waiting on {len(pending)} worker(s)...")
        if ack:
            orca("orchestration", "check", "--run", self.run_id, "--ack", ack, check=False)
        for d in pending:
            results[d] = "timeout"
        for d in dispatches:
            if d and not DRY_RUN:
                orca("orchestration", "worker-release", "--dispatch", d, check=False)
        return results

    # ---- git state -----------------------------------------------------
    @staticmethod
    def dirty_paths():
        """Paths that differ from HEAD, including every untracked file."""
        proc = subprocess.run(["git", "status", "--porcelain=v1", "-z", "-uall"],
                              capture_output=True, text=True)
        entries = proc.stdout.split("\0")
        paths, i = [], 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            paths.append(entry[3:])
            if entry[0] in "RC":  # a rename or copy is followed by its source path
                i += 1
        return sorted(set(paths))

    def save_baseline(self):
        """Record changes that existed before this run, once, so ship can leave them out."""
        if not os.path.exists(self.baseline_path):
            with open(self.baseline_path, "w") as f:
                json.dump(self.dirty_paths(), f, indent=1)
            log(f"baseline of pre-existing local changes saved to {self.baseline_path}")

    def run_paths(self):
        """(paths this run changed, pre-existing paths), or (None, None) without a baseline."""
        if not os.path.exists(self.baseline_path):
            return None, None
        with open(self.baseline_path) as f:
            before = set(json.load(f))
        now = set(self.dirty_paths())
        return sorted(now - before), sorted(before)

    # ---- specs ---------------------------------------------------------
    def rules(self, stage):
        return self.cfg.get("extra_rules", {}).get(stage, "")

    def test_command(self, stage):
        """Per-stage test command from `test_commands`, falling back to `test_command`."""
        return self.cfg.get("test_commands", {}).get(stage) or self.cfg["test_command"]

    def e2e_targets(self):
        """URLs for the E2E worker: the --module choices, else every configured module."""
        e2e = self.cfg.get("e2e", {})
        base = e2e.get("base_url", e2e.get("url", "")).rstrip("/")
        modules = e2e.get("modules", {})
        chosen = self.args.module.split(",") if self.args.module else list(modules)
        unknown = [m for m in chosen if m not in modules]
        if unknown:
            sys.exit(f"--module {', '.join(unknown)} not in e2e.modules: {', '.join(modules)}")
        if not modules:
            return base
        return "; ".join(f"{m}: {base}{modules[m]}" for m in chosen)

    def plan_spec(self, notes=""):
        revise = (f"\nThe previous plan in {self.plan} was rejected. Revise it with these notes: "
                  f"{notes}" if notes else "")
        return (
            f"Target: this repository. Read the project rules file first.\n"
            f"Change: plan and design this feature: {self.args.feature}{revise}\n"
            f"{skill_line(self.cfg, self.agents['plan'], self.mode.get('plan', []))}\n"
            f"Write the plan to {self.plan}: goal, files to change, step-by-step tasks, test plan "
            f"(unit tests and an end-to-end check with URL and steps), acceptance criteria.\n"
            f"Constraints: do NOT write application code. {self.rules('plan')} {self.common}\n"
            f"Ownership: only {self.plan} and planning artifacts the skills create.\n"
            f"Observable acceptance: {self.plan} exists and lists acceptance criteria.")

    def implement_spec(self):
        return (
            f"Target: this repository. Read the project rules file first.\n"
            f"Change: implement {self.plan} exactly, including its tests.\n"
            f"{skill_line(self.cfg, self.agents['implement'], self.mode.get('implement', []))}\n"
            f"Run tests with: {self.test_command('implement')}\n"
            f"Constraints: change only files the plan names. Documentation: edit only docs the plan "
            f"names, only the lines the change needs, and write those lines in English. "
            f"{self.rules('implement')} {self.common}\n"
            f"Ownership: source, test, and doc files named in the plan.\n"
            f"{EVIDENCE_RULE}\n"
            f"Observable acceptance: the plan's tests pass and every acceptance criterion is met.")

    def review_spec(self):
        return (
            f"Target: the uncommitted changes in this repository (git diff and untracked files) "
            f"against {self.plan}.\n"
            f"{skill_line(self.cfg, self.agents['review'], self.mode.get('review', []))}\n"
            f"Run tests with: {self.test_command('review')}\n"
            f"Write {self.review}, replacing any earlier report: verdict PASS/FAIL, test summary, "
            f"blocking issues with file:line and a concrete fix, non-blocking follow-ups.\n"
            f"Review scope: judge only lines added or changed in the diff. Pre-existing problems are "
            f"non-blocking follow-ups. Blocking issues are limited to functional bugs, failing tests "
            f"outside the known failures, unmet acceptance criteria, and problems introduced by "
            f"changed lines.\n"
            f"Constraints: do NOT edit any file except {self.review}. {self.rules('review')} "
            f"{self.common}\n"
            f"{self.implementer_evidence()}"
            f"Observable acceptance: {self.review} exists. Report --outcome succeeded only when the "
            f"verdict is PASS.")

    def implementer_evidence(self):
        """The last implement or fix report, so the reviewer judges evidence already produced."""
        if not self.last_impl_report:
            return ""
        return ("Evidence reported by the implementer (commands and quoted runner output). Check "
                "it against the code and rerun a cheap command when in doubt. Do not fail the "
                "review only because the same evidence is not in a second place, such as a file:\n"
                f"<<<\n{self.last_impl_report[:4000]}\n>>>\n")

    def e2e_spec(self):
        e2e = self.cfg.get("e2e", {})
        return (
            f"Target: the running app, feature '{self.slug}'. Module URLs: {self.e2e_targets()}.\n"
            f"{skill_line(self.cfg, self.agents['e2e'], e2e.get('skills', ['e2e-test-runner']))}\n"
            f"Follow the end-to-end check in {self.plan}. Use a test user only; never type real "
            f"credentials. {e2e.get('login_note', '')}\n"
            f"Write {self.e2e_report}: each step, PASS/FAIL, and anything not verified.\n"
            f"Constraints: do not edit application source or unit tests. Put any new E2E files only "
            f"under {e2e.get('files_dir', 'tests/e2e')}/{self.slug}/. {self.rules('e2e')} "
            f"{self.common}\n"
            f"Observable acceptance: {self.e2e_report} exists. Report --outcome succeeded only when "
            f"every step passes.")

    def fix_spec(self, reports):
        return (
            f"Target: this repository.\n"
            f"Change: fix every blocking issue in {', '.join(reports)} for the plan {self.plan}. "
            f"Ignore non-blocking follow-ups.\n"
            f"{skill_line(self.cfg, self.agents['implement'], self.mode.get('implement', []))}\n"
            f"Run tests with: {self.test_command('implement')}\n"
            f"Constraints: fix only the listed blocking issues. Documentation: only the lines the "
            f"fix needs, in English. Do not edit {', '.join(reports)} or any other review report: "
            f"the reviewer rewrites them. When a blocking issue asks for evidence, produce it and "
            f"put it in your worker report; the coordinator passes your report to the next review. "
            f"{self.rules('implement')} {self.common}\n"
            f"{EVIDENCE_RULE}\n"
            f"Observable acceptance: each blocking issue is addressed and the tests pass.")

    def ship_spec(self, follow_review):
        ship = self.cfg.get("ship", {})
        base, _ = resolve_base(self.cfg)
        prefix = "fix" if self.args.mode == "bugfix" else "feature"
        changed, before = (["(dry run)"], ["(dry run)"]) if DRY_RUN else self.run_paths()
        if changed is None:
            files = ("No baseline of pre-existing changes exists for this run. Before staging, "
                     "list the files you would commit and ask the coordinator to confirm them.")
        else:
            files = (f"Files this run changed (stage only these, and only after reading them): "
                     f"{', '.join(changed) or '(none)'}. Pre-existing local changes that are NOT "
                     f"part of this run (never stage them): {', '.join(before) or '(none)'}.")
        review = ("After the PR exists, follow its review as the skill describes."
                  if follow_review else
                  "The user follows the PR review: stop after the PR is created and skip review "
                  "monitoring.")
        return (
            f"Target: this repository. Ship the reviewed change for '{self.args.feature}'.\n"
            f"{skill_line(self.cfg, self.agents['ship'], ship.get('skills', []))}\n"
            f"The user approved commit, push, and a pull request to {base} for this change only.\n"
            f"Branch: if the current branch is {base} or the repository default branch, create "
            f"{prefix}/{self.slug} without discarding or switching away from the uncommitted "
            f"work; otherwise keep the current branch.\n"
            f"{files}\n"
            f"Never stage {self.cfg.get('plan_dir', 'docs/plans')}/, "
            f"{self.cfg.get('review_dir', 'docs/reviews')}/, .orca-pipeline/, or any .sql, "
            f".dump, .sqlite, .sqlite3, .psql, or .db file. Before committing, scan "
            f"`git diff --cached` for real credentials, IP addresses, server user names, and "
            f"personal or institution data; if you find any, stop and ask the coordinator.\n"
            f"{review} Never merge the PR, force-push, or bypass hooks. {self.rules('ship')} "
            f"{self.base_rules}\n"
            f"Observable acceptance: the branch is pushed and a PR to {base} exists. Put the PR "
            f"URL on the first line of your report. Report --outcome succeeded only then.")

    # ---- stages --------------------------------------------------------
    def stage_plan(self):
        notes = ""
        for attempt in range(1, 6):
            self.board("in_progress", f"planning (attempt {attempt})")
            d = self.start("plan", f"plan-{attempt}", self.plan_spec(notes))
            outcome = self.wait_all([d])[d]
            plan_ok = DRY_RUN or (outcome == "succeeded" and os.path.exists(self.plan))
            if not plan_ok:
                log(f"plan stage outcome={outcome}, plan exists={os.path.exists(self.plan)}")
            if not self.cfg.get("gates", {}).get("after_plan", True) or DRY_RUN:
                return plan_ok
            self.board(comment=f"plan ready: waiting for your approval ({self.plan})", unread=True)
            print(f"\nPlan ready: {self.plan}  (open it in your editor before you decide)")
            choice, extra = ask_human(
                "Continue to implementation? 'y' = continue, 'r <notes>' = revise, 'q' = quit",
                ["y", "r", "q"])
            if choice == "y":
                return True
            if choice == "q":
                return False
            notes = extra or ask_human("Revision notes", ["r"])[1]
        return False

    def stage_implement(self):
        self.board("in_progress", "implementing")
        d = self.start("implement", "implement", self.implement_spec())
        outcome = self.wait_all([d])[d]
        self.last_impl_report = self.reports.get(d, "")
        if outcome == "succeeded":
            return True
        log(f"implement outcome={outcome}")
        if self.cfg.get("gates", {}).get("after_failure", True) and not DRY_RUN:
            self.board(comment=f"implement {outcome}: waiting for your decision", unread=True)
            choice, _ = ask_human("Implement reported failure. Review anyway? 'y' or 'q'",
                                  ["y", "q"])
            return choice == "y"
        return False

    def stage_review(self):
        e2e_on = self.args.e2e or self.cfg.get("e2e", {}).get("enabled", False)
        max_rounds = self.cfg.get("max_fix_rounds", 2)
        for round_no in range(max_rounds + 1):
            self.board("in_review", f"review round {round_no + 1}" + (" + e2e" if e2e_on else ""))
            dispatches = {self.start("review", f"review-{round_no}", self.review_spec()): self.review}
            if e2e_on:
                dispatches[self.start("e2e", f"e2e-{round_no}", self.e2e_spec())] = self.e2e_report
            results = self.wait_all(list(dispatches))
            failed = [dispatches[d] for d, o in results.items() if o != "succeeded"]
            if not failed:
                self.board("done", f"review PASS: check git diff, then commit ({self.review})",
                           unread=True)
                log(f"DONE: review PASS. Plan {self.plan}, review {self.review}. "
                    f"Check git diff, run the tests yourself, then commit.")
                return True
            log(f"review FAIL ({', '.join(failed)}), round {round_no + 1} of {max_rounds + 1}")
            if round_no == max_rounds:
                break
            if self.cfg.get("gates", {}).get("before_fix", False) and not DRY_RUN:
                self.board(comment="review FAIL: approve the fix round?", unread=True)
                choice, _ = ask_human("Send a fix round to the implementer? 'y' or 'q'", ["y", "q"])
                if choice == "q":
                    return False
            self.board(comment=f"review FAIL: fix round {round_no + 1}")
            d = self.start("implement", f"fix-{round_no + 1}", self.fix_spec(failed))
            self.wait_all([d])
            self.last_impl_report = self.reports.get(d, "") or self.last_impl_report
        self.board(comment=f"STOP: review did not pass, read {self.review}", unread=True)
        log(f"STOP: review did not pass. Read {self.review} and decide by hand.")
        return False

    def stage_ship(self):
        """Ask the user, then let the ship agent commit, push, and open the PR."""
        ship = self.cfg.get("ship", {})
        if not ship.get("enabled", False):
            return True
        follow = ship.get("follow_review", False)
        if not DRY_RUN:
            changed, _ = self.run_paths()
            self.board(comment="review PASS: ship it?", unread=True)
            listed = ", ".join(changed) if changed is not None else "unknown (no baseline)"
            print(f"\nFiles this run changed: {listed}")
            choice, _ = ask_human(
                f"Ship with {', '.join(ship.get('skills', [])) or 'no skill'} ({self.agents['ship']})? "
                f"'y' = commit, push, open PR; 'w' = also follow the PR review; 'n' = stop here",
                ["y", "w", "n"])
            if choice != "n":
                # Pushing is outward-facing: a second, typed confirmation guards against a
                # stray or mistyped answer.
                # Keyboard only: the answer file cannot approve a push.
                confirm = read_line(">>> Type 'push' to commit, push, and open the PR now "
                                    "(anything else cancels): ", allow_file=False).strip()
                if confirm != "push":
                    choice = "n"
            if choice == "n":
                self.board(comment="review PASS: not shipped, commit by hand")
                log("ship skipped by user")
                return True
            follow = choice == "w"
        self.board(comment="shipping: commit, push, PR" + (" + review" if follow else ""))
        d = self.start("ship", "ship", self.ship_spec(follow))
        timeout = ship.get("timeout_min", 90 if follow else 30) * 60
        outcome = self.wait_all([d], timeout)[d]
        first_line = (self.reports.get(d) or "").strip().splitlines()[:1]
        pr = re.search(r"https://\S+/pull/\d+", first_line[0]) if first_line else None
        if outcome == "succeeded":
            self.board(comment=f"shipped: {pr.group(0) if pr else 'PR opened, see log'}",
                       unread=True)
            log(f"SHIPPED: {pr.group(0) if pr else 'see the ship report above'}")
            return True
        self.board(comment=f"ship {outcome}: check git status and the Codex tab", unread=True)
        log(f"STOP: ship outcome={outcome}. Check git status, git log, and the ship worker tab.")
        return False

    def run(self):
        stages = ["plan", "implement", "review", "ship"]
        start = stages.index(self.args.start_at)
        if not DRY_RUN and start <= 1:
            self.save_baseline()
        if not DRY_RUN:
            if self.args.resume_run:
                self.run_id = self.args.resume_run
            else:
                created = orca("orchestration", "run-create", "--objective",
                               f"{self.cfg.get('project', 'project')}: {self.args.feature}",
                               "--from", self.me)["result"]
                self.run_id = find_key(created, "runId") or find_key(created, "id")
            log(f"run={self.run_id} mode={self.args.mode} plan={self.plan}")
            base, note = resolve_base(self.cfg)
            log(f"base branch: {base}" + (f" ({note})" if note else ""))
        if start <= 0 and not self.stage_plan():
            self.board(comment="STOP: stopped at plan stage", unread=True)
            log("STOP: stopped at plan gate")
            return 1
        if start <= 1 and not self.stage_implement():
            self.board(comment="STOP: implement did not succeed", unread=True)
            log("STOP: implement stage did not succeed")
            return 1
        if start <= 2 and not self.stage_review():
            return 1
        return 0 if self.stage_ship() else 1


def main():
    global LOG_PATH, DRY_RUN, ANSWER_FILE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="per-project JSON config")
    ap.add_argument("--feature", required=True, help="feature request in plain words")
    ap.add_argument("--slug", required=True, help="short kebab-case name, e.g. quotation-discount")
    ap.add_argument("--mode", default="small", help="a key under 'modes' in the config")
    ap.add_argument("--start-at", choices=["plan", "implement", "review", "ship"], default="plan")
    ap.add_argument("--known-failures", help="overrides known_failures from the config")
    ap.add_argument("--resume-run", help="reuse a Run already bound to this terminal")
    ap.add_argument("--e2e", action="store_true", help="run the E2E worker for this run")
    ap.add_argument("--module", help="comma-separated keys of e2e.modules to test, e.g. admin,reports")
    ap.add_argument("--dry-run", action="store_true", help="print specs, start no workers")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        sys.exit("--slug must be kebab-case: lowercase letters, digits, hyphens")
    with open(args.config) as f:
        cfg = json.load(f)
    if args.mode not in cfg.get("modes", {}):
        sys.exit(f"--mode must be one of: {', '.join(cfg.get('modes', {}))}")
    DRY_RUN = args.dry_run
    if not DRY_RUN and not os.environ.get("ORCA_TERMINAL_HANDLE"):
        sys.exit("Run this inside an Orca terminal (ORCA_TERMINAL_HANDLE is missing).")
    if not DRY_RUN:
        os.makedirs(".orca-pipeline", exist_ok=True)
        LOG_PATH = f".orca-pipeline/{args.slug}.log"
        ANSWER_FILE = os.path.abspath(f".orca-pipeline/{args.slug}.answer")
    sys.exit(Pipeline(cfg, args).run())


if __name__ == "__main__":
    main()
