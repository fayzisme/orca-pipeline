#!/usr/bin/env python3
"""Onboard local projects to the Orca pipeline: write projects/<name>.json for each one.

  python3 orca_init.py <project path> [<project path> ...]
  python3 orca_init.py <project path> --name <config name>
  python3 orca_init.py --all [--root ~/Sites/app] [--root ~/code ...]
  options: --force  --dry-run  --no-register  --budget-usd 5  --model <id>

Works for any git checkout on this Mac, inside or outside ~/Sites, with or without Docker.

Per project:
  1. registers the folder in Orca (unless --no-register),
  2. adds .orca-pipeline/ to the repository's git info/exclude,
  3. collects facts it can check itself (branches, stack, runtime: the container that mounts
     the folder, or else the host's PHP/Node/Python and version pin files; test runner
     versions, local hostname or dev-server scripts, vault project, visible skills),
  4. runs Claude Code headless (claude -p) in the project folder with init-prompt.md. Claude
     may only read the project and write projects/<name>.json and
     projects/<name>.init-report.md,
  5. validates the result with validate_config.py and an orca_pipeline.py dry run.

A project that already has projects/<name>.json is skipped unless --force (the old config is
kept as <name>.json.bak). Two folders with the same name: the second one fails until you
give it its own --name. --dry-run prints the facts and the Claude command, changes nothing.
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orca_pipeline  # noqa: E402
import validate_config  # noqa: E402

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HERE, "projects")
# The reference config Claude shapes new configs after. A local, git-ignored
# local-reference.json (for example a copy of your best project config) wins over the
# generic example that ships with the repository.
LOCAL_REFERENCE = os.path.join(HERE, "local-reference.json")
EXAMPLE = LOCAL_REFERENCE if os.path.exists(LOCAL_REFERENCE) else \
    os.path.join(HERE, "examples", "laravel-docker.example.json")
GUIDE = os.path.join(HERE, "README.md")
PROMPT = os.path.join(HERE, "init-prompt.md")
ORCA = os.environ.get("ORCA_CLI_COMMAND", "orca")
VAULT = os.path.expanduser(os.environ.get("OBS_VAULT", "~/Documents/Obsidian Vault"))
SECRET_IN_URL = re.compile(r"//[^/@\s]+@")


def run(cmd, cwd=None, timeout=30):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}"


HEARTBEAT_SECONDS = 30  # without a terminal, a line this often keeps a run from looking stuck


def elapsed_text(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def wait_with_progress(proc, label):
    """Wait for a process and show that it is still running. Returns its exit code.

    In a terminal: one live line with a spinner and the elapsed time, replaced by a
    "finished" line at the end. Without a terminal (a pipe or a log): one line every
    HEARTBEAT_SECONDS. Ctrl+C stops the process and raises KeyboardInterrupt.
    """
    start = last_beat = time.time()
    tty = sys.stdout.isatty()
    utf8 = (sys.stdout.encoding or "").lower().startswith("utf")
    frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f" if utf8 else "|/-\\"
    width = shutil.get_terminal_size((100, 24)).columns
    tick = 0
    try:
        while proc.poll() is None:
            now = time.time()
            stamp = elapsed_text(now - start)
            if tty:
                line = f"  {frames[tick % len(frames)]} {label} {stamp}  (not finished, Ctrl+C cancels)"
                print("\r" + line[:width - 1] + "\033[K", end="", flush=True)
            elif now - last_beat >= HEARTBEAT_SECONDS:
                print(f"  ... {label}, still running ({stamp})", flush=True)
                last_beat = now
            tick += 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(("\r\033[K" if tty else "") + f"  interrupted after {elapsed_text(time.time() - start)}; "
              "the process was stopped.", flush=True)
        raise
    done = f"{label}: finished in {elapsed_text(time.time() - start)} (exit {proc.returncode})"
    print(("\r\033[K" if tty else "") + "  " + done, flush=True)
    return proc.returncode


def tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


def rule_path(path):
    """Claude Code permission rules: ~/ for home paths, // for other absolute paths."""
    return tilde(path) if path.startswith(HOME + "/") else "/" + path


