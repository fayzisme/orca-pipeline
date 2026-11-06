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

IMPLEMENT_AUTONOMY = (
    "The user already approved the plan. Do not look for other plans and do not ask for "
    "confirmation: if a skill says to confirm the plan first, treat it as confirmed and start "
    "implementing. For a decision that only the user can make, use the orchestration ask command "
    "and wait for the answer; never end your task with a request for approval. If a step or check "
    "needs something this environment does not have (a database tool, an MCP server, a command "
    "that is not installed), do not stop: do everything else, and list each skipped item under "
    "'Not verified' in your report. Never claim a check you did not run.")

EVIDENCE_RULE = (
    "Report: in your worker report, list each test command you ran with its quoted runner "
    "summary line (for example 'Tests: 15, Assertions: 15, Failures: 3.'). For a bug fix, also "
    "run the new test once before the fix and quote that failing summary. Keep the report under "
    "3000 characters.")

HERE = os.path.dirname(os.path.abspath(__file__))

# Which agent runs each stage when the config does not say. "review" may also be a list of
# agents, which then review in parallel; every other stage takes exactly one agent.
DEFAULT_AGENTS = {"plan": "claude", "implement": "opencode", "review": "codex",
                  "e2e": "codex", "ship": "codex"}
SINGLE_AGENT_STAGES = ("plan", "implement", "e2e", "ship")

# For an agent without a profile, or a profile without a skill_phrase.
GENERIC_SKILL_PHRASE = ("Use these skills before you start, if your tool supports skills: "
                        "{skills}.")


def load_agent_profiles(cfg=None):
    """Agent profiles: agents.json, then agents.local.json, then the config's agent_profiles.

    A profile describes how to start an agent and how it finds skills:
      orca_id        id for `orca orchestration worker-start --agent` (default: profile name)
      model, effort  optional --model / --effort for that launch
      cli            command that must be installed (checked by validate_config.py)
      skill_phrase   sentence that asks the agent to load skills ({skills}, {dollar_skills})
      skill_dirs     folders holding <skill>/SKILL.md; "~" and "{project}" are expanded
      plugin_skills  true when "<plugin>:<skill>" names resolve to installed plugins
      extends        another profile to start from, e.g. an alias "claude-opus" that
                     extends "claude" and only sets "model"
    Later sources override earlier ones key by key.
    """
    raw = {}
    for path in (os.path.join(HERE, "agents.json"), os.path.join(HERE, "agents.local.json")):
        if os.path.exists(path):
            with open(path) as f:
                for name, profile in json.load(f).get("agents", {}).items():
                    raw[name] = {**raw.get(name, {}), **profile}
    for name, profile in ((cfg or {}).get("agent_profiles") or {}).items():
        raw[name] = {**raw.get(name, {}), **profile}

    def resolve(name, seen=()):
        profile = raw[name]
        parent = profile.get("extends")
        if not parent or parent not in raw or parent in seen:
            return dict(profile)
        merged = {**resolve(parent, seen + (name,)), **profile}
        merged.setdefault("orca_id", parent)  # an alias launches its parent's agent
        return merged

    return {name: resolve(name) for name in raw}


def stage_agents(cfg):
    """{stage: agent} from the config over DEFAULT_AGENTS; "review" is always a list."""
    agents = {**DEFAULT_AGENTS, **cfg.get("agents", {})}
    reviewers = agents["review"] if isinstance(agents["review"], list) else [agents["review"]]
    agents["review"] = list(dict.fromkeys(reviewers))  # keep order, drop repeats
    return agents


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
    template = (cfg.get("skill_phrases", {}).get(agent)
                or load_agent_profiles(cfg).get(agent, {}).get("skill_phrase")
                or GENERIC_SKILL_PHRASE)
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


