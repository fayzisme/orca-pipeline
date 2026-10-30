# Orca pipeline shortcut for every onboarded project. Load from ~/.zshrc with:
#   source ~/orca-pipeline/orca-dev.zsh
#
#   orca-dev <project> <feature|fix|improve|hotfix> <slug> "<task>"
#            [--mode <mode>] [--no-branch] [orca_pipeline.py flags, e.g. --e2e --dry-run]
#   orca-dev --list
#   orca-init <project path>... | --all   onboard projects (orca_init.py --help)
#
# <project> is a config in $ORCA_PIPELINE_DIR/projects/<project>.json (create one with
# orca-init). Its "path" is the checkout and "ship.base_branch" the base branch (default
# staging).
#
#   1. Branch: switches the checkout to <type>/<slug>, created from origin/<base> when it
#      does not exist yet. Local changes are carried over, never discarded; if they block
#      the switch, it stops. --no-branch keeps the current branch.
#   2. Pipeline: opens a coordinator tab in the project's Orca workspace and runs
#      orca_pipeline.py. The mode follows the type (fix, hotfix: bugfix; feature, improve:
#      small); --mode overrides it.

: ${ORCA_PIPELINE_DIR:=$HOME/orca-pipeline}

# Prints "ok" or the error from an `orca ... --json` reply on stdin.
_orca_dev_report() {
  python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except ValueError:
    sys.exit("orca: " + raw.strip()[:500])
if not data.get("ok"):
    sys.exit("orca failed: " + json.dumps(data.get("error") or data)[:500])
print("ok: coordinator tab started in Orca")
'
}

# Prints "<path>\t<base branch>" from a project config. The base branch comes from
# orca_pipeline.resolve_base: the configured one when origin has it, else staging (under
# ~/Sites), origin's default branch, main, or master. A fallback is reported on stderr.
_orca_dev_target() {
  python3 -c '
import json, os, sys
sys.path.insert(0, sys.argv[2])
from orca_pipeline import resolve_base
cfg = json.load(open(sys.argv[1]))
path = os.path.expanduser(cfg.get("path", ""))
if not path:
    sys.exit("config has no \"path\": " + sys.argv[1])
base, note = resolve_base(cfg, path)
if note:
    print("base branch: " + note, file=sys.stderr)
print(path + "\t" + base)
' $1 $ORCA_PIPELINE_DIR
}

# Switches checkout $1 to branch $2, created from origin/$3. $4 = "dry" only prints.
_orca_dev_branch() {
  emulate -L zsh
  local dir=$1 branch=$2 base=$3 dry=$4 current
  current=$(git -C $dir branch --show-current) || return 1
  if [[ $current == $branch ]]; then
    print -- "branch: already on $branch"; return 0
  fi
  local exists=
  git -C $dir show-ref --verify --quiet refs/heads/$branch && exists=local
  [[ -z $exists ]] && git -C $dir show-ref --verify --quiet refs/remotes/origin/$branch \
    && exists=remote
  local how="(new, from origin/$base)"
  [[ -n $exists ]] && how="(existing $exists branch)"
  if [[ $dry == dry ]]; then
    print -- "branch (dry run, no switch): $current -> $branch $how"
    return 0
  fi
  if [[ -n $exists ]]; then
    git -C $dir switch --quiet $branch
  else
    git -C $dir fetch --quiet origin $base \
      && git -C $dir switch --quiet --no-track -c $branch origin/$base
  fi || {
    print -u2 -- "Could not switch to $branch. Nothing was discarded. Commit, stash, or move" \
      "the conflicting local changes yourself, then rerun (or pass --no-branch)."
    return 1
  }
  print -- "branch: $current -> $branch $how"
}