def slug(name):
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")


# ---- facts ---------------------------------------------------------------
def git_facts(path):
    _, current = run(["git", "-C", path, "branch", "--show-current"])
    _, remote = run(["git", "-C", path, "remote", "get-url", "origin"])
    _, head = run(["git", "-C", path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    _, refs = run(["git", "-C", path, "for-each-ref", "--format=%(refname:short)",
                   "refs/remotes/origin"])
    names = [r.split("/", 1)[1] for r in refs.splitlines() if "/" in r]
    wanted = ("staging", "master", "main", "dev", "develop", "development")
    remote = SECRET_IN_URL.sub("//", remote)  # never pass embedded credentials on
    host = re.search(r"(?:@|//)([^/:]+)", remote)
    return {
        "current_branch": current,
        "origin": remote,
        "origin_host": host.group(1) if host else "",
        "origin_default_branch": head.split("/", 1)[-1] if head else "",
        "integration_branches_on_origin": [n for n in wanted if n in names],
    }


def stack_facts(path):
    facts = {"bmad_present": os.path.isdir(f"{path}/_bmad"),
             "docs": [f for f in ("CLAUDE.md", "AGENTS.md", "README.md", "docs")
                      if os.path.exists(f"{path}/{f}")]}
    try:
        with open(f"{path}/composer.json") as f:
            composer = json.load(f)
        req = {**composer.get("require", {}), **composer.get("require-dev", {})}
        facts["composer"] = {k: v for k, v in req.items()
                             if k in ("php", "laravel/framework", "phpunit/phpunit")}
    except (OSError, ValueError):
        pass
    try:
        with open(f"{path}/package.json") as f:
            pkg = json.load(f)
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        facts["package_json"] = {
            "test_script": pkg.get("scripts", {}).get("test", ""),
            "frameworks": {k: v for k, v in deps.items() if k in (
                "vue", "react", "next", "jquery", "jest", "vitest", "mocha", "cypress",
                "@playwright/test")}}
    except (OSError, ValueError):
        pass
    facts["test_config"] = [f for f in ("phpunit.xml", "phpunit.xml.dist", "pytest.ini",
                                        "jest.config.js", "vitest.config.js", "tests", "test")
                            if os.path.exists(f"{path}/{f}")]
    return facts


def first_line(cmd, cwd=None, timeout=30):
    code, out = run(cmd, cwd=cwd, timeout=timeout)
    return out.splitlines()[0] if code == 0 and out else None


def container_facts(path):
    """The running container that mounts this folder, plus runtime versions inside it."""
    code, ids = run(["docker", "ps", "-q"])
    if code != 0 or not ids:
        return None
    code, out = run(["docker", "inspect", "--format",
                     "{{.Name}}|{{range .Mounts}}{{.Source}}=>{{.Destination}};{{end}}",
                     *ids.split()])
    for line in out.splitlines():
        name, _, mounts = line.partition("|")
        for m in mounts.split(";"):
            src, _, dst = m.partition("=>")
            if src.replace("/host_mnt", "", 1).rstrip("/") == path.rstrip("/"):
                c = name.lstrip("/")
                facts = {"where": "docker", "container": c, "mount_destination": dst}
                php = first_line(["docker", "exec", c, "php", "-r", "echo PHP_VERSION;"])
                facts["php_version"] = php if re.fullmatch(r"[\d.]+", php or "") else None
                if os.path.exists(f"{path}/vendor/bin/phpunit"):
                    facts["phpunit_version"] = first_line(
                        ["docker", "exec", "-w", dst, c, "vendor/bin/phpunit", "--version"],
                        timeout=60)
                return facts
    return None


def host_facts(path):
    """Runtime on this Mac, for projects that no running container mounts."""
    facts = {"where": "host"}
    pins = {}
    for f in (".nvmrc", ".node-version", ".php-version", ".python-version", ".tool-versions"):
        try:
            with open(f"{path}/{f}") as fh:
                pins[f] = fh.read().strip()[:80]
        except OSError:
            pass
    if pins:
        facts["version_pin_files"] = pins
    plain_php = glob.glob(f"{path}/*.php") or glob.glob(f"{path}/public/*.php")
    if os.path.exists(f"{path}/composer.json") or plain_php:
        facts["php_version"] = first_line(["php", "-r", "echo PHP_VERSION;"])
        facts["composer_json"] = os.path.exists(f"{path}/composer.json")
        facts["vendor_installed"] = os.path.isdir(f"{path}/vendor")
        if os.path.exists(f"{path}/vendor/bin/phpunit"):
            facts["phpunit_version"] = first_line(["php", "vendor/bin/phpunit", "--version"],
                                                  cwd=path, timeout=60)
    if os.path.exists(f"{path}/package.json"):
        facts["node_version"] = first_line(["node", "-v"])
        facts["node_modules_installed"] = os.path.isdir(f"{path}/node_modules")
        facts["package_manager"] = next(
            (pm for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                                 ("bun.lockb", "bun"), ("package-lock.json", "npm"))
             if os.path.exists(f"{path}/{lock}")), "npm")
    if any(os.path.exists(f"{path}/{f}") for f in ("pyproject.toml", "requirements.txt",
                                                  "setup.py")):
        venv = next((v for v in (".venv", "venv") if os.path.exists(f"{path}/{v}/bin/python")),
                    None)
        facts["python_venv"] = venv
        facts["python_version"] = first_line(
            [f"{path}/{venv}/bin/python" if venv else "python3", "--version"])
    compose = [f for f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                           "compose.yaml", "Dockerfile") if os.path.exists(f"{path}/{f}")]
    if compose:
        facts["docker_files_but_no_running_container"] = compose
    return facts


