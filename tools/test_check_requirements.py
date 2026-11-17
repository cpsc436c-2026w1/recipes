"""tools/check_requirements.py: a folder's requirements and its scenario tags match, or the check fails."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_requirements as cr  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

README = """# A recipe

## Use it when

Always.

## Requirements

Each requirement has a scenario tagged with its number.

**Main scenario**

- **R10** (ubiquitous) The recipe shall name every resource with `$PREFIX`. <!--src: ours: a tag that says shall -->
- **R11** (event) When a question is asked, the recipe shall write one trace item,
  with the question and the SQL.
- **R20** (unwanted) If the call is refused, then the recipe shall exit with code 1.

## Main scenario

1. A step that shall not be read as a requirement.

## Decisions

| Decision | What we chose |
|---|---|
| Names (R10) | a prefix |
"""

EXT_README = """# An extension

## Requirements

- **R30** (optional) Where `ext-cache` is used, the recipe shall answer a stored question without a model call.
- **R31** (complex) Where `ext-cache` is used, if the cache table cannot be read, then the recipe shall call the
  model.

## Decisions

| Part (R30) | What we chose |
"""

FEATURE = """Feature: a recipe

  @R10 @R11
  Scenario: names and traces
    Given a run

  @aws @R20
  Scenario Outline: a refused call
    Given the status <status>

    Examples:
      | status |
      | 429    |
"""

EXT_FEATURE = """@R30
Feature: an extension

  Scenario: a stored question
    Given a cache

  Scenario Outline: the table cannot be read
    Given the error <error>

    @R31
    Examples:
      | error |
      | gone  |