# Prints help: general when $1 is empty, else the modes of that project's config.
_orca_dev_help() {
  python3 - "$ORCA_PIPELINE_DIR/projects" "$1" "$ORCA_PIPELINE_DIR" <<'EOF'
import json, os, sys
projects, project = sys.argv[1], sys.argv[2]
sys.path.insert(0, sys.argv[3])
from orca_pipeline import stage_agents
names = sorted(f[:-5] for f in os.listdir(projects) if f.endswith(".json")) \
    if os.path.isdir(projects) else []
USE = {
    "small": "small or medium change: a field, a filter, a report column, a small rule",
    "bugfix": "something is broken: an error, a wrong number, a crash (root cause first)",
    "ui": "screens, layout, forms, styling, responsive pages",
    "secure": "login, permissions, user input, money, file uploads, anything security-sensitive",
    "large": "a large feature over several modules",
    "scratch": "a new module or project from zero",
}
if not project:
    print("""orca-dev: create the task branch, then run the multi-agent pipeline
(plan: Claude Code -> implement: OpenCode -> review: Codex -> ship gate) in an Orca tab.

Usage
  orca-dev <project> <type> <slug> "<task>" [--mode <mode>] [--no-branch] [pipeline flags]
  orca-dev <project> --help      modes and skills of that project
  orca-dev --list                projects that have a config
  orca-dev --agents [project]    agent profiles: Orca id, model, CLI, skill folders
  orca-init <path>               create the config for a new project

Arguments
  <project>   a config in ~/orca-pipeline/projects/<project>.json
  <type>      branch prefix: feature | fix | improve | hotfix  -> branch <type>/<slug>
  <slug>      kebab-case name, also used for docs/plans/<slug>.md and the coordinator tab
  "<task>"    what to build or fix, in plain words (quote it)

Mode: which skills the agents use. Without --mode:
  fix, hotfix       -> bugfix
  feature, improve  -> small
Use --mode when the task is one of these:""")
    for mode, use in USE.items():
        if mode not in ("small", "bugfix"):
            print(f"  --mode {mode:8} {use}")
    print("""
Options
  --mode <mode>          skill set for this run (see orca-dev <project> --help)
  --no-branch            stay on the current branch
  --dry-run              print every worker spec; start nothing, switch no branch
  --e2e                  also run the end-to-end worker (Codex), next to the review
  --module a,b           E2E only these modules (keys of e2e.modules in the config)
  --start-at <stage>     plan | implement | review | ship: skip stages already done
  --known-failures "..." tests that already fail before this change
  --resume-run <runId>   continue an existing run from the same coordinator tab

Gates (in the coordinator tab)
  after the plan:  y = continue, r <notes> = revise, q = quit
  after review PASS (ship): y = commit+push+PR, w = also follow the PR review,
                            n = stop; y and w then need the word: push

Examples
  orca-dev my-app fix invoice-rounding "Invoice totals round the wrong way" --e2e --module reports
  orca-dev my-app feature order-form-layout "Tidy the order form layout" --mode ui
  orca-dev my-app feature order-discount "Add a discount field" --dry-run""")
    print("\nProjects: " + (", ".join(names) or "none yet (run orca-init <path>)"))
    sys.exit(0)

path = os.path.join(projects, project + ".json")
if not os.path.exists(path):
    sys.exit(f"No config for '{project}'. Projects: {', '.join(names) or 'none'}. "
             f"Create one with: orca-init <path>")
cfg = json.load(open(path))
agents = stage_agents(cfg)
show = {s: (", ".join(a) if isinstance(a, list) else a) for s, a in agents.items()}
print("Agents: " + " | ".join(f"{s} {a}" for s, a in show.items()) + "   (orca-dev --agents for profiles)")
print(f"Modes for {project} ({cfg.get('path', '?')}). Default: fix/hotfix -> bugfix, "
      f"feature/improve -> small.\n")
seen = {}
for mode, stages in cfg.get("modes", {}).items():
    key = json.dumps({s: stages.get(s, []) for s in ("plan", "implement", "review")})
    note = ("   (default for fix/hotfix)" if mode == "bugfix" else
            "   (default for feature/improve)" if mode == "small" else "")
    if key in seen:
        note += f"   (same skills as {seen[key]} in this project)"
    seen.setdefault(key, mode)
    print(f"--mode {mode}{note}")
    print(f"  use for: {USE.get(mode, 'custom mode from this config')}")
    for stage in ("plan", "implement", "review"):
        print(f"  {stage:9} ({show[stage]}): {', '.join(stages.get(stage, [])) or '-'}")
    print()
e2e = cfg.get("e2e", {})
if e2e.get("modules"):
    print("E2E modules (--module): " + ", ".join(e2e["modules"]))
ship = cfg.get("ship", {})
print(f"Ship: {'on' if ship.get('enabled') else 'off'}, skills {', '.join(ship.get('skills', [])) or '-'}")
EOF
}