def runtime_facts(path):
    return container_facts(path) or host_facts(path)


def dev_server_facts(path):
    """Hints for the local URL of apps that are not served as <name>.local."""
    hints = {}
    try:
        with open(f"{path}/package.json") as f:
            scripts = json.load(f).get("scripts", {})
        hints["package_scripts"] = {k: v[:120] for k, v in scripts.items()
                                    if k in ("dev", "start", "serve", "preview", "test", "e2e")}
    except (OSError, ValueError):
        pass
    if os.path.exists(f"{path}/artisan"):
        hints["artisan"] = "php artisan serve is available (default http://127.0.0.1:8000)"
    return hints


def hostname_facts(name):
    try:
        with open("/etc/hosts") as f:
            hosts = {h for line in f if not line.lstrip().startswith("#")
                     for h in line.split()[1:]}
    except OSError:
        hosts = set()
    candidates = {f"{name}.local", f"{name.replace('_', '-')}.local"}
    return {"local_hosts_in_etc_hosts": sorted(candidates & hosts)}


def agent_facts():
    """Every agent profile (agents.json, agents.local.json, the reference's agent_profiles),
    with whether its CLI is installed here."""
    with open(EXAMPLE) as f:
        profiles = orca_pipeline.load_agent_profiles(json.load(f))
    return {name: {"orca_id": p.get("orca_id", name), "model": p.get("model"),
                   "cli_installed": bool(p.get("cli") and shutil.which(p["cli"]))}
            for name, p in profiles.items()}


def skill_facts(path):
    """Visibility of every skill the reference config uses, for every agent profile."""
    with open(EXAMPLE) as f:
        example = json.load(f)
    names = {s for mode in example["modes"].values() for stage in mode.values() for s in stage}
    names |= set(example.get("e2e", {}).get("skills", []))
    names |= set(example.get("ship", {}).get("skills", []))
    names |= {"bmad-build", "implement-plan", "dev-workflow:dev-workflow", "nodejs-codestyle",
              "superpowers:systematic-debugging", "superpowers:requesting-code-review"}
    profiles = orca_pipeline.load_agent_profiles(example)
    return {agent: sorted(n for n in names
                          if validate_config.skill_visible(agent, n, path, profiles))
            for agent in profiles}