def interactive():
    """True when a person is at the terminal to answer a question."""
    return sys.stdin.isatty()


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
        self.agents = stage_agents(cfg)
        for stage in SINGLE_AGENT_STAGES:
            if not isinstance(self.agents[stage], str):
                sys.exit(f"agents.{stage} must name one agent; only agents.review takes a list")
        self.profiles = load_agent_profiles(cfg)
        reviewers = self.agents["review"]
        review_dir = cfg.get("review_dir", "docs/reviews")
        # One reviewer keeps <slug>.md; several each get <slug>-<agent>.md.
        self.reviews = {a: self.review if len(reviewers) == 1 else f"{review_dir}/{self.slug}-{a}.md"
                        for a in reviewers}
        self.timeout = cfg.get("stage_timeout_min", 45) * 60
        self.baseline_path = f".orca-pipeline/{self.slug}.baseline.json"
        self.reports = {}  # dispatch -> body of its worker_done message
        self.last_impl_report = ""  # handed to the next review as evidence
        self.titles = {}   # dispatch -> "title (agent)", for messages to the user
        self.blocked = {}  # dispatch -> when the user was last told it waits at a prompt
        known = args.known_failures or cfg.get("known_failures") or "none"
        self.base_rules = (
            "No human is watching your terminal: do not wait for approval and do not open "
            "interactive prompts; for a blocking question use the orchestration ask command from "
            "your preamble. Never write real credentials, IP addresses, or personal data into "
            f"files. Tests that already fail before this change (out of scope): {known}.")
        self.common = f"Do not commit, push, or switch branches. {self.base_rules}"

    # ---- orca plumbing -------------------------------------------------
    def start(self, stage, title, spec, agent=None):
        """Start one worker. `agent` names a profile; it defaults to the stage's agent."""
        agent = agent or self.agents[stage]
        profile = self.profiles.get(agent, {})
        orca_id = profile.get("orca_id", agent)
        launch = ["--agent", orca_id]
        for flag in ("model", "effort"):
            if profile.get(flag):
                launch += [f"--{flag}", str(profile[flag])]
        label = agent if orca_id == agent else f"{agent} = {' '.join(launch[1:])}"
        if DRY_RUN:
            print(f"\n===== {title} ({label}) =====\n{spec}\n")
            return f"dry-{title}"
        data = orca("orchestration", "worker-start", "--spec", spec, "--task-title", title,
                    "--worktree", "current", *launch, "--run", self.run_id, "--from", self.me)
        dispatch = find_key(data.get("result") or {}, "dispatchId")
        log(f"started {label} for '{title}': dispatch={dispatch}")
        self.titles[dispatch] = f"{title} ({label})"
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
        """Wait until every dispatch in `dispatches` reports. Returns {dispatch: outcome}.

        A worker stopped at a prompt for a person (an approval or a question) has neither
        finished nor failed: the coordinator says where it waits and keeps waiting. When the
        time limit passes it asks whether to wait longer; "timeout" comes back only when you
        stop waiting, or when nobody is at the terminal to ask.
        """
        if DRY_RUN:
            return {d: "succeeded" for d in dispatches}
        pending = set(d for d in dispatches if d)
        results = {}
        limit = timeout or self.timeout
        deadline = time.time() + limit
        ack = None  # acknowledged on the next check, which also returns the next batch
        while pending:
            if time.time() >= deadline:
                if not self.keep_waiting(pending, limit):
                    break
                deadline = time.time() + limit
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
                payload = parse_payload(m)
                dispatch = payload.get("dispatchId")
                if dispatch not in pending:
                    log(f"ignoring {mtype} for dispatch {dispatch}")
                    continue
                if mtype == "escalation" and payload.get("stopReason") == "needs-input":
                    self.note_blocked(dispatch)  # the dispatch is still open: not a result
                    continue
                outcome = payload.get("outcome") or (
                    "failed" if mtype == "escalation" else "unknown")
                results[dispatch] = outcome
                self.reports[dispatch] = str(m.get("body") or "")
                pending.discard(dispatch)
                self.blocked.pop(dispatch, None)
                log(f"{mtype} {dispatch}: outcome={outcome} | {str(m.get('body'))[:2000]}")
            if not messages:
                stuck = len(pending & set(self.blocked))
                log(f"still waiting on {len(pending)} worker(s)"
                    + (f" ({stuck} at a prompt that needs you)" if stuck else "") + "...")
        if ack:
            orca("orchestration", "check", "--run", self.run_id, "--ack", ack, check=False)
        for d in pending:
            results[d] = "timeout"
        for d in dispatches:
            if d and results.get(d) != "timeout":  # a worker that still runs cannot be released
                orca("orchestration", "worker-release", "--dispatch", d, check=False)
        return results

    def note_blocked(self, dispatch):
        """A worker stopped at a prompt that needs a person. Say so, at most every 5 minutes."""
        now = time.time()
        if now - self.blocked.get(dispatch, 0) < 300:
            return
        self.blocked[dispatch] = now
        title = self.titles.get(dispatch, dispatch)
        log(f"{title} stopped at a prompt that needs a person (dispatch {dispatch})")
        self.board(comment=f"{title} waits for you in its tab", unread=True)
        print(f"\n>>> {title} stopped at a prompt that needs you (an approval or a question).\n"
              f"    Open its tab in Orca and answer there. The work is not finished, and the\n"
              f"    coordinator keeps waiting. To see what it shows:\n"
              f"      {ORCA} orchestration worker-read --dispatch {dispatch}", flush=True)

    def keep_waiting(self, pending, limit):
        """The time limit passed and workers still run. True when the user wants to wait more."""
        names = ", ".join(self.titles.get(d, d) for d in sorted(pending))
        minutes = max(1, int(limit // 60))
        log(f"time limit of {minutes} min passed; still running: {names}")
        if not (self.cfg.get("gates", {}).get("on_timeout", True) and interactive()):
            return False
        self.board(comment=f"still running after {minutes} min: keep waiting?", unread=True)
        print(f"\nStill running after {minutes} minutes: {names}")
        choice, _ = ask_human(f"Keep waiting {minutes} more minutes? 'w' = keep waiting, "
                              f"'q' = stop waiting", ["w", "q"])
        return choice == "w"

    def stop_worker(self, dispatch):
        """Cancel a worker that still runs, so two workers never write the same files."""
        if dispatch and not DRY_RUN:
            data = orca("orchestration", "worker-stop", "--dispatch", dispatch, check=False)
            log(f"stopped {self.titles.get(dispatch, dispatch)}: ok={data.get('ok')}")


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
            f"{self.agents['implement']} implements the plan and {', '.join(self.agents['review'])} "
            f"review it. They may not have the tools you have, such as MCP servers (database "
            f"tools, for example) or helper commands. Write tasks and acceptance criteria they can "
            f"carry out with files, the shell, and the test command above. Put anything that needs "
            f"other tools, such as checks against a real database or updates of a notes tool, under "
            f"a heading 'Manual checks for the user'; those are not acceptance criteria.\n"
            f"Constraints: do NOT write application code. {self.rules('plan')} {self.common}\n"
            f"Ownership: only {self.plan} and planning artifacts the skills create.\n"
            f"Observable acceptance: {self.plan} exists and lists acceptance criteria.")

    def implement_spec(self, notes=""):
        return (
            f"Target: this repository. Read the project rules file first.\n"
            f"Change: implement {self.plan} exactly, including its tests.\n"
            f"{skill_line(self.cfg, self.agents['implement'], self.mode.get('implement', []))}\n"
            f"Run tests with: {self.test_command('implement')}\n"
            f"Constraints: change only files the plan names. Documentation: edit only docs the plan "
            f"names, only the lines the change needs, and write those lines in English. "
            f"{self.rules('implement')} {self.common}\n"
            f"Ownership: source, test, and doc files named in the plan.\n"
            f"{IMPLEMENT_AUTONOMY}\n"
            + (f"Notes from the user for this attempt: {notes}\n" if notes else "") +
            f"{EVIDENCE_RULE}\n"
            f"Observable acceptance: the plan's tests pass and every acceptance criterion is met.")

    def review_spec(self, agent=None):
        agent = agent or self.agents["review"][0]
        report = self.reviews[agent]
        others = [a for a in self.agents["review"] if a != agent]
        parallel = (f"Other reviewers ({', '.join(others)}) review the same change at the same "
                    f"time and write their own reports; judge independently and do not read or "
                    f"edit their reports.\n" if others else "")
        return (
            f"Target: the uncommitted changes in this repository (git diff and untracked files) "
            f"against {self.plan}.\n"
            f"{skill_line(self.cfg, agent, self.mode.get('review', []))}\n"
            f"Run tests with: {self.test_command('review')}\n"
            f"Write {report}, replacing any earlier report: verdict PASS/FAIL, test summary, "
            f"blocking issues with file:line and a concrete fix, non-blocking follow-ups.\n"
            f"{parallel}"
            f"Review scope: judge only lines added or changed in the diff. Pre-existing problems are "
            f"non-blocking follow-ups. Blocking issues are limited to functional bugs, failing tests "
            f"outside the known failures, unmet acceptance criteria, and problems introduced by "
            f"changed lines.\n"
            f"Constraints: do NOT edit any file except {report}. {self.rules('review')} "
            f"{self.common}\n"
            f"{self.implementer_evidence()}"
            f"Observable acceptance: {report} exists. Report --outcome succeeded only when the "
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
            f"{IMPLEMENT_AUTONOMY}\n"
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
    def plan_stamp(self):
        """(mtime, size) of the plan file, or None when it does not exist."""
        try:
            st = os.stat(self.plan)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def plan_problem(self, outcome, before, dispatch):
        """Why the plan is not finished, or "" when it is.

        Finished means: the worker reported success, and the plan file exists, is not empty,
        and was written by this attempt (it differs from `before`, the stamp taken at the
        start), so a plan left from an earlier run does not count.
        """
        if DRY_RUN:
            return ""
        if outcome == "timeout":
            return "the plan worker did not report within the time limit"
        if outcome != "succeeded":
            return f"the plan worker reported '{outcome}'"
        after = self.plan_stamp()
        if after is None:
            return f"the worker reported done, but {self.plan} does not exist"
        if after == before:
            return f"the worker reported done, but it did not change {self.plan}"
        if after[1] == 0:
            return f"the worker reported done, but {self.plan} is empty"
        return ""

    def stage_plan(self):
        notes = ""
        for attempt in range(1, 6):
            before = self.plan_stamp()
            self.board("in_progress", f"planning (attempt {attempt})")
            d = self.start("plan", f"plan-{attempt}", self.plan_spec(notes))
            outcome = self.wait_all([d])[d]
            problem = self.plan_problem(outcome, before, d)
            gate_on = self.cfg.get("gates", {}).get("after_plan", True)
            if problem:
                # Never offer "continue to implementation" without a finished plan.
                log(f"plan not finished: {problem}")
                if not gate_on:
                    return False
                self.board(comment="plan not finished: see the coordinator tab", unread=True)
                print(f"\nPlan not finished: {problem}.\n"
                      f"There is nothing to approve yet, so the pipeline does not go on.")
                self.say_report(d)
                choice, extra = ask_human(
                    "Start the plan worker again? 'r' = start again ('r <notes>' adds notes), "
                    "'q' = quit", ["r", "q"])
                if choice == "q":
                    if outcome == "timeout":
                        print(f"{self.titles.get(d, d)} is still open in its tab.")
                    return False
                if outcome == "timeout":
                    self.stop_worker(d)  # never two plan workers writing the same file
                notes = extra
                continue
            if not gate_on or DRY_RUN:
                return True
            with open(self.plan, errors="replace") as f:
                size = sum(1 for _ in f)
            self.board(comment=f"plan ready: waiting for your approval ({self.plan})", unread=True)
            print(f"\nPlan ready: {self.plan} ({size} lines; open it in your editor before you "
                  f"decide)")
            choice, extra = ask_human(
                "Continue to implementation? 'y' = continue, 'r <notes>' = revise, 'q' = quit",
                ["y", "r", "q"])
            if choice == "y":
                return True
            if choice == "q":
                return False
            notes = extra or ask_human("Revision notes", ["r"])[1]
        return False

    def say_report(self, dispatch, limit=1500):
        """Show what a worker said when it stopped, so the user can decide with the facts."""
        text = (self.reports.get(dispatch) or "").strip()
        if text:
            shown = text if len(text) <= limit else text[:limit].rstrip() + " ..."
            print("The worker said:\n" + "\n".join("  " + line for line in shown.splitlines()))

    def tree_stamp(self):
        """{path: (mtime, size)} of every changed or untracked file, not counting plans, reviews,
        and pipeline files. Two stamps show what a worker changed, even in a file that was
        already modified before the run."""
        skip = (self.cfg.get("plan_dir", "docs/plans").rstrip("/") + "/",
                self.cfg.get("review_dir", "docs/reviews").rstrip("/") + "/", ".orca-pipeline/")
        stamp = {}
        for path in self.dirty_paths():
            if path.startswith(skip):
                continue
            try:
                st = os.stat(path)
                stamp[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp[path] = None  # deleted
        return stamp

    @staticmethod
    def changed_between(before, after):
        missing = object()
        return sorted(p for p in set(before) | set(after)
                      if before.get(p, missing) != after.get(p, missing))

    def stage_implement(self):
        gate_on = self.cfg.get("gates", {}).get("after_failure", True)
        notes = ""
        for attempt in range(1, 4):
            title = "implement" if attempt == 1 else f"implement-{attempt}"
            self.board("in_progress", "implementing" + (f" (attempt {attempt})" if attempt > 1 else ""))
            before = None if DRY_RUN else self.tree_stamp()
            d = self.start("implement", title, self.implement_spec(notes))
            outcome = self.wait_all([d])[d]
            self.last_impl_report = self.reports.get(d, "")
            if DRY_RUN:
                return True
            changed = self.changed_between(before, self.tree_stamp())
            if outcome == "succeeded" and changed:
                return True
            if outcome == "timeout":
                self.stop_worker(d)  # do not review while the implementer still writes files
            what = ("reported success but changed no files" if outcome == "succeeded" else
                    "did not finish in time" if outcome == "timeout" else f"reported '{outcome}'")
            log(f"implement {what}; files changed: {len(changed)}")
            if not gate_on:
                return outcome == "succeeded"
            self.board(comment=f"implement {what}: waiting for your decision", unread=True)
            print(f"\nThe implement worker {what}.")
            self.say_report(d)
            if changed:
                print(f"{len(changed)} file(s) changed in this attempt, for example: "
                      f"{', '.join(changed[:5])}")
                choice, extra = ask_human(
                    "Review anyway? 'y' = review what is there, 'r' = start the implement worker "
                    "again ('r <guidance>' adds guidance), 'q' = quit", ["y", "r", "q"])
            else:
                print("No files were changed, so there is nothing to review yet. If the plan is "
                      "already implemented, quit and run again with --start-at review.")
                choice, extra = ask_human(
                    "Start the implement worker again? 'r' = start again ('r <answer or guidance>' "
                    "is passed to it), 'q' = quit", ["r", "q"])
            if choice == "y":
                return True
            if choice == "q":
                return False
            notes = extra
        return False

    def stage_review(self):
        e2e_on = self.args.e2e or self.cfg.get("e2e", {}).get("enabled", False)
        max_rounds = self.cfg.get("max_fix_rounds", 2)
        reviewers = self.agents["review"]
        reports = ", ".join(self.reviews.values())
        for round_no in range(max_rounds + 1):
            self.board("in_review", f"review round {round_no + 1}"
                       + (f" ({len(reviewers)} reviewers)" if len(reviewers) > 1 else "")
                       + (" + e2e" if e2e_on else ""))
            # Every reviewer, and the E2E worker, runs in parallel; all must pass.
            dispatches = {}
            for agent in reviewers:
                title = f"review-{round_no}" + (f"-{agent}" if len(reviewers) > 1 else "")
                dispatches[self.start("review", title, self.review_spec(agent), agent)] = \
                    self.reviews[agent]
            if e2e_on:
                dispatches[self.start("e2e", f"e2e-{round_no}", self.e2e_spec())] = self.e2e_report
            results = self.wait_all(list(dispatches))
            failed = [dispatches[d] for d, o in results.items() if o != "succeeded"]
            if not failed:
                self.board("done", f"review PASS: check git diff, then commit ({reports})",
                           unread=True)
                log(f"DONE: review PASS. Plan {self.plan}, review {reports}. "
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
        self.board(comment=f"STOP: review did not pass, read {reports}", unread=True)
        log(f"STOP: review did not pass. Read {reports} and decide by hand.")
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
