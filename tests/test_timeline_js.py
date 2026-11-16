"""Logika výběru termínu tažením na časové ose (src/charging/static/timeline.js), spuštěná přes node.
Když node není nainstalovaný, test se přeskočí (CI na GitHubu ho má)."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "charging" / "static" / "timeline.js"

CHECKS = r"""
const t = require(process.argv[1]);
const F = true, B = false;
const free = [F, F, F, B, F, F, F, F, F, F, F, F];   // slot 3 je obsazený
const cases = [
  [t.tap(null, 0, free, 8), {start: 0, end: 0}, "klepnutí = začátek"],
  [t.tap({start: 0, end: 0}, 2, free, 8), {start: 0, end: 2}, "klepnutí na pozdější slot prodlouží"],
  [t.tap({start: 0, end: 2}, 4, free, 8), {start: 4, end: 4}, "přes obsazený slot nejde, začne nový výběr"],
  [t.tap({start: 0, end: 2}, 3, free, 8), {start: 0, end: 2}, "obsazený slot nic nezmění"],
  [t.tap({start: 4, end: 6}, 1, free, 8), {start: 1, end: 1}, "slot před začátkem = nový výběr"],
  [t.drag({start: 4, end: 4}, 11, free, 8), {start: 4, end: 11}, "tažení až do limitu 8 slotů"],
  [t.drag({start: 4, end: 4}, 11, free, 7), {start: 4, end: 4}, "přes limit se konec neposune"],
  [t.drag({start: 4, end: 9}, 6, free, 8), {start: 4, end: 6}, "tažení zpět výběr zkrátí"],
  [t.drag({start: 4, end: 6}, 2, free, 8), {start: 4, end: 6}, "tažení před začátek nic"],
  [t.rangeOk(free, 0, 3, 8), false, "úsek s obsazeným slotem není volný"],
];
let failed = 0;
for (const [actual, expected, name] of cases) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) { failed++; console.log("FAIL", name, actual); }
}
process.exit(failed);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node není nainstalovaný")
def test_timeline_selection_logic():
    result = subprocess.run(["node", "-e", CHECKS, str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
