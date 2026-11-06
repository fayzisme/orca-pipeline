You are onboarding the project in the current directory (@PROJECT@) to a multi-agent Orca
pipeline. Your only output is two files: a pipeline config and a short report. You run
unattended: nobody can answer questions, so record open questions in the report instead.

## Output

1. `@CONFIG_OUT@` — a JSON config with the same keys and structure as the reference config
   `@EXAMPLE@`. Read the reference and the section of `@GUIDE@` that explains each key
   ("Project configuration").
2. `@REPORT_OUT@` — Markdown, in English, at most 60 lines:
   - `## Summary` — stack, runtime (container or host), test runner, local URL, base branch,
     in 3 to 6 bullets.
   - `## Decisions` — each non-obvious value you chose and the evidence (file:line).
   - `## Open questions` — what a human must confirm or fill in (for example a test user,
     a module URL you could not confirm, a base branch). Write "none" if there are none.
   - `## Validation` — the last validator output.

## Hard rules

- Read-only on the project. Never edit, create, or delete files in the project, and never
  run git commands that change state.
- Never read `.env` files, credentials, keys, certificates, or files that hold passwords or
  tokens. Never put a real password, token, IP address, or person's data in the config or
  the report. Point to environment variables instead, as the reference does
  (use `<PROJECT>_E2E_USER` and `<PROJECT>_E2E_PASSWORD` style names).
- Do not run tests, artisan, composer, npm, or docker. Facts that need them are below.
- Only use skills that the facts list as visible for the agent of that stage. Never invent
  skill names.

## Facts collected by orca-init (trust these over guesses)

```json
@FACTS@
```

## How to decide each value

- `project`: "@PROJECT@". `path`: "@PATH_TILDE@".
- Test commands. Pick the runner from the project (phpunit.xml, composer scripts,
  package.json "test", vitest/jest config, pytest config). Then, by `runtime.where`:
  - `docker`: run tests inside the container
    (`docker exec -w <mount destination> <container> <runner> ...`), as the reference does,
    and add to `extra_rules.implement` that php/artisan/composer/phpunit run only inside the
    container, never with the host toolchain.
  - `host`: no running container mounts this project, so tests run on this Mac from the
    project root with the project's own binaries: `vendor/bin/phpunit`, the package
    manager in the facts (`npm test`, `npx vitest run <file>`, `pnpm test`), or the venv's
    `pytest`. Compare the host versions in the facts with what the project requires
    (composer.json `php`, `engines`, version pin files). When they conflict, put the exact
    command that selects the right version (for example from a version pin file) in
    `test_commands`, or, if you cannot find one, say that tests may not run on this host in
    `known_failures`, in `extra_rules.implement`, and in the open questions. When
    dependencies are not installed (`vendor_installed` / `node_modules_installed` false),
    tests cannot run yet: say so the same way, and never add an install step yourself.
    If the facts list Docker files but no running container, add an open question asking
    whether the project should be started in Docker; configure the host commands meanwhile.
  If there is no test suite, or the runner cannot work with the runtime, say so in
  `test_commands`, `known_failures`, and the report, and use a syntax check (`php -l` on
  changed files, `node --check`, `python -m py_compile`) as the fallback command.
  `implement` runs only the test files the plan names; `review` runs the full suite once.
- Database safety: find out whether tests share the app database (phpunit.xml `DB_*`
  values, test bootstrap). If they do, or if you cannot tell, copy the reference's rule that
  tests must not write to the database into `extra_rules.implement` and `extra_rules.review`.
- `known_failures`: "none" unless the code clearly shows a broken test; never guess.
- `agents`: keep the stages' agents as in the reference. Only name agents listed under
  `agents` in the facts, preferably ones with `cli_installed` true. `agents.review` may be
  a list when the reference uses several reviewers; every other stage takes one agent. Copy
  the reference's `agent_profiles` when it has them.
- `modes`: keep the six modes (small, bugfix, ui, secure, large, scratch). Start from the
  reference skills, then drop or replace each skill that is not visible for that agent in
  the facts. Laravel skills only for Laravel projects; `bmad-*` skills only if the facts say
  BMad is present (otherwise use `dev-workflow:dev-workflow` for planning and
  `implement-plan` for implementing in large and scratch too). Every stage needs at least
  one visible skill.
- `e2e`: `enabled` false. `base_url`: `http://<name>.local` when that host is in the
  facts; otherwise the dev-server URL the project documents or its scripts show (for
  example `http://localhost:5173` for Vite, `http://127.0.0.1:8000` for `artisan serve`),
  plus an `extra_rules.e2e` sentence saying the user must start that server first; if the
  project is not a web app (CLI, library, script), set `base_url` to "" and `modules` to
  {}. `modules`: top-level route prefixes of the app's modules (for example from
  `modules/*/Http/routes.php`, `routes/web.php`, or the router file); confirm them by
  reading route files, not by guessing. `login_note`: how a test user logs in, using
  environment variables. Keep `skills` as in the reference if visible to the e2e agent.
- `ship`: `skills` as in the reference when visible to the ship agent, `follow_review` false,
  `timeout_min` 30. `base_branch` is where task branches start and where PRs go:
  - first, the branch the project's docs or CLAUDE.md name for PRs (for example a branching
    guide), when it exists on origin;
  - otherwise, when `under_sites` is true: `staging` when it exists on origin (the ~/Sites
    convention);
  - otherwise, when `under_sites` is false: origin's default branch, which is usually `main`
    or `master`. Do not pick `staging` just because it exists outside ~/Sites; mention it
    as an open question instead;
  - if none of these exist, list it as an open question.
  `enabled` true only when origin is on github.com and a ship skill is visible (the ship
  skill opens the PR with `gh`);
  otherwise false, with an open question. When `under_sites` is false, add an open
  question confirming that this project uses the same commit and PR rules (the ship skill was
  written for ~/Sites projects).
- `board`: as in the reference. `gates`, `stage_timeout_min`, `max_fix_rounds`,
  `answer_questions`, `plan_dir`, `review_dir`: as in the reference unless the project's
  docs say otherwise.
- `extra_rules`: project-specific rules agents keep missing, in English, one short paragraph
  per stage. Read CLAUDE.md / AGENTS.md / README and, when the facts name a
  `vault_project`, run `obs decisions --project <vault_project>`, then `obs get <adr>
  --section Decision --max-lines 40` for the ones about tests, runtime, or data safety.
  Mention the runtime constraints (for example Laravel 5.2 / PHP 7 limits) and how to run
  commands (inside the container, or on the host with which binaries). Do not copy
  rules from the reference that do not apply to this project. The implement, review, e2e,
  and ship rules are read by other agents that may lack tools only your session has (MCP
  servers such as database tools, or commands such as `obs` and `gq`). Never make such a
  tool required there; write "if available" instead.

## Validate before you finish

Run `@VALIDATE@` and fix every ERROR it reports. Warnings may stay, but explain each one in
the report. Stop after the validator passes, or after three attempts; then report what is
still failing.
