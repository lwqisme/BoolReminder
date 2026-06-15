# Agent Development Notes

## Environment & Deployment Topology

BoolReminder runs in a split setup. Respect this boundary:

- **Local (this machine)**: code development + unit tests ONLY. Never run the app container or production services here.
- **Remote server (SSH alias `tct-sh`)**: deployment + online/production testing. All container and service operations happen here.
- **Sync channel**: the only thing that moves code between local and remote is the `origin/main` git remote, plus SSH for remote commands.

Standard shipping workflow:

1. Local: develop, run unit tests (e.g. `./test_ga.sh` for GA changes).
2. Local: commit + push → `git add <files> && git commit -m "<msg>" && git push`.
3. Remote: pull + rebuild + restart → `ssh tct-sh 'cd /home/ubuntu/projects/BoolReminder && git pull && docker compose up --build -d'`.
4. Remote: smoke-test the deployed service over SSH (curl health endpoints, check logs, etc.).

Use `ssh tct-sh '<cmd>'` for any remote command. Use `scp`/`rsync` only for files not tracked in git.

## Skills

Before starting any task, always read the full SKILL.md of this skill:

- Caveman: `/home/ubuntu/.pi/agent/skills/caveman/SKILL.md` — ultra-compressed communication mode (~75% fewer tokens)

Use the `read` tool to load it at the beginning of each conversation.

---

This project values convergent, reuse-first development.

- Prefer reusing existing modules, helpers, workers, routes, templates, styles, and tests before adding new paths.
- Keep changes close to the current module boundary and avoid scattering related behavior across unrelated files.
- Fix inconsistencies by aligning the existing flow instead of creating parallel implementations.
- Add abstractions only when they remove real duplication or simplify an established pattern.
- Keep UI and API changes consistent with the surrounding codebase unless the task explicitly calls for a redesign.
- When a behavior already exists in one place, extend or compose it rather than reimplementing it elsewhere.
- After completing any code change, automatically commit, push to `origin/main`, then rebuild + restart the Docker container ON THE REMOTE SERVER, and verify. Do not wait for the user to ask. Concretely:
  ```bash
  # local
  git add <files> && git commit -m "<msg>" && git push
  # remote
  ssh tct-sh 'cd /home/ubuntu/projects/BoolReminder && git pull && docker compose up --build -d'
  ```
  Do NOT run `docker compose` locally — the container lives on `tct-sh` (see topology above).

## GA Changes — MANDATORY Test Suite

**EVERY commit touching GA code (drawdown/strategy_parameter_genetic.py, web/app.py GA endpoints, web/templates/strategy_parameter_lab.html GA section, web/static/strategy_parameter_lab_worker.js) MUST pass the full GA test suite first:**

```bash
./test_ga.sh
```

This runs:
- Python: `test_strategy_parameter_genetic` (23 tests) — GA engine logic
- Python: `test_strategy_parameter_registry` (27 tests) — regression guard
- Python: `test_ga_e2e` (5 tests) — Worker lifecycle + continuous params + API structure
- JavaScript: `test_parameter_lab_ga.js` (19 tests) — mutate/crossover/select/NaN handling, gaParamKey dedup, display-stats dedup

**70 tests total. ALL must pass.** Never commit GA changes with failing or skipped tests.

After GA changes: verify the page loads AND the GA packet endpoint works — both over SSH on the remote server, since the service runs on `tct-sh`:
  ```bash
  # page must return 200
  ssh tct-sh 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/strategy-lab/parameter-lab'
  # endpoint must report success
  ssh tct-sh 'curl -s -X POST http://127.0.0.1:5000/api/strategy-lab/parameter-lab/ga-packet -H "Content-Type: application/json" -d "{\"ga_buy_strategy\":\"pyramid_3\",\"ga_sell_strategy\":\"none\",\"start\":\"2026-01-01\",\"end\":\"2026-05-01\",\"ga_population_size\":3}"' | python3 -c "import json,sys; assert json.load(sys.stdin)['success']"
  ```