def vault_facts(path):
    """The vault project page for this folder. Pages under ~/ are named after the path
    (~/code/my_app -> code-my-app); others keep the folder name."""
    if not shutil.which("obs"):
        return {"vault_project": None, "note": "obs not installed"}
    base = os.path.basename(path)
    rel = os.path.relpath(path, HOME) if path.startswith(HOME + "/") else base
    candidates = [re.sub(r"[/_]+", "-", rel).lower(), base, base.lower(),
                  base.replace("_", "-").lower()]
    for cand in dict.fromkeys(candidates):
        if os.path.isfile(os.path.join(VAULT, "projects", f"{cand}.md")):
            _, out = run(["obs", "decisions", "--project", cand])
            count = len([l for l in out.splitlines() if l.strip().startswith(("-", "|", "20"))])
            return {"vault_project": cand, "vault_decision_lines": count}
    return {"vault_project": None, "note": "no vault project page for this folder"}


def collect_facts(path, name):
    return {"project": name, "path": tilde(path),
            "under_sites": path.startswith(f"{HOME}/Sites/"),
            "git": git_facts(path), "stack": stack_facts(path),
            "runtime": runtime_facts(path), "hosts": hostname_facts(name),
            "dev_server": dev_server_facts(path), "agents": agent_facts(),
            "skills_visible": skill_facts(path),
            "vault": vault_facts(path)}


# ---- steps ---------------------------------------------------------------
ORCA_DOWN = ("Orca is not running, or its runtime can't be reached, so the folder can't be added "
             "to it. Open the Orca app (or run: orca open), then run the same command again. "
             "To onboard without Orca, pass --no-register and add the folder later with: "
             "orca repo add --path <folder>")


def orca_down_message(detail=""):
    return ORCA_DOWN + (f" ({detail})" if detail else "")


def orca_reachable():
    """(True, "") unless Orca says it is down; then (False, why).

    Fail-open: an answer we don't recognise counts as up, and the next orca command
    reports its own error.
    """
    code, out = run([ORCA, "status", "--json"])
    try:
        data = json.loads(out)
    except ValueError:
        return False, f"could not run '{ORCA} status': {out[:100] or 'no output'}"
    result = data.get("result") or {}
    app = result.get("app") or {}
    runtime = result.get("runtime") or {}
    if not data.get("ok") or app.get("running") is False or runtime.get("reachable") is False:
        return False, ""
    return True, ""


def register(path, dry):
    """Add the folder to Orca. Returns (ok, message); the caller decides what a failure means."""
    up, detail = orca_reachable()
    if not up:
        if dry:
            return True, "would register in Orca (Orca is not running right now; open it before a real run)"
        return False, orca_down_message(detail)
    code, out = run([ORCA, "repo", "list", "--json"])
    try:
        data = json.loads(out)
        if not data.get("ok"):
            return False, f"could not list Orca projects: {json.dumps(data.get('error'))[:200]}"
        result = data.get("result", {})
        repos = result.get("repos", result) if isinstance(result, dict) else result
        if any(os.path.realpath(r.get("path", "")) == os.path.realpath(path) for r in repos):
            return True, "already registered in Orca"
    except (ValueError, AttributeError):
        return False, f"could not read the Orca project list: {out[:200]}"
    if dry:
        return True, "would register in Orca"
    code, out = run([ORCA, "repo", "add", "--path", path, "--json"])
    try:
        data = json.loads(out)
    except ValueError:
        data = {}
    if data.get("ok"):
        return True, "registered in Orca"
    return False, f"Orca registration failed: {json.dumps(data.get('error') or out)[:200]}"


