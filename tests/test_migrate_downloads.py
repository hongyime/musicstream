import hashlib
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_downloads_to_music.py"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migrate_dry_run_leaves_files_intact(tmp_path):
    root = tmp_path / "drive"
    target = root / "music"
    root.mkdir()
    target.mkdir()

    # Create a rogue artist dir in root
    rogue_file = root / "Artist A" / "Album 1" / "01 - Song.mp3"
    rogue_file.parent.mkdir(parents=True)
    rogue_file.write_text("hello music")

    cmd = [sys.executable, str(SCRIPT_PATH), "--root", str(root), "--target", str(target), "--dry-run"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "Found 1 rogue items" in res.stderr or "Found 1 rogue items" in res.stdout

    # File should still be in root
    assert rogue_file.is_file()
    assert not (target / "Artist A").exists()


def test_migrate_moves_whole_dir_and_preserves_protected(tmp_path):
    root = tmp_path / "drive"
    target = root / "music"
    root.mkdir()
    target.mkdir()

    # Protected dirs
    data_dir = root / "data"
    data_dir.mkdir()
    (data_dir / "keep.txt").write_text("keep me")
    marker = root / ".musicstream-ready"
    marker.write_text("ready")

    # Rogue artist
    rogue_file = root / "Artist B" / "Album 1" / "01 - Track.mp3"
    rogue_file.parent.mkdir(parents=True)
    rogue_file.write_text("track audio content")

    cmd = [sys.executable, str(SCRIPT_PATH), "--root", str(root), "--target", str(target)]
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Protected items untouched
    assert (data_dir / "keep.txt").is_file()
    assert marker.is_file()
    assert not (root / "Artist B").exists()

    # Moved to target
    moved = target / "Artist B" / "Album 1" / "01 - Track.mp3"
    assert moved.is_file()
    assert moved.read_text() == "track audio content"


def test_migrate_merges_and_quarantines_differing_files(tmp_path):
    root = tmp_path / "drive"
    target = root / "music"
    root.mkdir()
    target.mkdir()

    # Artist already exists in target
    target_existing = target / "Artist C" / "Album 1" / "01 - Differing.mp3"
    target_existing.parent.mkdir(parents=True)
    target_existing.write_text("old content in music folder")

    target_identical = target / "Artist C" / "Album 1" / "02 - Same.mp3"
    target_identical.write_text("identical content")

    # Rogue versions in root
    rogue_diff = root / "Artist C" / "Album 1" / "01 - Differing.mp3"
    rogue_diff.parent.mkdir(parents=True)
    rogue_diff.write_text("new content from rogue download")

    rogue_same = root / "Artist C" / "Album 1" / "02 - Same.mp3"
    rogue_same.write_text("identical content")

    rogue_new = root / "Artist C" / "Album 1" / "03 - BrandNew.mp3"
    rogue_new.write_text("brand new track")

    cmd = [sys.executable, str(SCRIPT_PATH), "--root", str(root), "--target", str(target)]
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    # Root artist dir should be completely gone
    assert not (root / "Artist C").exists()

    # Brand new file moved
    assert (target / "Artist C" / "Album 1" / "03 - BrandNew.mp3").read_text() == "brand new track"

    # Identical file kept in target
    assert (target / "Artist C" / "Album 1" / "02 - Same.mp3").read_text() == "identical content"

    # Differing file updated with rogue content and old quarantined
    assert (target / "Artist C" / "Album 1" / "01 - Differing.mp3").read_text() == "new content from rogue download"
    orphans = list((target / "Artist C" / "Album 1").glob("01 - Differing.mp3.orphan-*"))
    assert len(orphans) == 1
    assert orphans[0].read_text() == "old content in music folder"
