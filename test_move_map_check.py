import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("mmc", Path(__file__).parent / "move-map-check.py")
mmc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mmc)


def setup(tmp, dest_text, rows):
    src = tmp / "src.md"
    src.write_text("# Top\nFirst sentence here is long enough. Second one is also long.\n## Sub\nAnother sentence that is long.\n")
    (tmp / "staging").mkdir()
    (tmp / "staging" / "dest.md").write_text(dest_text)
    m = tmp / "move-map.md"
    m.write_text("| source | heading | kind | destination |\n|---|---|---|---|\n" + "".join(
        f"| {src} | {h} | {k} | {d} |\n" for h, k, d in rows))
    return src, m


def test_pass_when_all_headings_mapped_and_text_present(tmp_path):
    src, m = setup(tmp_path, (tmp_path / "x").as_posix() and
                   "# Top\nFirst sentence here is long enough. Second one is also long.\n## Sub\nAnother sentence that is long.\n",
                   [("Top", "verbatim", "dest.md"), ("Sub", "verbatim", "dest.md")])
    errs = mmc.check([src], m, tmp_path / "staging", n=5, seed=21)
    assert errs == []


def test_missing_heading_fails(tmp_path):
    src, m = setup(tmp_path, "whatever", [("Top", "dropped:obsolete", "-")])
    errs = mmc.check([src], m, tmp_path / "staging", n=5, seed=21)
    assert any("Sub" in e for e in errs)


def test_verbatim_text_absent_fails(tmp_path):
    src, m = setup(tmp_path, "nothing relevant", [("Top", "verbatim", "dest.md"), ("Sub", "verbatim", "dest.md")])
    errs = mmc.check([src], m, tmp_path / "staging", n=5, seed=21)
    assert any("not found" in e for e in errs)


def test_rewritten_needs_line(tmp_path):
    src, m = setup(tmp_path, "x", [("Top", "rewritten", "dest.md"), ("Sub", "hook:x", "-")])
    errs = mmc.check([src], m, tmp_path / "staging", n=5, seed=21)
    assert any("line" in e for e in errs)
