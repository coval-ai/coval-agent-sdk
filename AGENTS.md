# Project Guidance

- Read `README.md`, `docs/local-development.md`, and `docs/developer-api.md` before changing the SDK.
- Keep the repository private; do not change its visibility, publish distributions, or add publishing automation without an explicit user request.
- Keep the SDK independent of the backend checkout, Django, AWS configuration, and agent-playground source paths.
- Preserve `coval_voice`, compatibility imports, and the current distribution contract during extraction; discuss API and naming changes through the developer API design before implementing them.
- Keep framework integrations outside the core transport and lifecycle implementation.
- Use synthetic examples and tests, read secrets from the environment, and never copy local credentials into this project.
- Run `scripts/check` after changes that affect packaging or SDK behavior; it validates a built wheel outside the checkout without live API or provider calls.
- Keep proposed interfaces in design documentation clearly labeled as unimplemented.
