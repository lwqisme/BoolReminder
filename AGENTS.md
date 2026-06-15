# Agent Development Notes

## Environment & Deployment Topology

BoolReminder is a split setup across TWO hosts. The agent may be invoked on EITHER host, so never assume "this machine" means one specific box — **branch your behavior on which host the agent is currently running on.**

### Host identities (stable — use these to tell them apart)

| Host | OS | `hostname` | repo path | role |
|------|----|------------|-----------|------|
| **Dev (Mac)** | Darwin / macOS | `JohndeMacBook-Air.local` | `/Users/johntaylor/projects/BoolReminder` | development + unit tests |
| **Server (`tct-sh`)** | Linux | `VM-4-5-ubuntu` | `/home/ubuntu/projects/BoolReminder` | deployment + online testing, runs the container |

Detection rule (run first, every session):
```bash
hostname; uname -s
# JohndeMacBook-Air.local + Darwin      → you are on the DEV Mac
# VM-4-5-ubuntu          + Linux        → you are on the SERVER (== tct-sh, but possibly via local shell, not SSH)
```

### Role boundary (applies regardless of which host the agent runs on)

- Code development + unit tests belong on the **Dev Mac**.
- The Docker container / production service runs ONLY on the **Server**. Never `docker compose` on the Mac.
- The only thing that moves code between hosts is the `origin/main` git remote. Use `ssh tct-sh '<cmd>'` to drive the server from the Mac. No `scp`/`rsync` for git-tracked files.

### Standard shipping workflow — branch on where the agent is running

**Case A — agent running on the DEV Mac** (the common case):
1. Develop + run unit tests locally (e.g. `./test_ga.sh` for GA changes).
2. `git add <files> && git commit -m "<msg>" && git push`
3. `ssh tct-sh 'cd /home/ubuntu/projects/BoolReminder && git pull && docker compose up --build -d'`
4. Smoke-test over SSH: `ssh tct-sh 'curl ... ; docker logs ...'`

**Case B — agent running on the SERVER directly** (e.g. user SSH'd in and is working there):
1. Develop + run unit tests right there (`./test_ga.sh` etc.) — the repo is the same code.
2. `git add <files> && git commit -m "<msg>" && git push`
3. No SSH hop needed — deploy directly: `git pull && docker compose up --build -d` (run from `/home/ubuntu/projects/BoolReminder`).
4. Smoke-test directly: `curl ... ; docker logs ...` (the service listens on `127.0.0.1:5000` here).
   ⚠️ Do NOT run unit tests that require the Mac-only environment on the server unless they pass there too.

In both cases the deliverable is identical: code pushed to `origin/main` + container rebuilt on the server + smoke test green. The only difference is whether steps 3–4 need an `ssh tct-sh` prefix.

## Skills

Before starting any task, always read the full SKILL.md of this skill:

- Caveman: `/home/ubuntu/.pi/agent/skills/caveman/SKILL.md` — ultra-compressed communication mode (~75% fewer tokens)

Use the `read` tool to load it at the beginning of each conversation.

⚠️ This path exists on the **Server only** (`VM-4-5-ubuntu`). If the agent is running on the Mac, the file won't be present locally — either `ssh tct-sh 'cat /home/ubuntu/.pi/agent/skills/caveman/SKILL.md'` to read it remotely, or skip it on the Mac.

---

This project values convergent, reuse-first development.

- Prefer reusing existing modules, helpers, workers, routes, templates, styles, and tests before adding new paths.
- Keep changes close to the current module boundary and avoid scattering related behavior across unrelated files.
- Fix inconsistencies by aligning the existing flow instead of creating parallel implementations.
- Add abstractions only when they remove real duplication or simplify an established pattern.
- Keep UI and API changes consistent with the surrounding codebase unless the task explicitly calls for a redesign.
- When a behavior already exists in one place, extend or compose it rather than reimplementing it elsewhere.
- After completing any code change, automatically commit, push to `origin/main`, rebuild + restart the container on the SERVER, and verify — following the topology rules above (Case A on the Mac, Case B on the server). Do not wait for the user to ask. Never run `docker compose` on the Mac; the container lives on `tct-sh` (`VM-4-5-ubuntu`) only.

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

After GA changes: verify the page loads AND the GA packet endpoint works. Run the checks against the SERVER's `127.0.0.1:5000` — over SSH if the agent is on the Mac (Case A), directly if the agent is already on the server (Case B):

  ```bash
  # page must return 200
  #   Case A (Mac):   ssh tct-sh 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/strategy-lab/parameter-lab'
  #   Case B (server): curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/strategy-lab/parameter-lab
  # endpoint must report success:
  #   Case A (Mac):   ssh tct-sh 'curl -s -X POST http://127.0.0.1:5000/api/strategy-lab/parameter-lab/ga-packet -H "Content-Type: application/json" -d "{\"ga_buy_strategy\":\"pyramid_3\",\"ga_sell_strategy\":\"none\",\"start\":\"2026-01-01\",\"end\":\"2026-05-01\",\"ga_population_size\":3}"' | python3 -c "import json,sys; assert json.load(sys.stdin)['success']"
  #   Case B (server): curl -s -X POST http://127.0.0.1:5000/api/strategy-lab/parameter-lab/ga-packet -H 'Content-Type: application/json' -d '{"ga_buy_strategy":"pyramid_3","ga_sell_strategy":"none","start":"2026-01-01","end":"2026-05-01","ga_population_size":3}' | python3 -c "import json,sys; assert json.load(sys.stdin)['success']"
  ```