"""


def recipe(tmp_path, readme=README, feature=FEATURE, name="a-recipe", ext=None, ext_feature=None):
    folder = tmp_path / name
    (folder / "features").mkdir(parents=True)
    (folder / "README.md").write_text(readme)
    if feature is not None:
        (folder / "features" / "a.feature").write_text(feature)
    if ext is not None:
        (folder / "ext-cache").mkdir()
        (folder / "ext-cache" / "README.md").write_text(ext)
    if ext_feature is not None:
        (folder / "features" / "ext.feature").write_text(ext_feature)
    return folder


def problems(folder):
    return cr.check_folder(folder)[0]


# ---- what passes ----

def test_matching_requirements_and_tags_pass(tmp_path, capsys):
    folder = recipe(tmp_path)
    assert problems(folder) == []
    assert cr.main([], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "a-recipe: 3 requirements (1 ubiquitous, 1 event, 1 unwanted), each with a scenario" in out


def test_an_extension_readme_continues_the_numbering(tmp_path, capsys):
    folder = recipe(tmp_path, ext=EXT_README, ext_feature=EXT_FEATURE)
    assert problems(folder) == []
    assert cr.main([], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "a-recipe: 5 requirements (1 ubiquitous, 1 event, 1 unwanted, 1 optional, 1 complex)" in out
    assert "    README.md: 3 (1 ubiquitous, 1 event, 1 unwanted)" in out
    assert "    ext-cache/README.md: 2 (1 optional, 1 complex)" in out


def test_a_requirement_is_read_with_its_form_and_its_wrapped_line(tmp_path):
    reqs = cr.requirements(cr.section(README, "Requirements"))
    assert [(n, form) for n, form, _ in reqs] == [(10, "ubiquitous"), (11, "event"), (20, "unwanted")]
    assert reqs[1][2].endswith("with the question and the SQL.")
    assert "src:" not in reqs[0][2]  # a source tag is not part of the sentence, and its words are not counted


def test_a_folder_without_the_section_is_skipped_and_listed(tmp_path, capsys):
    recipe(tmp_path)
    old = recipe(tmp_path, readme="# Old recipe\n\n## Main scenario\n\n1. A step.\n", feature=None, name="old-recipe")
    (tmp_path / "_template").mkdir()
    (tmp_path / "_template" / "README.md").write_text("## Requirements\n\n- **R10** (ubiquitous) <the sentence>\n")
    assert problems(old) is None
    assert cr.main([], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "skipped, no Requirements section yet: old-recipe" in out and "_template" not in out
    assert "1 folder(s) checked, 1 skipped" in out


def test_list_prints_where_each_requirement_is_tagged(tmp_path, capsys):
    recipe(tmp_path, ext=EXT_README, ext_feature=EXT_FEATURE)
    assert cr.main(["--list"], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "a-recipe R20 (unwanted) README.md: a.feature:7" in out
    assert "a-recipe R31 (complex) ext-cache/README.md: ext.feature:10" in out


def test_the_repo_itself_passes():
    assert cr.main([], root=ROOT) == 0


# ---- requirements and tags that do not match ----

def test_a_requirement_without_a_scenario_tag_fails(tmp_path, capsys):
    folder = recipe(tmp_path, feature=FEATURE.replace("@aws @R20", "@aws"))
    assert problems(folder) == ["R20 has no scenario: tag a scenario in features/ with @R20"]
    assert cr.main([], root=tmp_path) == 1
    assert "MISMATCH  a-recipe: R20 has no scenario" in capsys.readouterr().err


def test_a_folder_with_requirements_and_no_feature_files_fails(tmp_path):
    assert len(problems(recipe(tmp_path, feature=None))) == 3


def test_a_tag_that_names_no_requirement_fails(tmp_path):
    folder = recipe(tmp_path, feature=FEATURE.replace("@R10 @R11", "@R10 @R11 @R30"))
    assert problems(folder) == ["@R30 (a.feature:3) names a requirement the folder's READMEs do not have"]


@pytest.mark.parametrize("change", [
    ("Given the status <status>", "Given the tag @R20 in a step"),                 # in a step
    ("Feature: a recipe\n", "Feature: a recipe\n  The scenarios of @R20.\n"),      # in the Feature description
    ("Feature: a recipe\n", "Feature: a recipe\n  @R20\n  the description goes on\n"),  # a line that starts with @
    ("  @aws\n  Scenario Outline", "  # @R20\n  @aws\n  Scenario Outline"),        # in a comment
])
def test_only_a_tag_line_directly_above_a_scenario_counts(tmp_path, change):
    feature = FEATURE.replace("@aws @R20", "@aws").replace(*change)
    assert problems(recipe(tmp_path, feature=feature)) == ["R20 has no scenario: tag a scenario in features/ with @R20"]


def test_tags_above_feature_and_examples_and_on_two_lines_count(tmp_path):
    feature = "@R10\nFeature: f\n\n  @aws\n  @R11\n  Scenario Outline: s\n    Given <x>\n\n    @R20\n    Examples:\n      | x |\n"
    assert problems(recipe(tmp_path, feature=feature)) == []


# ---- sections the check would not read ----

@pytest.mark.parametrize("heading", ["## Requirements (EARS)", "### Requirements", "# Requirements", "## Requirements:"])
def test_a_requirements_heading_of_another_spelling_fails(tmp_path, capsys, heading):
    folder = recipe(tmp_path, readme=README.replace("## Requirements", heading), feature=None)
    found = problems(folder)
    assert found == [f"README.md: the heading '{heading}' is not read; write it as '## Requirements'"]
    assert cr.main([], root=tmp_path) == 1


def test_a_requirements_heading_inside_a_code_block_is_not_a_heading(tmp_path):
    readme = "# Old recipe\n\n```\n## Requirements (a sample)\n```\n"
    assert problems(recipe(tmp_path, readme=readme, feature=None)) is None


def test_feature_files_without_the_section_fail(tmp_path):
    folder = recipe(tmp_path, readme="# Old recipe\n\n## Main scenario\n\n1. A step.\n")
    assert problems(folder) == ["features/ has scenarios and README.md has no '## Requirements' section"]


def test_an_extension_section_without_one_in_the_folder_readme_fails(tmp_path):
    folder = recipe(tmp_path, readme="# Old recipe\n", feature=None, ext=EXT_README, ext_feature=EXT_FEATURE)
    assert "README.md has no '## Requirements' section, and a README below it has one" in problems(folder)


@pytest.mark.parametrize("line", [
    "- R12 (ubiquitous) The recipe shall keep no key in its output.",          # the number is not in bold
    "- **R12:** (ubiquitous) The recipe shall keep no key in its output.",     # a colon inside the bold
    "R12 is planned.",                                                         # a note
    "| R12 | planned |",                                                       # a table row
])
def test_a_line_that_names_a_requirement_and_is_not_one_fails(tmp_path, line):
    readme = README.replace("- **R20**", "\n" + line + "\n\n- **R20**")
    found = problems(recipe(tmp_path, readme=readme))
    assert len(found) == 1 and found[0].startswith("README.md: this line of the Requirements section names a")


def test_an_empty_requirements_section_fails(tmp_path):
    readme = "# A recipe\n\n## Requirements\n\nLater.\n\n## Main scenario\n"
    assert "has no requirement" in problems(recipe(tmp_path, readme=readme, feature=None))[0]


# ---- numbering ----

@pytest.mark.parametrize("numbers,problem", [
    ([10, 11, 20], None),
    ([10, 20, 21, 22, 30], None),
    ([1, 2], "R1 is not under R0"),
    ([10, 30], "R30 is out of order: the next requirement in steps of ten is R20"),
    ([20, 30], "R20 is out of order: the next requirement in steps of ten is R10"),
    ([10, 12], "R12 is out of order: the next sub-requirement of R10 is R11"),
    ([10, 20, 11], "R11 is not under R10"),
    ([10, 21], "R21 is not under R20"),
    ([10, 20, 20], "R20 is used 2 times"),
    ([10, 11, 11], "R11 is used 2 times"),
])
def test_numbering_follows_the_course_rule(numbers, problem):
    found = cr.numbering_problems(numbers)
    assert (found == []) if problem is None else any(p.startswith(problem) for p in found), found


def test_numbering_is_checked_in_the_readme_and_across_readmes(tmp_path):
    folder = recipe(tmp_path, readme=README.replace("**R20**", "**R30**"), feature=FEATURE.replace("@R20", "@R30"))
    assert problems(folder) == ["R30 is out of order: the next requirement in steps of ten is R20"]
    gap = recipe(tmp_path, ext=EXT_README.replace("R30", "R40").replace("R31", "R41"), name="b",
                 ext_feature=EXT_FEATURE.replace("R30", "R40").replace("R31", "R41"))
    assert problems(gap) == ["R40 is out of order: the next requirement in steps of ten is R30"]


# ---- one shall, the subject, the form ----

def test_one_shall_in_any_capitals(tmp_path):
    two = README.replace("then the recipe shall exit with code 1.", "then the recipe shall print it and shall exit.")
    assert problems(recipe(tmp_path, readme=two)) == ["R20 has 2 'shall'; a requirement has exactly one"]
    capital = README.replace("then the recipe shall exit with code 1.", "then the recipe shall exit. It Shall print.")
    assert problems(recipe(tmp_path, readme=capital, name="b")) == ["R20 has 2 'shall'; a requirement has exactly one"]
    none = README.replace("the recipe shall exit with code 1.", "the recipe exits with code 1.")
    assert problems(recipe(tmp_path, readme=none, name="c")) == ["R20 has 0 'shall'; a requirement has exactly one"]


def test_the_subject_is_the_recipe(tmp_path):
    readme = README.replace("then the recipe shall exit", "then `clean_up.py` shall exit")
    assert problems(recipe(tmp_path, readme=readme)) == [
        "R20 does not say 'the recipe shall'; the subject of every requirement is the recipe"]


@pytest.mark.parametrize("sentence,form", [
    ("The recipe shall name every resource with `$PREFIX`.", "ubiquitous"),
    ("When a question ends, the recipe shall write one trace item.", "event"),
    ("While a table is being deleted, the recipe shall report it as absent.", "state"),
    ("If the call is refused, then the recipe shall exit with code 1.", "unwanted"),
    ("Where `ext-cache` is used, the recipe shall store a result only when it is an answer.", "optional"),
    ("While a table is being deleted, when a run starts, the recipe shall wait.", "complex"),
    ("Where `ext-cache` is used, if a read fails, then the recipe shall call the model.", "complex"),
    ("If the call is refused, the recipe shall exit with code 1.", None),           # If without then
    ("After a run, the recipe shall print the cost.", None),                        # no EARS keyword
    ("Every resource the recipe shall name with `$PREFIX`.", None),
    ("The script shall exit.", None),
])
def test_the_form_a_sentence_has(sentence, form):
    assert cr.form_of(sentence) == form


def test_the_label_is_checked_against_the_sentence(tmp_path):
    wrong = README.replace("(unwanted) If", "(event) If")
    assert problems(recipe(tmp_path, readme=wrong)) == [
        "R20 is labelled event, and its sentence has the form unwanted (run with --help for the keywords of each form)"]
    no_then = README.replace("then the recipe shall exit", "the recipe shall exit")
    assert "has none of the six forms" in problems(recipe(tmp_path, readme=no_then, name="b"))[0]
    unknown = README.replace("(unwanted)", "(error)")
    assert "does not name its EARS form" in problems(recipe(tmp_path, readme=unknown, name="c"))[0]


# ---- references and source tags ----

def test_a_readme_may_name_only_requirements_the_folder_has(tmp_path):
    folder = recipe(tmp_path, readme=README.replace("Names (R10)", "Names (R10, R40)"))
    assert problems(folder) == ["README.md names R40, which the folder's READMEs do not have"]
    ext = recipe(tmp_path, ext=EXT_README.replace("Part (R30)", "Part (R30, R99)"), ext_feature=EXT_FEATURE, name="b")
    assert problems(ext) == ["ext-cache/README.md names R99, which the folder's READMEs do not have"]
    across = recipe(tmp_path, readme=README.replace("Names (R10)", "Names (R10, R31)"), ext=EXT_README,
                    ext_feature=EXT_FEATURE, name="c")
    assert problems(across) == []  # the folder README may name a requirement of an extension


@pytest.mark.parametrize("tag,ok", [
    ("<!--src: receipt: https://docs.aws.amazon.com/x.html read 2026-11-13 -->", True),
    ("<!--src: measured: output/main.txt -->", True),
    ("<!--src: ours: enough for a brief throttle -->", True),
    ("<!--src: assumed -->", True),
    ("<!--src: unchecked -->", True),
    ("<!--src: facts: sandbox_budget -->", True),
    ("<!--src: https://docs.aws.amazon.com/x.html read 2026-11-13 -->", False),
    ("<!--src: main/model.py PRICES_CAD -->", False),
    ("<!--src: ours, main/engine.py -->", False),
])
def test_a_source_tag_starts_with_a_course_token(tmp_path, tag, ok):
    found = problems(recipe(tmp_path, readme=README + f"\nA price. {tag}\n"))
    assert (found == []) if ok else (len(found) == 1 and "does not start with one of receipt:" in found[0]), found


@pytest.mark.parametrize("tag,pointer", [
    ("<!--src: ours: main/engine.py MAX_ROWS -->", "main/engine.py"),
    ("<!--src: ours: lib.sh -->", "lib.sh"),
    ("<!--src: ours: the limit in MAX_ROWS -->", "MAX_ROWS"),
    ("<!--src: ours: ext-lag-alarm/lag -->", "ext-lag-alarm/lag"),
    ("<!--src: ours: short enough for one screen -->", None),
    ("<!--src: ours: 1 GB / 4 KB per unit / 2 for eventual reads; 0.1375 USD per million -->", None),
    ("<!--src: ours: 100 units by the rules of https://docs.aws.amazon.com/x/GSI.html -->", None),
    ("<!--src: ours: VACUUM an hour after OPTIMIZE, in JSON -->", None),
])
def test_an_ours_tag_gives_a_reason_and_points_at_no_code(tmp_path, tag, pointer):
    found = problems(recipe(tmp_path, readme=README + f"\nA choice. {tag}\n"))
    assert (found == []) if pointer is None else (len(found) == 1 and f"points at code ({pointer})" in found[0]), found


@pytest.mark.parametrize("tag,ok", [
    ("<!--src: receipt: https://docs.aws.amazon.com/x.html read 2026-11-13 -->", True),
    ("<!--src: receipt: output/main.txt -->", True),
    ("<!--src: receipt: ../output/ext-cache.txt, the last line -->", True),
    ("<!--src: receipt: ../llm-gateway/output/main-offline.txt shows 429 -->", True),
    ("<!--src: receipt: ../llm-gateway/main/app.py -->", False),
    ("<!--src: receipt: the three API pages, read 2026-11-13 -->", False),
    ("<!--src: receipt: main/handler.py send -->", False),
    ("<!--src: receipt: DDIA 2e ch. 8 -->", False),
])
def test_a_receipt_names_a_url_or_a_captured_output(tmp_path, tag, ok):
    found = problems(recipe(tmp_path, readme=README + f"\nA fact. {tag}\n"))
    assert (found == []) if ok else (len(found) == 1 and "names neither a URL nor a captured output" in found[0]), found

