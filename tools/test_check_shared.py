"""tools/check_shared.py: copies that differ in code fail; copies that differ in comments or docstrings pass."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_shared  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

A = '''
import re

def _norm(q: str) -> str:
    """Lower case, one space between words."""
    return re.sub(r"\\s+", " ", q.strip().lower())   # a comment
'''
A_OTHER_WORDS = '''
import re
def _norm(q: str) -> str:
    return re.sub(r"\\s+", " ",
                  q.strip().lower())
'''
A_DRIFTED = '''
import re
def _norm(q: str) -> str:
    return re.sub(r"\\s+", " ", q.strip())
'''


def write(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)


def helper(*files, **more):
    return [{"name": "_norm", "files": list(files), **more}]


def test_comments_docstrings_and_line_breaks_may_differ(tmp_path):
    write(tmp_path, **{"a.py": A, "b.py": A_OTHER_WORDS})
    problems, notes = check_shared.check(helper("a.py", "b.py"), tmp_path)
    assert problems == [] and notes == ["_norm: 2 copies, the same"]


def test_a_copy_whose_code_differs_fails_and_the_difference_is_shown(tmp_path):
    write(tmp_path, **{"a.py": A, "b.py": A_DRIFTED})
    problems, _ = check_shared.check(helper("a.py", "b.py"), tmp_path)
    assert len(problems) == 1
    assert "b.py differs from the one in a.py" in problems[0]
    assert "-    return re.sub('\\\\s+', ' ', q.strip().lower())" in problems[0]


def test_a_listed_file_without_the_helper_fails_unless_optional(tmp_path):
    write(tmp_path, **{"a.py": A, "b.py": "x = 1\n"})
    problems, _ = check_shared.check(helper("a.py", "b.py"), tmp_path)
    assert problems == ["_norm: b.py is listed in the allowlist and does not define it"]
    assert check_shared.check(helper("a.py", "b.py", optional=True), tmp_path)[0] == []


def test_a_listed_file_that_is_missing_fails_even_when_optional(tmp_path):
    write(tmp_path, **{"a.py": A})
    problems, _ = check_shared.check(helper("a.py", "gone.py", optional=True), tmp_path)
    assert problems == ["_norm: gone.py is listed in the allowlist and does not exist"]


def test_a_nested_helper_and_a_constant_are_found(tmp_path):
    nested = "def outer():\n    def gone(what):\n        return what\n    return gone\nGONE = {'a', 'b'}\n"
    write(tmp_path, **{"a.py": nested, "b.py": nested.replace("{'a', 'b'}", "{'a'}")})
    assert check_shared.check([{"name": "gone", "files": ["a.py", "b.py"]}], tmp_path)[0] == []
    assert len(check_shared.check([{"name": "GONE", "files": ["a.py", "b.py"]}], tmp_path)[0]) == 1


def test_a_method_is_not_taken_for_the_module_function(tmp_path):
    with_method = "def cost(x):\n    return x\nclass Reply:\n    def cost(self):\n        return 0\n"
    write(tmp_path, **{"a.py": with_method, "b.py": "def cost(x):\n    return x\n"})
    assert check_shared.check([{"name": "cost", "files": ["a.py", "b.py"]}], tmp_path)[0] == []


def test_shell_functions(tmp_path):
    one = 'run() { echo; echo "\\$ $*"; [ -n "${DRY:-}" ] || "$@"; }\n'
    many = "finish() {\n  # last step\n  if [ -n \"${KEEP:-}\" ]; then\n    say kept   # stays\n  fi\n}\n"
    write(tmp_path, **{"a.sh": one + many, "b.sh": many.replace("  # last step\n", "") + one,
                       "c.sh": one.replace(' [ -n "${DRY:-}" ] ||', "")})
    sh = lambda name, *files: [{"name": name, "kind": "shell", "files": list(files)}]  # noqa: E731
    assert check_shared.check(sh("run", "a.sh", "b.sh"), tmp_path)[0] == []
    assert check_shared.check(sh("finish", "a.sh", "b.sh"), tmp_path)[0] == []
    assert len(check_shared.check(sh("run", "a.sh", "c.sh"), tmp_path)[0]) == 1   # the copy that lost the DRY test


def test_a_second_definition_of_a_shell_function_fails(tmp_path):
    one = 'run() { echo; echo "\\$ $*"; [ -n "${DRY:-}" ] || "$@"; }\n'
    write(tmp_path, **{"a.sh": one, "b.sh": one + 'say() { echo; }\nrun() { "$@"; }\n'})
    problems, _ = check_shared.check([{"name": "run", "kind": "shell", "files": ["a.sh", "b.sh"]}], tmp_path)
    assert problems == ["run: b.sh defines the function run 2 times; keep one definition"]


def test_a_second_module_level_definition_fails(tmp_path):
    ms = "def _ms(t):\n    return int(t * 1000)\n"
    write(tmp_path, **{"a.py": ms, "b.py": ms + "def _ms(t):\n    return 0\n",
                       "c.py": ms + "_ms = lambda t: 0\n", "d.py": "_ms: int = 1\n_ms += 1\n"})
    for other in ("b.py", "c.py", "d.py"):
        problems, _ = check_shared.check([{"name": "_ms", "files": ["a.py", other]}], tmp_path)
        assert problems == [f"_ms: {other} defines _ms 2 times at module level; keep one definition"]


def test_a_hash_inside_a_shell_string_is_code(tmp_path):
    write(tmp_path, **{"a.sh": 'say() { echo "x # a"; }\n', "b.sh": 'say() { echo "x # b"; }\n',
                       "c.sh": '# heading\nsay() { echo "x # a"; }\n'})
    sh = lambda *files: [{"name": "say", "kind": "shell", "files": list(files)}]  # noqa: E731
    assert len(check_shared.check(sh("a.sh", "b.sh"), tmp_path)[0]) == 1
    assert check_shared.check(sh("a.sh", "c.sh"), tmp_path)[0] == []


def test_a_helper_with_fewer_than_two_copies_is_not_called_the_same(tmp_path):
    write(tmp_path, **{"a.py": A, "b.py": "x = 1\n"})
    problems, notes = check_shared.check(helper("a.py", "b.py", optional=True), tmp_path)
    assert problems == [] and notes == ["_norm: copies found in 1 of 2 listed files, nothing to compare"]


def test_the_repo_passes_and_every_listed_file_exists():
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "check_shared.py")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