def exclude_pipeline_dir(path, dry):
    _, rel = run(["git", "-C", path, "rev-parse", "--git-path", "info/exclude"])
    exclude = rel if os.path.isabs(rel) else os.path.join(path, rel)
    lines = open(exclude).read().splitlines() if os.path.exists(exclude) else []
    if ".orca-pipeline/" in lines:
        return ".orca-pipeline/ already excluded"
    if dry:
        return "would add .orca-pipeline/ to git info/exclude"
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a") as f:
        f.write(("\n" if lines and lines[-1] else "") + ".orca-pipeline/\n")
    return "added .orca-pipeline/ to git info/exclude"


def claude_command(path, name, facts, args):
    config = os.path.join(PROJECTS, f"{name}.json")
    report = os.path.join(PROJECTS, f"{name}.init-report.md")
    validate = (f"python3 {shlex.quote(os.path.join(HERE, 'validate_config.py'))} "
                f"{shlex.quote(config)}")
    with open(PROMPT) as f:
        prompt = f.read()
    for key, value in {"@PROJECT@": name, "@CONFIG_OUT@": config, "@REPORT_OUT@": report,
                       "@EXAMPLE@": EXAMPLE, "@GUIDE@": GUIDE, "@PATH_TILDE@": tilde(path),
                       "@VALIDATE@": validate,
                       "@FACTS@": json.dumps(facts, indent=1)}.items():
        prompt = prompt.replace(key, value)
    allowed = ["Read", "Glob", "Grep",
               f"Write({rule_path(config)})", f"Edit({rule_path(config)})",
               f"Write({rule_path(report)})", f"Edit({rule_path(report)})",
               f"Bash({validate}:*)", f"Bash({validate})",
               "Bash(obs decisions:*)", "Bash(obs get:*)", "Bash(obs search:*)",
               "Bash(gq:*)", "Bash(git log:*)", "Bash(git branch:*)", "Bash(git show:*)"]
    denied = ["Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/*.key)",
              "Read(**/id_rsa*)", "Read(**/id_ed25519*)", "WebFetch", "WebSearch"]
    cmd = ["claude", "-p", prompt, "--permission-mode", "dontAsk",
           "--add-dir", HERE, "--output-format", "text",
           "--allowedTools", *allowed, "--disallowedTools", *denied]
    if args.budget_usd:
        cmd += ["--max-budget-usd", str(args.budget_usd)]
    if args.model:
        cmd += ["--model", args.model]
    return cmd, config, report


def configured_path(config):
    try:
        with open(config) as f:
            return os.path.realpath(os.path.expanduser(json.load(f).get("path", "")))
    except (OSError, ValueError):
        return None


