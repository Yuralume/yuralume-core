"""TDD for the ``.lumebackup`` inner zip container (CB1).

Byte-level container only: round trip through all four member
categories, zip-slip rejection, decompression budget, and silent
forward-compatible ignoring of unknown members (same conventions as the
``.lumecard`` packager, restated here for the streaming variant).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from kokoro_link.infrastructure.character_backup import packager as _packager
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveReader,
    BackupArchiveTooLargeError,
    BackupArchiveWriter,
    InvalidBackupArchiveError,
    _BudgetedStream,
    _ReadBudget,
)

_MANIFEST = {"backup_schema_version": 1, "character_name": "美緒"}
_ASSET_KEY = "characters/abc123/images/0.png"
_ASSET_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(4096)
_YAML = "id: cafe_idol\ntitle: 咖啡廳偶像試鏡\n"
_JSONL_LINES = [b'{"id": "m1"}\n', b'{"id": "m2"}\n']


def _build_archive() -> io.BytesIO:
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest(json.dumps(_MANIFEST, ensure_ascii=False))
        with writer.open_data_file("messages.jsonl") as sink:
            for line in _JSONL_LINES:
                sink.write(line)
        writer.write_arc_template("cafe_idol.yaml", _YAML)
        writer.write_asset(_ASSET_KEY, io.BytesIO(_ASSET_BYTES))
    buffer.seek(0)
    return buffer


# --- round trip ------------------------------------------------------------


def test_round_trip_all_member_categories() -> None:
    with BackupArchiveReader(_build_archive()) as reader:
        assert reader.read_manifest() == _MANIFEST

        data_files = {
            name: stream.read() for name, stream in reader.iter_data_files()
        }
        assert data_files == {"messages.jsonl": b"".join(_JSONL_LINES)}

        templates = dict(reader.iter_arc_templates())
        assert templates == {"cafe_idol.yaml": _YAML}

        assets = {key: stream.read() for key, stream in reader.iter_assets()}
        assert assets == {_ASSET_KEY: _ASSET_BYTES}


def test_asset_streams_read_in_chunks() -> None:
    with BackupArchiveReader(_build_archive()) as reader:
        for _key, stream in reader.iter_assets():
            collected = b""
            while chunk := stream.read(512):
                collected += chunk
            assert collected == _ASSET_BYTES


def test_unknown_members_are_silently_ignored() -> None:
    buffer = _build_archive()
    with zipfile.ZipFile(buffer, "a") as zf:
        zf.writestr("extra/from-the-future.bin", b"newer exporter")
        zf.writestr("notes.txt", b"stray top-level member")
        zf.writestr("data/not-jsonl.csv", b"wrong extension")
    buffer.seek(0)

    with BackupArchiveReader(buffer) as reader:
        assert reader.read_manifest() == _MANIFEST
        assert [name for name, _ in reader.iter_data_files()] == ["messages.jsonl"]
        assert [name for name, _ in reader.iter_arc_templates()] == ["cafe_idol.yaml"]
        assert [key for key, _ in reader.iter_assets()] == [_ASSET_KEY]


# --- manifest --------------------------------------------------------------


def test_missing_manifest_rejected() -> None:
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_arc_template("a.yaml", _YAML)
    buffer.seek(0)

    with BackupArchiveReader(buffer) as reader:
        with pytest.raises(InvalidBackupArchiveError):
            reader.read_manifest()


@pytest.mark.parametrize(
    "raw",
    [b"not json", b'["top-level array"]'],
    ids=["invalid-json", "non-object"],
)
def test_bad_manifest_rejected(raw: bytes) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", raw)
    buffer.seek(0)

    with BackupArchiveReader(buffer) as reader:
        with pytest.raises(InvalidBackupArchiveError):
            reader.read_manifest()


# --- safety ----------------------------------------------------------------


@pytest.mark.parametrize(
    "member",
    [
        "../evil.txt",
        "assets/../../evil.txt",
        "/absolute.txt",
        "\\backslash.txt",
        "assets\\..\\evil.txt",
        "C:evil.txt",
    ],
    ids=["dotdot", "nested-dotdot", "absolute", "backslash", "win-dotdot", "drive"],
)
def test_unsafe_member_path_rejects_whole_archive(member: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", b"{}")
        zf.writestr(member, b"payload")
    buffer.seek(0)

    with pytest.raises(InvalidBackupArchiveError):
        BackupArchiveReader(buffer)


def test_writer_refuses_unsafe_member_paths() -> None:
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        with pytest.raises(InvalidBackupArchiveError):
            writer.write_asset("../escape.png", io.BytesIO(b"x"))
        with pytest.raises(InvalidBackupArchiveError):
            writer.write_arc_template("..\\evil.yaml", _YAML)
        with pytest.raises(InvalidBackupArchiveError):
            writer.open_data_file("../evil.jsonl")


def test_not_a_zip_rejected() -> None:
    with pytest.raises(InvalidBackupArchiveError):
        BackupArchiveReader(io.BytesIO(b"definitely not a zip"))


# --- decompression budget --------------------------------------------------


def test_declared_size_over_budget_rejected_up_front() -> None:
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest("{}")
        writer.write_asset("big.bin", io.BytesIO(bytes(100 * 1024)))
    buffer.seek(0)

    with pytest.raises(BackupArchiveTooLargeError):
        BackupArchiveReader(buffer, max_uncompressed_bytes=1024)


def test_within_budget_archive_reads_fully() -> None:
    buffer = _build_archive()
    declared = len(_ASSET_BYTES) + len(_YAML.encode()) + 1024
    with BackupArchiveReader(buffer, max_uncompressed_bytes=declared) as reader:
        reader.read_manifest()
        for _name, stream in reader.iter_data_files():
            stream.read()
        list(reader.iter_arc_templates())
        for _key, stream in reader.iter_assets():
            stream.read()


def test_data_files_charged_once_across_scan_and_landing_passes() -> None:
    """B1 reproduction: the restore walks every ``data/*.jsonl`` twice
    (scan pass, then landing pass). An honest archive whose single full
    decompression fits the budget exactly must survive both walks —
    charging each pass separately used to fail legitimate backups midway
    through landing with a "declared sizes were a lie" accusation."""
    buffer = _build_archive()
    declared = sum(
        info.file_size
        for info in zipfile.ZipFile(buffer).infolist()
        if not info.is_dir()
    )
    buffer.seek(0)
    with BackupArchiveReader(
        buffer, max_uncompressed_bytes=declared,
    ) as reader:
        reader.read_manifest()
        for _pass in ("scan", "landing"):
            assert list(reader.iter_data_lines("messages.jsonl"))
        list(reader.iter_arc_templates())
        for _key, stream in reader.iter_assets():
            stream.read()


def test_rereading_a_member_is_not_charged_twice() -> None:
    budget = _ReadBudget(10, {"data/a.jsonl": 8})
    _BudgetedStream(io.BytesIO(bytes(8)), budget, "data/a.jsonl").read()
    # A fresh stream over the same member: those bytes are already paid
    # for — the high-water mark keeps the re-read free.
    _BudgetedStream(io.BytesIO(bytes(8)), budget, "data/a.jsonl").read()


def test_member_decompressing_past_its_declared_size_is_a_lie() -> None:
    # A hostile zip can lie about file_size in its central directory, so
    # the budget is enforced on real decompressed bytes too. The wrapper
    # is exercised directly: crafting a lying central directory is
    # gratuitous binary surgery for the same guarantee.
    budget = _ReadBudget(1000, {"data/a.jsonl": 10})
    stream = _BudgetedStream(io.BytesIO(bytes(20)), budget, "data/a.jsonl")
    stream.read(8)
    with pytest.raises(BackupArchiveTooLargeError, match="lied"):
        stream.read(8)


def test_total_over_budget_does_not_accuse_the_archive_of_lying() -> None:
    """The two failure modes carry distinct messages: blowing the total
    budget is「太大」, not「造假」— only a member that inflates past its
    own declared size earns the lie accusation."""
    budget = _ReadBudget(10, {"assets/a.bin": 8, "assets/b.bin": 8})
    _BudgetedStream(io.BytesIO(bytes(8)), budget, "assets/a.bin").read()
    with pytest.raises(BackupArchiveTooLargeError) as excinfo:
        _BudgetedStream(io.BytesIO(bytes(8)), budget, "assets/b.bin").read()
    assert "lie" not in str(excinfo.value)
    assert "budget" in str(excinfo.value)


# --- S3: per-member ceilings (one member can't be the whole bomb) ----------


def test_single_manifest_member_over_its_cap_rejected_up_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 reproduction: the total budget alone lets one member declare
    almost the whole budget and OOM the process the instant a read-whole
    member (the manifest) materialises. The per-member cap rejects the
    honest oversize at construction, before a byte is read."""
    monkeypatch.setattr(_packager, "_MAX_MANIFEST_BYTES", 64)
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest("x" * 4096)  # honest, but past the tiny cap
    buffer.seek(0)
    with pytest.raises(BackupArchiveTooLargeError, match="per-member"):
        BackupArchiveReader(buffer)


def test_single_arc_template_member_over_its_cap_rejected_up_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_packager, "_MAX_ARC_TEMPLATE_BYTES", 64)
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest("{}")
        writer.write_arc_template("big.yaml", "id: x\n" + "a" * 4096)
    buffer.seek(0)
    with pytest.raises(BackupArchiveTooLargeError, match="per-member"):
        BackupArchiveReader(buffer)


def test_data_files_are_not_per_member_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data files (read line by line, bounded per line) and assets
    (legitimately large media) keep no small per-member cap — capping them
    would reject honest heavy backups."""
    monkeypatch.setattr(_packager, "_MAX_MANIFEST_BYTES", 8)
    monkeypatch.setattr(_packager, "_MAX_ARC_TEMPLATE_BYTES", 8)
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest("{}")  # under 8 bytes
        with writer.open_data_file("messages.jsonl") as sink:
            for i in range(500):
                sink.write(f'{{"id": {i}}}\n'.encode("utf-8"))
    buffer.seek(0)
    # Constructs fine despite the tiny manifest/yaml caps: the jsonl member
    # is exempt.
    with BackupArchiveReader(buffer) as reader:
        assert len(list(reader.iter_data_lines("messages.jsonl"))) == 500


def test_single_jsonl_line_bomb_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 reproduction: a data file that is one enormous line (no newline)
    is an unbounded buffer in ``iter_data_lines`` even though it passes the
    total budget — the per-line cap stops it before OOM."""
    monkeypatch.setattr(_packager, "_MAX_DATA_LINE_BYTES", 1024)
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest("{}")
        with writer.open_data_file("messages.jsonl") as sink:
            sink.write(b"x" * 4096)  # one line, no newline, over the cap
    buffer.seek(0)
    with BackupArchiveReader(buffer) as reader:
        with pytest.raises(InvalidBackupArchiveError, match="data line"):
            list(reader.iter_data_lines("messages.jsonl"))


class _ChunkedStream:
    """A file-like that hands back a fixed small piece per read, tracking
    how many bytes it has actually served (to prove a bounded read)."""

    def __init__(self, piece: bytes, count: int) -> None:
        self._piece = piece
        self._remaining = count
        self.served = 0

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        self._remaining -= 1
        self.served += len(self._piece)
        return self._piece


def test_whole_read_charges_incrementally_and_stops_on_a_lie() -> None:
    """S3 reproduction: a member whose central directory declares a small
    size but decompresses huge must be caught *while* streaming, not after
    the whole payload is buffered. ``read(-1)`` now decompresses in bounded
    chunks and charges each — so a lying member trips before it OOMs."""
    budget = _ReadBudget(10**9, {"data/a.jsonl": 8})
    inner = _ChunkedStream(b"xxxx", count=1000)  # would be 4000 bytes total
    stream = _BudgetedStream(inner, budget, "data/a.jsonl")
    with pytest.raises(BackupArchiveTooLargeError, match="lied"):
        stream.read()  # whole-member read
    # Stopped after the charge crossed the declared size (12 > 8), having
    # pulled only three 4-byte pieces — never the full 4000 bytes.
    assert inner.served == 12
