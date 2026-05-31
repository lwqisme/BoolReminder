# Agent Development Notes

This project values convergent, reuse-first development.

- Prefer reusing existing modules, helpers, workers, routes, templates, styles, and tests before adding new paths.
- Keep changes close to the current module boundary and avoid scattering related behavior across unrelated files.
- Fix inconsistencies by aligning the existing flow instead of creating parallel implementations.
- Add abstractions only when they remove real duplication or simplify an established pattern.
- Keep UI and API changes consistent with the surrounding codebase unless the task explicitly calls for a redesign.
- When a behavior already exists in one place, extend or compose it rather than reimplementing it elsewhere.
- After completing any code change, automatically commit with a concise message, push to origin/main, and rebuild + restart the Docker container: `git add <files> && git commit -m "<msg>" && git push && docker compose up --build -d`. Do not wait for the user to ask.

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

After GA changes: verify the page loads (`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/strategy-lab/parameter-lab` must return 200) AND the GA packet endpoint works (`curl -s -X POST http://127.0.0.1:5000/api/strategy-lab/parameter-lab/ga-packet -H 'Content-Type: application/json' -d '{"ga_buy_strategy":"pyramid_3","ga_sell_strategy":"none","start":"2026-01-01","end":"2026-05-01","ga_population_size":3}' | python3 -c "import json,sys; assert json.load(sys.stdin)['success']"`).