def onboard(path, args, index=1, total=1):
    path = os.path.realpath(os.path.expanduser(path))
    name = slug(args.name or os.path.basename(path))
    print(f"\n=== {name} ({tilde(path)})" + (f"  [{index} of {total}]" if total > 1 else ""))
    if run(["git", "-C", path, "rev-parse", "--git-dir"])[0] != 0:
        print("  SKIP: not a git repository")
        return False
    config = os.path.join(PROJECTS, f"{name}.json")
    if os.path.exists(config):
        other = configured_path(config)
        if other and other != path:
            print(f"  FAIL: the name '{name}' is already used by {tilde(other)}. "
                  f"Onboard this folder alone with --name <other-name>.")
            return False
        if not args.force:
            print(f"  SKIP: {tilde(config)} exists (use --force to redo it)")
            if args.no_register:
                return True
            # Cheap and safe to repeat: finishes a registration that failed the first time.
            ok, message = register(path, args.dry_run)
            print("  " + message)
            return ok
    if not args.no_register:
        ok, message = register(path, args.dry_run)
        print("  [1/4] " + message)
        if not ok:
            # Stop before the Claude run: nothing was changed, and no time or budget was spent.
            print("  FAIL: stopped before changing anything or running Claude.")
            return False
    print("  " + exclude_pipeline_dir(path, args.dry_run))
    print("  [2/4] collecting facts: git, stack, runtime, skills ...", flush=True)
    facts = collect_facts(path, name)
    cmd, config, report = claude_command(path, name, facts, args)
    if args.dry_run:
        print("  facts:\n" + "\n".join("    " + l for l in json.dumps(facts, indent=1).splitlines()))
        shown = [shlex.quote(c) for c in cmd]
        shown[2] = "<init-prompt.md with facts>"
        print("  would run in the project folder:", " ".join(shown))
        return True

    os.makedirs(PROJECTS, exist_ok=True)
    backup = config + ".bak"
    if os.path.exists(config):
        os.replace(config, backup)
    if os.path.exists(report):
        os.remove(report)  # a report from an earlier run would describe the old config
    log_path = os.path.join(PROJECTS, f"{name}.init.log")
    print(f"  [3/4] Claude reads the project and writes the config; this often takes 2 to 10 "
          f"minutes. Log: {tilde(log_path)}", flush=True)
    try:
        with open(log_path, "w") as log:
            proc = subprocess.Popen(cmd, cwd=path, stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "ORCA_INIT_CONFIG_OUT": config})
            wait_with_progress(proc, "Claude is working")
    except KeyboardInterrupt:
        if not os.path.exists(config) and os.path.exists(backup):
            os.replace(backup, config)
            print("  the previous config was restored.", flush=True)
        raise
    if not os.path.exists(config):
        restored = ""
        if os.path.exists(backup):
            os.replace(backup, config)
            restored = "; the previous config was restored"
        print(f"  FAIL: Claude did not write the config (exit {proc.returncode}); see the log"
              + restored)
        return False

    print("  [4/4] validating the configuration ...", flush=True)
    errors, warnings = validate_config.validate(config)
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    with open(config) as f:
        first_mode = next(iter(json.load(f).get("modes", {"small": 0})))
    code, out = run(["python3", os.path.join(HERE, "orca_pipeline.py"), "--config", config,
                     "--mode", first_mode, "--slug", "init-check", "--feature", "init check",
                     "--dry-run"], cwd=path)
    print("  pipeline dry run: " + ("ok" if code == 0 else f"FAILED: {out[-300:]}"))
    ok = not errors and code == 0
    print(f"  {'PASS' if ok else 'FAIL'}: review {tilde(report)} and {tilde(config)}")
    return ok


HELP = """orca-init: prepare a project for orca-dev, once per project.

It writes ~/orca-pipeline/projects/<name>.json (which agents, skills, test commands, E2E
URLs, base branch, and rules orca-dev uses for that project) by running Claude Code
headless in the project folder. Run it before the first orca-dev on a project, and again
with --force when the project's setup changes (new test runner, container, base branch).

Usage
  orca-init <path> [<path> ...]        onboard these project folders
  orca-init <path> --name <name>       pick the config name (default: the folder name)
  orca-init --all [--root <folder>]    every git repo directly under ~/Sites/app (or --root;
                                       repeat --root for several folders)
  orca-init <path> --dry-run           show what it found and would run; change nothing

Options
  --dry-run          print the collected facts and the Claude command only. Use it first.
  --force            redo a project that already has a config. The old config is kept as
                     <name>.json.bak and put back if the new run fails.
  --name <name>      needed when two folders share a name (~/Sites/app/api and ~/code/api)
  --no-register      do not add the folder to Orca's project list. Use it when Orca is closed
                     and you only want the configuration.
  --budget-usd <n>   cost cap per Claude run, in dollars (default 5; 0 = no cap)
  --model <id>       Claude model for the onboarding run (default: your Claude setting)

What happens per project
  1. Registers the folder in Orca, and adds .orca-pipeline/ to .git/info/exclude. If Orca
     is not running, orca-init stops here without running Claude: open Orca (or run
     `orca open`) and run the same command again. A project that already has a
     configuration is only registered, so nothing is redone.
  2. Collects facts it can check itself: branches on origin, stack versions, the runtime
     (a Docker container that mounts the folder, or this Mac's PHP/Node/Python), the test
     runner, the <name>.local host or dev-server URL, the vault page, and which skills
     Claude, OpenCode, and Codex can each see.
  3. Runs Claude (claude -p) in the folder. Claude may only read the project and write two
     files; it cannot edit the project, read .env files or keys, run docker, or browse.
  4. Checks the config with validate_config.py and a pipeline dry run, then prints PASS/FAIL.

While it works, orca-init shows steps [1/4] to [4/4]. During step 3 (the slow one) a live
line with a spinner and the elapsed time stays on screen; without a terminal, a line is
printed every 30 seconds. The run is finished only when the 'Done in ...' line appears.
Ctrl+C stops it, and an existing configuration is put back.

Files it writes (in ~/orca-pipeline/projects/)
  <name>.json              the config orca-dev reads
  <name>.init-report.md    decisions with evidence, open questions, validator result
  <name>.init.log          Claude's full output, for when something fails

After a run
  1. Read <name>.init-report.md, mainly "Open questions" (test user, base branch, tests
     that cannot run here) and fix the config by hand where needed.
  2. Check it again:  python3 ~/orca-pipeline/validate_config.py ~/orca-pipeline/projects/<name>.json
  3. See its modes:    orca-dev <name> --help
  4. Try a dry run:    orca-dev <name> feature try-it "Small test task" --dry-run

Examples
  orca-init ~/code/my-app --dry-run
  orca-init ~/code/my-app
  orca-init ~/code/api --name api-v2
  orca-init --all --root ~/code --root ~/work
  orca-init ~/code/my-app --force"""


