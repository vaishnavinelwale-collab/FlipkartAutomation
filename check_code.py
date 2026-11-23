"""Quick self-check for the scraper files. Run:  python check_code.py

Reports (1) syntax / indentation errors, (2) functions defined twice in one file (a sign that code was
pasted twice), (3) names that are used but never defined (needs `pip install pyflakes`, optional).
It only reads the files; it changes nothing."""
import ast
import collections
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
bad = 0
for f in sorted(HERE.glob("*.py")):
    src = f.read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(src, str(f))
    except SyntaxError as exc:
        print(f"SYNTAX ERROR  {f.name}  line {exc.lineno}: {exc.msg}")
        bad += 1
        continue
    dup = [n for n, c in collections.Counter(
        x.name for x in tree.body if isinstance(x, (ast.FunctionDef, ast.ClassDef))).items() if c > 1]
    lines = src.count("\n") + 1
    note = f"  <-- DEFINED TWICE: {', '.join(dup)} (file pasted twice?)" if dup else ""
    print(f"ok            {f.name} ({lines} lines){note}")
    bad += bool(dup)
try:
    from pyflakes.api import check
    from pyflakes.reporter import Reporter

    class R(Reporter):
        def unexpectedError(self, filename, msg):
            print("pyflakes:", filename, msg)

    for f in sorted(HERE.glob("*.py")):
        import io
        out = io.StringIO()
        check(f.read_text(encoding="utf-8-sig"), f.name, Reporter(out, out))
        for line in out.getvalue().splitlines():
            if "undefined name" in line:
                print("UNDEFINED     " + line)
                bad += 1
except ImportError:
    print("(install pyflakes to also check for undefined names)")
print("ALL GOOD" if not bad else f"{bad} problem(s) found")
sys.exit(1 if bad else 0)
