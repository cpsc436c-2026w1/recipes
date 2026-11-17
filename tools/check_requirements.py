#!/usr/bin/env python3
"""Keep each recipe's requirements and its behave scenarios matched.

    python3 tools/check_requirements.py            # exit 1 when a folder's requirements and scenarios do not match
    python3 tools/check_requirements.py --list     # each requirement and the scenarios tagged with it

A recipe's requirements are under the heading `## Requirements` in the folder README, and those of an extension
under the same heading in `ext-<name>/README.md`. Each requirement is one list item that starts with its number in
bold, then its EARS form in brackets, then one sentence whose subject is "the recipe", with one "shall":

    - **R20** (unwanted) If the SQL is not one SELECT, then the recipe shall return the rejection to the LLM.

The scenarios are in `<folder>/features/*.feature`. A scenario names the requirements it checks in the tag lines
directly above `Scenario:`, `Scenario Outline:`, `Examples:` or `Feature:` (`@R20 @R21`). `uv run behave` in the
folder runs them.

This check fails when, in a folder that has the heading:
- a requirement has no scenario tagged with it, or a tag names a requirement no README of the folder has;
- the numbering breaks the course rule: R10, R20, R30 ... in steps of ten, in order, with no number left out;
  sub-requirements R11, R12 ... right after their R10, in order, with no number left out. The READMEs of a folder
  are read as one list, the folder README first, then the others in the order of their first number;
- a requirement does not have exactly one "shall" (in any capitals), does not say "the recipe shall", names no EARS
  form, or names a form its sentence does not have:
      ubiquitous  The recipe shall ...
      event       When ..., the recipe shall ...
      state       While ..., the recipe shall ...
      unwanted    If ..., then the recipe shall ...
      optional    Where ..., the recipe shall ...
      complex     two or more of When, While, If (with then), Where before "the recipe shall";
- a line of a Requirements section names a requirement (R20) without being a requirement item, for example an item
  whose number is not in bold;
- a README of the folder names, outside its Requirements section, a requirement the folder does not have;
- a source tag `<!--src: ... -->` in a README of the folder does not start with one of the course's tokens:
  receipt:, facts:, measured:, ours:, unchecked, assumed;
- an `ours:` tag points at code instead of giving the reason for the choice: its text names a file path
  (`main/engine.py`), a file (`run.sh`) or an UPPER_CASE constant (`MAX_ROWS`). Write "ours: enough for a brief
  throttle";
- a `receipt:` tag names neither a public URL (it starts with http:// or https://) nor a captured output
  (`output/...`, or another recipe's `../<recipe>/output/...`).

It also fails, in every folder, when a heading starts with "Requirements" and is not exactly `## Requirements`
(a section the check would not read), and when a folder has `features/*.feature` and its README has no
Requirements section. A folder with neither is skipped and listed, so a recipe without requirements does not
fail.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORMS = ("ubiquitous", "event", "state", "unwanted", "optional", "complex")
KEYWORD_FORM = {"when": "event", "while": "state", "if": "unwanted", "where": "optional"}
SOURCE_TOKENS = ("receipt:", "facts:", "measured:", "ours:", "unchecked", "assumed")
SCENARIO_KEYWORDS = ("Feature:", "Rule:", "Scenario:", "Scenario Outline:", "Scenario Template:", "Examples:")
ITEM = re.compile(r"^\s*[-*]\s+\*\*R(\d+)\*\*\s*(?:\(([a-z]+)\))?(.*)$")
TAG = re.compile(r"@R(\d+)\b")
REF = re.compile(r"\bR(\d+)\b")
HEADING = re.compile(r"^(#{1,6})\s+(Requirements\b.*?)\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.S)
SOURCE_TAG = re.compile(r"<!--\s*src:\s*(.*?)\s*-->", re.S)
# What an `ours:` tag must not hold: a path (word/word, not part of a URL and not " / " in a sum), a file name,
# a constant in capitals with an underscore.
CODE_POINTER = re.compile(r"(?<![\w/:.])[\w.\-]+/[\w.\-/]+|\b[\w\-]+\.(?:py|sh|sql|toml|json|ya?ml)\b|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
RECEIPT_OK = re.compile(r"receipt:\s*(?:https?://|(?:\.\./(?:[\w\-]+/)?)?output/)")   # also another recipe's, ../llm-gateway/output/


def section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    """(first line, line after the last) of the text under `## <heading>`, up to the next `## ` heading."""
    for i, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return i + 1, end
    return None


def section(readme: str, heading: str) -> str | None:
    """The text under `## <heading>`, or None when the README has no such heading."""
    lines = readme.splitlines()
    span = section_span(lines, heading)
    return None if span is None else "\n".join(lines[span[0]:span[1]])


def without_comments(text: str) -> str:
    """The text with HTML comments (source tags) taken out; line breaks inside a comment are kept."""
    return COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def read_section(text: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    """(requirements, stray lines) of a Requirements section. A requirement is (number, form, sentence); its
    sentence runs to the next list item, blank line, heading or table row. A stray line names a requirement
    (R20) and is neither a requirement item nor the rest of one."""
    found: list[list] = []
    stray: list[str] = []
    current: list | None = None
    in_code = False
    for line in without_comments(text).splitlines():
        if line.strip().startswith("```"):
            in_code, current = not in_code, None
            continue
        if in_code:
            continue
        m = ITEM.match(line)
        if m:
            current = [int(m.group(1)), m.group(2) or "", m.group(3).strip()]
            found.append(current)
        elif current is not None and line.strip() and not re.match(r"^\s*([-*]\s|#|\|)", line):
            current[2] += " " + line.strip()
        else:
            current = None
            if REF.search(line):
                stray.append(line.strip())
    return [(n, form, sentence) for n, form, sentence in found], stray


def requirements(text: str) -> list[tuple[int, str, str]]:
    return read_section(text)[0]


def form_of(sentence: str) -> str | None:
    """The EARS form a sentence has, from the keywords before "the recipe shall"; None when it has no form."""
    m = re.search(r"\bthe recipe shall\b", sentence, flags=re.I)
    if not m:
        return None
    before = sentence[:m.start()].strip()
    if not before:
        return "ubiquitous"
    first = re.match(r"[A-Za-z]+", before)
    keywords = {w.lower() for w in re.findall(r"\b(when|while|if|where)\b", before, flags=re.I)}
    if not first or first.group(0).lower() not in KEYWORD_FORM:
        return None
    if "if" in keywords and not re.search(r"\bthen$", before, flags=re.I):
        return None  # If ... then
    return "complex" if len(keywords) > 1 else KEYWORD_FORM[first.group(0).lower()]


def scenario_tags(folder: pathlib.Path) -> dict[int, list[str]]:
    """Requirement number -> where it is tagged ("main.feature:12"). Only the tag lines directly above Feature,
    Rule, Scenario, Scenario Outline or Examples count: a tag in a description, a step or a comment does not."""
    tags: dict[int, list[str]] = {}
    for path in sorted((folder / "features").glob("**/*.feature")):
        lines = [line.strip() for line in path.read_text().splitlines()]
        for i, line in enumerate(lines):
            if not line.startswith(SCENARIO_KEYWORDS):
                continue
            j = i - 1
            while j >= 0 and lines[j].startswith("@"):
                for m in TAG.finditer(lines[j]):
                    tags.setdefault(int(m.group(1)), []).append(f"{path.name}:{j + 1}")
                j -= 1
    return tags


def numbering_problems(numbers: list[int]) -> list[str]:
    problems = []
    for n in sorted({n for n in numbers if numbers.count(n) > 1}):
        problems.append(f"R{n} is used {numbers.count(n)} times")
    expected_top, last_top, last_sub = 10, None, None
    for n in dict.fromkeys(numbers):  # each number once, in the order written
        if n % 10 == 0:
            if n != expected_top:
                problems.append(f"R{n} is out of order: the next requirement in steps of ten is R{expected_top}")
            last_top, last_sub, expected_top = n, n, max(expected_top, n) + 10
        else:
            parent = n - n % 10
            if last_top != parent:
                problems.append(f"R{n} is not under R{parent}: a sub-requirement follows its own R{parent}")
            elif n != last_sub + 1:
                problems.append(f"R{n} is out of order: the next sub-requirement of R{parent} is R{last_sub + 1}")
            last_sub = n
    return problems


def readmes(folder: pathlib.Path) -> list[pathlib.Path]:
    """The folder README, then the README of each folder one level down (an extension, a representative)."""
    below = sorted(p for p in folder.glob("*/README.md") if not p.parent.name.startswith((".", "_")))
    return [folder / "README.md", *below]


def check_folder(folder: pathlib.Path) -> tuple[list[str] | None, dict]:
    """(problems, details) for one recipe folder. problems is None when the folder has nothing to check: no
    Requirements heading of any spelling and no feature files."""
    problems: list[str] = []
    sections: list[tuple[str, list, str]] = []   # (README name, its requirements, the README outside the section)
    texts: dict[str, str] = {}
    for path in readmes(folder):
        name = str(path.relative_to(folder))
        text = texts[name] = path.read_text()
        lines = text.splitlines()
        in_code = False
        for line in lines:
            in_code ^= line.strip().startswith("```")
            m = HEADING.match(line)
            if m and not in_code and line.strip() != "## Requirements":
                problems.append(f"{name}: the heading '{line.strip()}' is not read; write it as '## Requirements'")
        span = section_span(lines, "Requirements")
        if span is None:
            continue
        reqs, stray = read_section("\n".join(lines[span[0]:span[1]]))
        for line in stray:
            problems.append(f"{name}: this line of the Requirements section names a requirement and is not one; "
                            f"a requirement starts with '- **R10** (form)': {line[:80]}")
        if not reqs:
            problems.append(f"{name}: the Requirements section has no requirement "
                            f"(a list item that starts with **R10**)")
        sections.append((name, reqs, "\n".join(lines[:span[0]] + lines[span[1]:])))
    has_features = any((folder / "features").glob("**/*.feature"))
    if not sections:
        if has_features:
            problems.append("features/ has scenarios and README.md has no '## Requirements' section")
        return (problems or None), {}
    if sections[0][0] != "README.md":
        problems.append("README.md has no '## Requirements' section, and a README below it has one")
    ordered = [sections[0]] + sorted(sections[1:], key=lambda s: s[1][0][0] if s[1] else 0)
    reqs = [(n, form, sentence, name) for name, items, _ in ordered for n, form, sentence in items]
    numbers = [n for n, _, _, _ in reqs]
    tags = scenario_tags(folder)
    problems += numbering_problems(numbers)
    for n, form, sentence, _ in reqs:
        shalls = len(re.findall(r"\bshall\b", sentence, flags=re.I))
        found = form_of(sentence)
        if shalls != 1:
            problems.append(f"R{n} has {shalls} 'shall'; a requirement has exactly one")
        elif not re.search(r"\bthe recipe shall\b", sentence, flags=re.I):
            problems.append(f"R{n} does not say 'the recipe shall'; the subject of every requirement is the recipe")
        if form not in FORMS:
            problems.append(f"R{n} does not name its EARS form in brackets after the number; "
                            f"the forms are {', '.join(FORMS)}")
        elif shalls == 1 and found != form and re.search(r"\bthe recipe shall\b", sentence, flags=re.I):
            problems.append(f"R{n} is labelled {form}, and its sentence "
                            f"{'has the form ' + found if found else 'has none of the six forms'} "
                            f"(run with --help for the keywords of each form)")
        if n not in tags:
            problems.append(f"R{n} has no scenario: tag a scenario in features/ with @R{n}")
    for n in sorted(set(tags) - set(numbers)):
        problems.append(f"@R{n} ({', '.join(tags[n])}) names a requirement the folder's READMEs do not have")
    for name, text in texts.items():
        outside = next((rest for section_name, _, rest in sections if section_name == name), text)
        for n in sorted({int(m.group(1)) for m in REF.finditer(without_comments(outside))} - set(numbers)):
            problems.append(f"{name} names R{n}, which the folder's READMEs do not have")
        for m in SOURCE_TAG.finditer(text):
            if not m.group(1).startswith(SOURCE_TOKENS):
                problems.append(f"{name}: the source tag 'src: {m.group(1)[:50]}' does not start with one of "
                                f"{', '.join(SOURCE_TOKENS)}")
            elif m.group(1).startswith("ours:") and (pointer := CODE_POINTER.search(m.group(1)[5:])):
                problems.append(f"{name}: the source tag 'src: {m.group(1)[:50]}' points at code ({pointer.group(0)}); "
                                f"an ours: tag gives the reason for the choice")
            elif m.group(1).startswith("receipt:") and not RECEIPT_OK.match(m.group(1)):
                problems.append(f"{name}: the source tag 'src: {m.group(1)[:50]}' names neither a URL nor a captured "
                                f"output (output/...)")
    return problems, {"requirements": reqs, "tags": tags, "sections": ordered}


def recipe_folders(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p.parent for p in root.glob("*/README.md") if not p.parent.name.startswith(("_", ".")))


def count_by_form(reqs) -> str:
    counts = {f: sum(1 for r in reqs if r[1] == f) for f in FORMS}
    return ", ".join(f"{count} {form}" for form, count in counts.items() if count)


def main(argv: list[str] | None = None, root: pathlib.Path = ROOT) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="print each requirement and where it is tagged")
    args = p.parse_args(argv)
    failed, checked, skipped = 0, [], []
    for folder in recipe_folders(root):
        problems, details = check_folder(folder)
        if problems is None:
            skipped.append(folder.name)
            continue
        checked.append(folder.name)
        reqs = details.get("requirements", [])
        if args.list:
            for n, form, _, name in reqs:
                where = ", ".join(details["tags"].get(n, ["no scenario"]))
                print(f"{folder.name} R{n} ({form}) {name}: {where}")
        for line in problems:
            print(f"MISMATCH  {folder.name}: {line}", file=sys.stderr)
        failed += len(problems)
        if not problems:
            print(f"{folder.name}: {len(reqs)} requirements ({count_by_form(reqs)}), each with a scenario")
            for name, items, _ in details["sections"]:
                print(f"    {name}: {len(items)} ({count_by_form(items)})")
    if skipped:
        print(f"skipped, no Requirements section yet: {', '.join(skipped)}")
    if failed:
        print(f"\n{failed} problem(s). Change the README's Requirements and the scenario tags together.",
              file=sys.stderr)
        return 1
    print(f"check_requirements: {len(checked)} folder(s) checked, {len(skipped)} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
