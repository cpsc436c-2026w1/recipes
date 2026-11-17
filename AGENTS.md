# Rules for coding assistants working from these recipes

These recipes are for CPSC 436C project teams. Students read them, and so do coding assistants acting for them. If
you are an assistant, follow these rules when you use a recipe or change one.

## Find the right recipe

- `index.json` lists every recipe: what it builds, the project shapes and extensions it serves, the practices it
  teaches and their sources, the reusable files, and how to validate it. `tools/build_index.py` rebuilds it.
- The folder `README.md` is the recipe. Read its **Decisions** table before changing code: each row says what was
  chosen, why, and which course constraint or best practice it follows.

## Keep the course constraints

- Region `ca-central-1`. Every resource name starts with `$PREFIX`. Every recipe has a clean-up step; run it after a
  real run, so nothing keeps billing in the team's sandbox.
- No keys, tokens or account numbers in code, commits or outputs. Model calls go through the course LLM gateway with
  the student's own course token, read from `KB_MCP_TOKEN` and the address from `COURSE_GATEWAY_URL` in the
  environment. Keep `COURSE_MCP_TOKEN` and `GATEWAY_URL` as the fallback when the new names are not set.
- Offline first: `uv sync && uv run pytest -q` passes without an AWS account. Run against AWS only when the student
  asks for it, and say what it costs (the README's **Cost and clean-up** section).
- When you change a decision, change the README's Decisions row with it, and keep the source of every practice.
- Where a README has a **Requirements** section, a change in what the recipe does changes the requirement (one
  sentence about "the recipe", one "shall", numbered R10, R20 ... with R11, R12 ... under them; an extension's are
  in its own README) and its behave scenario in `features/`, tagged with the number. `python3 tools/check_requirements.py` and `uv run behave` in the folder must pass.
- A recipe folder is self-contained, so a helper two recipes need is copied, not imported. `tools/shared_helpers.toml`
  names the copies that must stay the same; `python3 tools/check_shared.py` fails when they differ. Change every copy
  or none.

## Help the student understand

- Explain changes in terms of the decision and the practice behind it, not only the code.
- Do not present a recipe's example values (limits, prices, thresholds) as rules. Some README values are marked
  "measured" with the captured run in `output/`, an AWS documentation link, "course default", or "assumed"; others
  carry no mark.
- Students are graded on their own decisions and their process. Show your reasoning so they can check it; do not
  make design choices for them silently.

