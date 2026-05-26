# Agent Development Notes

This project values convergent, reuse-first development.

- Prefer reusing existing modules, helpers, workers, routes, templates, styles, and tests before adding new paths.
- Keep changes close to the current module boundary and avoid scattering related behavior across unrelated files.
- Fix inconsistencies by aligning the existing flow instead of creating parallel implementations.
- Add abstractions only when they remove real duplication or simplify an established pattern.
- Keep UI and API changes consistent with the surrounding codebase unless the task explicitly calls for a redesign.
- When a behavior already exists in one place, extend or compose it rather than reimplementing it elsewhere.
- After completing any code change, automatically commit with a concise message, push to origin/main, and rebuild + restart the Docker container: `git add <files> && git commit -m "<msg>" && git push && docker compose up --build -d`. Do not wait for the user to ask.
