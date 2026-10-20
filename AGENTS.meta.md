# AGENTS.meta.md

Authoring rules for this repository's `AGENTS.md`. This file also governs edits to itself. Instruction discovery and precedence follow the [AGENTS.md standard](https://agents.md/); an explicit user instruction takes precedence.

## What belongs here

- Put short, actionable SDK rules and exact development commands in `AGENTS.md`.
- Put contributor workflow in `CONTRIBUTING.md`, Python conventions in `docs/python-style.md`, and technical explanations in the relevant `docs/` page.
- Record a recurring mistake as a rule at the boundary that owns it; avoid speculative rules and rules tied to one person's workstation.
- Keep unreleased API proposals separate from instructions for the implemented API.

## Writing a rule

- Start each rule with an imperative verb; use `Never` for a hard prohibition and `Ask first` only for a decision that needs authorization.
- Honor authorization already present in the conversation instead of asking for it again.
- Keep each bullet to one instruction, adding one explanatory sentence only when the reason is otherwise unclear.
- Name paths and commands exactly in backticks, including the flags needed to run a check.
- Link the explanatory document from the section that needs it rather than repeating its prose.
- State which check enforces a rule when enforcement exists; distinguish automated enforcement from reviewer judgment.
- Use headings and flat bullets; keep tables, long explanations, and nested procedures in the linked documentation.
- Avoid private issue links, pull-request narration, implementation-history notes, personal paths, and credentials.

## Maintaining the guidance

- Update a rule when the code, command, or supported interface changes.
- Remove obsolete rules and duplicated instructions instead of accumulating exceptions.
- Read `AGENTS.md`, this file, and affected linked sections after an edit; resolve contradictions before finishing.
- Verify every referenced local path and command, and label any check that was not executed.
- Follow `CONTRIBUTING.md` for documentation ownership, prose, and comment review; preserve Python formatting rules when adapting guidance from another language.