def main():
    if any(a in ("-h", "--help", "help") for a in sys.argv[1:2]) or "--help" in sys.argv:
        names = sorted(f[:-5] for f in os.listdir(PROJECTS) if f.endswith(".json")) \
            if os.path.isdir(PROJECTS) else []
        print(HELP + "\n\nAlready onboarded: " + (", ".join(names) or "none"))
        return
    ap = argparse.ArgumentParser(prog="orca-init", add_help=False,
                                 usage="orca-init <path> [...] | --all [--root <folder>] "
                                       "[options]   (orca-init --help for details)")
    ap.add_argument("paths", nargs="*", help="project folders")
    ap.add_argument("--all", action="store_true", help="every git repo directly under --root")
    ap.add_argument("--root", action="append",
                    help="folder whose git repos --all onboards; repeat for several "
                         "(default ~/Sites/app)")
    ap.add_argument("--name", help="config name for a single project (default: folder name)")
    ap.add_argument("--force", action="store_true", help="redo projects that have a config")
    ap.add_argument("--dry-run", action="store_true", help="print facts and commands only")
    ap.add_argument("--no-register", action="store_true", help="do not add repos to Orca")
    ap.add_argument("--budget-usd", type=float, default=5.0,
                    help="cost cap per Claude run (0 = none)")
    ap.add_argument("--model", help="Claude model for the init runs")
    args = ap.parse_args()

    paths = list(args.paths)
    if args.all:
        for root in map(os.path.expanduser, args.root or ["~/Sites/app"]):
            if not os.path.isdir(root):
                ap.error(f"--root {root} is not a folder")
            paths += [os.path.join(root, d) for d in sorted(os.listdir(root))
                      if os.path.exists(os.path.join(root, d, ".git"))]
    if not paths:
        ap.error("give project paths or --all")
    if args.name and len(paths) != 1:
        ap.error("--name works with exactly one project path")
    if not args.dry_run and not shutil.which("claude"):
        sys.exit("claude CLI not found in PATH")
    if not args.dry_run and not args.no_register:
        up, detail = orca_reachable()
        if not up:  # say so once, before any project is touched
            sys.exit(orca_down_message(detail))
    started = time.time()
    results = {}
    try:
        for number, p in enumerate(paths, 1):
            results[p] = onboard(p, args, number, len(paths))
    except KeyboardInterrupt:
        print("\norca-init cancelled. Projects not reached were not changed.", flush=True)
        sys.exit(130)
    failed = [tilde(os.path.realpath(os.path.expanduser(p))) for p, ok in results.items()
              if not ok]
    print(f"\nDone in {elapsed_text(time.time() - started)}: {len(results) - len(failed)} ok, "
          f"{len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
