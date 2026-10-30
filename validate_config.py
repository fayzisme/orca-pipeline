#!/usr/bin/env python3
"""Check an orca_pipeline.py project config before using it.

  python3 validate_config.py projects/<project>.json [--no-network]

Errors (exit 1): missing keys, a "path" that is not a git checkout, a mode without its
stages, a skill the stage's agent cannot see, an unknown agent. Warnings (exit 0): base
branch missing on origin, E2E URLs that do not answer, no test_commands per stage, a test
command that cannot start here (container not running, binary not installed or missing in
the project).
Read-only: it never changes the config or the project.
"""
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orca_pipeline  # noqa: E402  (shared resolve_base)

HOME = os.path.expanduser("~")
KNOWN_AGENTS = ("claude", "opencode", "codex")
STAGES = ("plan", "implement", "review")


def skill_dirs(agent, project):
    """Folders that hold <skill>/SKILL.md for each agent, global first, then the project."""
    if agent == "claude":
        roots = [f"{HOME}/.claude/skills", f"{project}/.claude/skills"]
    elif agent == "opencode":
        roots = [f"{HOME}/.config/opencode/skills", f"{HOME}/.claude/skills",
                 f"{HOME}/.agents/skills", f"{project}/.opencode/skills",
                 f"{project}/.claude/skills", f"{project}/.agents/skills"]
    else:  # codex
        roots = [f"{HOME}/.agents/skills", f"{HOME}/.codex/skills", f"{project}/.agents/skills"]
    return [r for r in roots if os.path.isdir(r)]


def plugin_skill_exists(name):
    """Claude plugin skills are named <plugin>:<skill>."""
    plugin, _, skill = name.partition(":")
    patterns = [f"{HOME}/.claude/plugins/cache/*/{plugin}/*/skills/{skill}/SKILL.md",
                f"{HOME}/.claude/local-plugins/{plugin}/skills/{skill}/SKILL.md"]
    return any(glob.glob(p) for p in patterns)


def skill_visible(agent, name, project):
    if agent == "claude" and ":" in name:
        return plugin_skill_exists(name)
    bare = name.split(":")[-1]  # other agents see plugin skills only as plain folders
    return any(os.path.isfile(f"{d}/{bare}/SKILL.md") for d in skill_dirs(agent, project))


def git(project, *args):
    return subprocess.run(["git", "-C", project, *args], capture_output=True, text=True)


def running_containers():
    proc = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                          text=True)
    return set(proc.stdout.split()) if proc.returncode == 0 else None


def command_problem(command, project, containers):
    """Why a test command cannot start on this Mac, or None. Text after ' (' is a note."""
    words = shlex.split(command.split(" (")[0], posix=True) if command else []
    while words and ("=" in words[0] and not words[0].startswith("-")):
        words.pop(0)  # FOO=bar env assignments
    if len(words) >= 3 and words[0] == "cd" and words[2] == "&&":
        words = words[3:]
    if not words:
        return None
    if words[:2] == ["docker", "exec"]:
        rest, i = words[2:], 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-w", "--workdir", "-u", "--user", "-e", "--env") else 1
        name = rest[i] if i < len(rest) else ""
        if containers is None:
            return "docker is not running"
        return None if name in containers else f"container '{name}' is not running"
    first = words[0]
    if "/" in first and not os.path.isabs(first):
        return None if os.path.exists(os.path.join(project, first)) else f"{first} not found"
    return None if shutil.which(first) else f"'{first}' is not installed on this Mac"


def url_status(url):
    try:
        req = urllib.request.Request(url, method="GET")
        # Do not follow redirects: a 302 to a login page already proves the route answers.
        opener = urllib.request.build_opener(NoRedirect)
        return opener.open(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001 - any failure is just "unreachable"
        return f"unreachable ({type(e).__name__})"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate(path, network=True):
    errors, warnings = [], []
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [f"cannot read config: {e}"], []

    for key in ("project", "path", "test_command", "modes"):
        if not cfg.get(key):
            errors.append(f"missing key: {key}")
    project = os.path.expanduser(cfg.get("path", ""))
    if project and not git(project, "rev-parse", "--git-dir").returncode == 0:
        errors.append(f"path is not a git checkout: {cfg.get('path')}")

    agents = {"plan": "claude", "implement": "opencode", "review": "codex", "e2e": "codex",
              "ship": "codex", **cfg.get("agents", {})}
    for stage, agent in agents.items():
        if agent not in KNOWN_AGENTS:
            errors.append(f"agents.{stage}: unknown agent '{agent}'")

    for mode, stages in (cfg.get("modes") or {}).items():
        for stage in STAGES:
            if not stages.get(stage):
                errors.append(f"modes.{mode}.{stage}: empty")
            for skill in stages.get(stage) or []:
                if not skill_visible(agents.get(stage), skill, project):
                    errors.append(f"modes.{mode}.{stage}: '{skill}' not visible to "
                                  f"{agents.get(stage)}")
    for stage, skills in (("e2e", cfg.get("e2e", {}).get("skills", [])),
                          ("ship", cfg.get("ship", {}).get("skills", []))):
        for skill in skills:
            if not skill_visible(agents.get(stage), skill, project):
                errors.append(f"{stage}.skills: '{skill}' not visible to {agents.get(stage)}")

    if not cfg.get("test_commands"):
        warnings.append("no test_commands: every stage uses test_command")
    if project:
        containers = running_containers()
        commands = {"test_command": cfg.get("test_command", "")}
        commands.update({f"test_commands.{k}": v for k, v in
                         (cfg.get("test_commands") or {}).items()})
        for key, command in commands.items():
            try:
                problem = command_problem(command, project, containers)
            except ValueError as e:  # unbalanced quotes
                problem = f"cannot parse ({e})"
            if problem:
                warnings.append(f"{key}: {problem}")
    if not cfg.get("known_failures"):
        warnings.append("known_failures not set: use \"none\" or list the failing tests")

    if project:  # orca-dev branches from it even when the ship stage is off
        _, note = orca_pipeline.resolve_base(cfg, project)
        if note:
            warnings.append(f"base branch: {note}")

    e2e = cfg.get("e2e", {})
    if e2e.get("modules") and not e2e.get("base_url"):
        warnings.append("e2e.modules are set but e2e.base_url is empty")
    if network and e2e.get("base_url"):
        base_url = e2e["base_url"].rstrip("/")
        for name, route in (e2e.get("modules") or {"(base)": ""}).items():
            status = url_status(base_url + route)
            if not (isinstance(status, int) and status < 500 and status != 404):
                warnings.append(f"e2e {name}: {base_url + route} -> {status}")
    return errors, warnings


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    errors, warnings = validate(args[0], network="--no-network" not in sys.argv)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print("RESULT: " + ("FAIL" if errors else "PASS") +
          f" ({len(errors)} errors, {len(warnings)} warnings)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