# Lists agent profiles (agents.json, agents.local.json, and the project's agent_profiles).
_orca_dev_agents() {
  python3 - "$ORCA_PIPELINE_DIR" "$1" <<'EOF'
import json, os, shutil, sys
sys.path.insert(0, sys.argv[1])
from orca_pipeline import load_agent_profiles, stage_agents
cfg = {}
if sys.argv[2]:
    path = os.path.join(sys.argv[1], "projects", sys.argv[2] + ".json")
    if not os.path.exists(path):
        sys.exit(f"No config for '{sys.argv[2]}'")
    cfg = json.load(open(path))
profiles = load_agent_profiles(cfg)
used = {}
if cfg:
    for stage, names in stage_agents(cfg).items():
        for n in (names if isinstance(names, list) else [names]):
            used.setdefault(n, []).append(stage)
project_dir = os.path.expanduser(cfg.get("path", "."))
print("Agent profiles" + (f" for {sys.argv[2]}" if cfg else "") + ":\n")
for name in sorted(set(profiles) | set(used)):
    p = profiles.get(name)
    if p is None:
        print(f"{name}  (no profile: starts with --agent {name}; skills not checked)")
    else:
        flags = "".join(f" --{k} {p[k]}" for k in ("model", "effort") if p.get(k))
        cli = p.get("cli")
        cli_state = "-" if not cli else cli + (" (installed)" if shutil.which(cli)
                                               else " (NOT installed)")
        dirs = [os.path.expanduser(d.replace("{project}", project_dir))
                for d in p.get("skill_dirs", [])]
        print(f"{name}  {p.get('description', '')}")
        print(f"  launch: --agent {p.get('orca_id', name)}{flags}   cli: {cli_state}")
        print(f"  skill folders: {sum(os.path.isdir(d) for d in dirs)} of {len(dirs)} exist"
              + ("; plugin skills" if p.get("plugin_skills") else ""))
    if name in used:
        print(f"  used for: {', '.join(used[name])}")
    print()
print("Add or change agents in agents.local.json (all projects) or agent_profiles in a "
      "project config.")
EOF
}

orca-dev() {
  emulate -L zsh
  local usage='usage: orca-dev <project> <feature|fix|improve|hotfix> <slug> "<task>" [--mode <mode>] [--no-branch] [pipeline flags...]
       orca-dev --help | orca-dev <project> --help | orca-dev --list | orca-dev --agents [project]'
  local projects=$ORCA_PIPELINE_DIR/projects
  if [[ $1 == (--help|-h|help) ]]; then
    _orca_dev_help ""; return
  fi
  if [[ $2 == (--help|-h|help) ]]; then
    _orca_dev_help $1; return
  fi
  if [[ $1 == --agents ]]; then
    _orca_dev_agents ${2:-}; return
  fi
  if [[ $1 == --list ]]; then
    local f
    for f in $projects/*.json(N); do print -- ${f:t:r}; done
    return 0
  fi
  if (( $# < 4 )); then
    print -u2 -- $usage; return 2
  fi
  local project=$1 type=$2 slug=$3 task=$4; shift 4
  local config=$projects/$project.json
  if [[ ! -f $config ]]; then
    print -u2 -- "No config for '$project' ($config). Create it with: orca-init <path>"
    return 2
  fi
  if [[ $type != (feature|fix|improve|hotfix) ]]; then
    print -u2 -- $usage; return 2
  fi
  if [[ ! $slug =~ '^[a-z0-9][a-z0-9-]*$' ]]; then
    print -u2 -- "slug must be kebab-case: lowercase letters, digits, hyphens"; return 2
  fi
  local target dir base
  target=$(_orca_dev_target $config) || return 2
  dir=${target%%$'\t'*}; base=${target#*$'\t'}
  if [[ ! -d $dir/.git && ! -f $dir/.git ]]; then
    print -u2 -- "Not a git checkout: $dir (from \"path\" in $config)"; return 2
  fi

  local mode=small branch_step=1 dry= ; local -a pass
  [[ $type == (fix|hotfix) ]] && mode=bugfix
  while (( $# )); do
    case $1 in
      --mode)
        if (( $# < 2 )); then print -u2 -- "--mode needs a value"; return 2; fi
        mode=$2; shift 2 ;;
      --no-branch) branch_step=; shift ;;
      *) [[ $1 == --dry-run ]] && dry=dry; pass+=("$1"); shift ;;
    esac
  done

  if [[ -n $branch_step ]]; then
    _orca_dev_branch $dir $type/$slug $base $dry || return 1
  fi

  local cmd="cd ${(q)dir} && python3 ${(q)ORCA_PIPELINE_DIR}/orca_pipeline.py"
  cmd+=" --config ${(q)config} --mode ${(q)mode} --slug ${(q)slug} --feature ${(q)task}"
  (( ${#pass} )) && cmd+=" ${(j: :)${(@q)pass}}"
  orca terminal create --worktree path:$dir --title coord-$slug --focus --json \
    --command "$cmd" | _orca_dev_report
}


# orca-init <project path>... | --all   (see orca_init.py --help)
orca-init() { python3 $ORCA_PIPELINE_DIR/orca_init.py "$@"; }
