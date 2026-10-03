"""Run every test: tests/test_*.py and the site's node test.

    .venv\\Scripts\\python.exe tools\\run_tests.py                       on SQLite (a throw-away file per test)
    .venv\\Scripts\\python.exe tools\\run_tests.py --pg postgresql://postgres:PASSWORD@127.0.0.1:5432/postgres
                                                   --pg-bin C:\\pgsql\\bin
                                                                       on PostgreSQL as well: each test file gets
                                                                       its own new database, dropped afterwards
    ... tools\\run_tests.py test_money_flow test_db                       only these

Output of a failed test is printed in full; a passed one shows its last line.
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def admin_url(url: str) -> str:
    return "postgresql+asyncpg://" + url.split("://", 1)[1] if url.startswith(("postgres://", "postgresql://")) else url


async def _pg(url: str, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def run_one(path: Path, env: dict) -> tuple[bool, str, float]:
    t0 = time.monotonic()
    cmd = ["node", str(path)] if path.suffix == ".mjs" else [PY, str(path)]
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=900)
    return p.returncode == 0, (p.stdout + p.stderr).strip(), time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="test names (test_money_flow …); default: all")
    ap.add_argument("--pg", help="a PostgreSQL URL with the right to create databases")
    ap.add_argument("--pg-only", action="store_true", help="skip the SQLite round")
    ap.add_argument("--pg-bin", help="PostgreSQL's bin folder (pg_dump / pg_restore for the backup test)")
    a = ap.parse_args()
    if a.pg_bin:
        os.environ["PG_BIN"] = a.pg_bin

    tests = sorted((ROOT / "tests").glob("test_*.py")) + sorted((ROOT / "tests").glob("test_*.mjs"))
    if a.only:
        tests = [t for t in tests if t.stem in a.only or t.name in a.only]
    rounds = ([] if a.pg_only else [None]) + ([a.pg] if a.pg else [])
    failed = []
    for pg in rounds:
        print(f"\n=== {'PostgreSQL' if pg else 'SQLite'} ===")
        for t in tests:
            if pg and t.suffix == ".mjs":
                continue  # the site test has no database
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "DATABASE_URL": ""}
            db = None
            if pg:
                db = f"sh_test_{uuid.uuid4().hex[:10]}"
                asyncio.run(_pg(admin_url(pg), f'CREATE DATABASE "{db}"'))
                env["DATABASE_URL"] = pg.rsplit("/", 1)[0] + "/" + db
            try:
                ok, out, sec = run_one(t, env)
            finally:
                if db:
                    asyncio.run(_pg(admin_url(pg), f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            lines = out.splitlines()
            last = next((x for x in reversed(lines) if x.startswith("ALL GOOD")), lines[-1] if lines else "")
            print(f"{'ok ' if ok else 'FAIL'} {t.name:28} {sec:5.0f}s  {last[:110]}")
            if not ok:
                failed.append(f"{t.name} ({'PostgreSQL' if pg else 'SQLite'})")
                print("\n".join("     " + line for line in out.splitlines()[-40:]))
    print(f"\n{'ALL PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
