"""The gate every uploaded byte passes before anything tries to understand it.

Ordering is the design. Counts and declared sizes are checked before any file is read,
the file type is decided from the bytes before any parser is chosen, and a container's
*declared* contents are inspected before a single entry is decompressed. Each of those is
a step where a hostile file gets one fewer chance to be handled by code that assumed it
was friendly.

Three things this deliberately does not do:

* **It does not trust the extension.** ``.xlsx`` is what the browser said; ``PK\\x03\\x04``
  followed by a ``macroEnabled`` content type is what the file is. The rename is the
  cheapest attack there is, so the name is used for nothing but the error message.
* **It does not decompress to measure.** A zip bomb is refused from the central
  directory's declared sizes and ratios. Reading an entry to find out how big it is *is*
  the attack.
* **It does not open a workbook.** ``openpyxl`` is never imported here. Everything this
  module does to an Office file is done with ``zipfile`` and ``defusedxml``, so a file
  that fails the gate has never reached a parser.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass

from defusedxml import ElementTree as DefusedET

from server.errors import ApiError

log = logging.getLogger("upload_demo.intake")

# --------------------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------------------

MAX_FILES = 5
MAX_FILE_MB = 25
MAX_TOTAL_MB = 40
MAX_NOTES_CHARS = 2000
MAX_PDF_PAGES = 150

MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024

#: Zip-container limits. ``docx`` and ``xlsx`` are zips, so these apply to every Office
#: upload even though a bare ``.zip`` is refused outright.
MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
#: Per-entry compression ratio. The worst ratio in the real deal workbooks this was sized
#: against is 37.8; a bomb runs to four figures.
MAX_ZIP_RATIO = 200

#: The most any single archive entry may declare before the PARENT process will read it.
#:
#: The archive-wide caps above bound the package; they do not bound one entry inside it.
#: A crafted `[Content_Types].xml` can declare 150 MB, sit under the 200 MB total and
#: under the 200x per-entry ratio, and still be decompressed, decoded and parsed here — in
#: the API process, on the event loop's thread pool, before the sandboxed child has seen
#: anything. A real manifest is a few kilobytes; 512 KB is generous by two orders of
#: magnitude and cheap to refuse.
#:
#: One cap for every entry, on purpose: ``[Content_Types].xml`` is the only entry this
#: process reads today. Split it per role the moment a second one is — not before.
MAX_PARENT_ENTRY_BYTES = 512 * 1024

ACCEPTED_EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv")

KINDS: tuple[str, ...] = ("pdf", "docx", "doc", "xlsx", "xls", "csv")

#: What `sniff` answers for a compound file. PROVISIONAL: it names the container, and the
#: sandboxed child resolves it to `doc` or `xls` before the batch is admitted. It is never
#: a kind a parser is chosen by.
OLE_CONTAINER_KIND = "ole"

# --------------------------------------------------------------------------------------
# Content types inside an OOXML package
# --------------------------------------------------------------------------------------

CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_XLTX = "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml"
CT_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_DOTX = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"

#: Anything whose content type says it can carry code. The point of the demo is that a
#: server opens a stranger's file; a macro-enabled package is the one shape where that is
#: not merely a parsing problem.
MACRO_MARKER = "macroenabled"
#: The other spelling of the same thing. A package whose MAIN type is an ordinary
#: spreadsheet can still declare `application/vnd.ms-office.vbaProject` for one of its
#: parts, and that part is executable code.
VBA_MARKER = "vbaproject"
#: And the part itself, by name, for a package that declares no content type for it.
VBA_PART = "vbaproject.bin"

CT_TO_KIND: dict[str, str] = {
    CT_XLSX: "xlsx",
    CT_XLTX: "xlsx",
    CT_DOCX: "docx",
    CT_DOTX: "docx",
}


@dataclass(frozen=True)
class Accepted:
    """One file that passed every check, with what the gate concluded about it.

    ``probed`` is what stops the WORKER re-running work the gate already did. Every PDF
    was probed twice on the same bytes — once here to answer the page and encryption caps,
    once again at the top of the run — and a document whose probe approaches the 45-second
    parser limit could spend most of the 120-second run deadline being checked twice and
    time out despite having passed intake.
    """

    filename: str
    kind: str
    size: int
    pages: int | None = None
    encrypted: bool = False
    probed: bool = False


# --------------------------------------------------------------------------------------
# Batch-level caps — before a single byte of content is looked at
# --------------------------------------------------------------------------------------


def check_batch(count: int, notes: str) -> None:
    if count < 1:
        raise ApiError(400, "no_files", "Attach at least one document to run the pipeline.")
    if count > MAX_FILES:
        raise ApiError(
            400,
            "too_many_files",
            f"This demo takes up to {MAX_FILES} files at a time, and {count} were "
            f"attached. Send the offering memorandum, the T-12 and the rent roll.",
        )
    if len(notes) > MAX_NOTES_CHARS:
        raise ApiError(
            400,
            "notes_too_long",
            f"The notes box holds {MAX_NOTES_CHARS:,} characters and this one has "
            f"{len(notes):,}. Trim it and try again.",
        )


def check_file_size(filename: str, size: int) -> None:
    if size <= 0:
        raise ApiError(400, "empty_file", f"{filename} is empty.")
    if size > MAX_FILE_BYTES:
        raise ApiError(
            413,
            "file_too_large",
            f"{filename} is {size / 1024 / 1024:.1f} MB and the limit for one file is "
            f"{MAX_FILE_MB} MB.",
        )


def check_total_size(total: int) -> None:
    if total > MAX_TOTAL_BYTES:
        raise ApiError(
            413,
            "batch_too_large",
            f"Those files come to {total / 1024 / 1024:.1f} MB together and the limit for "
            f"one run is {MAX_TOTAL_MB} MB.",
        )


# --------------------------------------------------------------------------------------
# What is this file, really
# --------------------------------------------------------------------------------------

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def unsupported(filename: str, detail: str = "") -> ApiError:
    return ApiError(
        415,
        "unsupported_type",
        f"{filename} is not a file type this demo reads. It takes "
        f"{', '.join(ACCEPTED_EXTENSIONS)}."
        + (f" {detail}" if detail else ""),
    )


def sniff(data: bytes, filename: str) -> str:
    """Decide the kind from the leading bytes, then confirm it from the container.

    The extension is never consulted. A ``.zip`` upload is refused here not by its name
    but because a bare zip has no Office content type inside it.
    """
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(ZIP_MAGIC):
        return inspect_ooxml(data, filename)
    if data.startswith(OLE_MAGIC):
        # The CONTAINER, not the format. Which of the two legacy formats it is takes a
        # parser, and that runs in the child — see `resolve_ole_report`.
        return OLE_CONTAINER_KIND
    if looks_like_csv(data):
        return "csv"
    raise unsupported(filename)


#: How far back from the end of the file the End-of-Central-Directory record may sit.
#:
#: The EOCD is 22 bytes plus a comment of at most 65,535, so 64 KiB + a little covers every
#: legal archive. Bounded because this scan runs in the API process on a stranger's bytes:
#: it reads a fixed tail, never the file.
_EOCD_SEARCH_BYTES = 66 * 1024

_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_EOCD = b"PK\x06\x06"


def declared_entry_count(data: bytes) -> int | None:
    """How many entries this archive SAYS it holds, read from its own end record.

    ``zipfile.ZipFile(...)`` parses every central-directory record and builds a ``ZipInfo``
    for each one before ``infolist()`` returns, so a 25 MB archive declaring hundreds of
    thousands of tiny entries allocated all of them **in the API process** before the
    entry cap could fire. The cap has to be answerable without that allocation, and the
    archive already states the number: it is a field in the End-of-Central-Directory
    record, sixteen bytes from the end of a comment-free zip.

    ZIP64 is handled because that is exactly how a crafted file would step around a
    16-bit field: when the count reads 0xFFFF the real number is in the ZIP64 EOCD, found
    through its locator.

    Returns None when no end record can be found — an unreadable archive is not this
    function's refusal to make; ``ZipFile`` will say so in its own words.
    """
    tail = data[-_EOCD_SEARCH_BYTES:] if len(data) > _EOCD_SEARCH_BYTES else data
    marker = tail.rfind(_EOCD_SIGNATURE)
    if marker == -1 or len(tail) - marker < 22:
        return None
    total = int.from_bytes(tail[marker + 10:marker + 12], "little")
    if total != 0xFFFF:
        return total

    # ZIP64. The locator sits immediately before the EOCD and points at the ZIP64 EOCD,
    # whose own total-entries field is 64 bits wide. Every read below is inside `tail`.
    locator = tail.rfind(_ZIP64_LOCATOR, 0, marker)
    if locator == -1 or locator + 16 > len(tail):
        return total
    offset = int.from_bytes(tail[locator + 8:locator + 16], "little")
    absolute = offset - (len(data) - len(tail))
    if absolute < 0 or absolute + 40 > len(tail):
        # The ZIP64 record is outside the tail this function is willing to read. Saying
        # "unknown" here would let the file through on a technicality, so the 16-bit
        # saturation value stands and the caller refuses it.
        return total
    if tail[absolute:absolute + 4] != _ZIP64_EOCD:
        return total
    return int.from_bytes(tail[absolute + 32:absolute + 40], "little")


_CENTRAL_SIGNATURE = b"PK\x01\x02"
#: The fixed part of a central-directory record: 46 bytes, then the name, extra and
#: comment whose lengths it declares.
_CENTRAL_FIXED = 46


def central_directory_offset(data: bytes) -> "int | None":
    """Where the central directory starts, per the end record. ``None`` if unreadable."""
    tail = data[-_EOCD_SEARCH_BYTES:] if len(data) > _EOCD_SEARCH_BYTES else data
    marker = tail.rfind(_EOCD_SIGNATURE)
    if marker == -1 or len(tail) - marker < 22:
        return None
    offset = int.from_bytes(tail[marker + 16:marker + 20], "little")
    if offset == 0xFFFFFFFF:
        locator = tail.rfind(_ZIP64_LOCATOR, 0, marker)
        if locator == -1 or locator + 16 > len(tail):
            return None
        zip64 = int.from_bytes(tail[locator + 8:locator + 16], "little")
        absolute = zip64 - (len(data) - len(tail))
        if absolute < 0 or absolute + 56 > len(tail):
            return None
        if tail[absolute:absolute + 4] != _ZIP64_EOCD:
            return None
        offset = int.from_bytes(tail[absolute + 48:absolute + 56], "little")
    return offset if 0 <= offset < len(data) else None


def count_entries(data: bytes, limit: int) -> "int | None":
    """Walk the central directory and COUNT its records, stopping just past ``limit``.

    The end record's entry total is a claim by the file, and `declared_entry_count` reads
    it — but an archive is free to under-declare, and `ZipFile` then allocates a `ZipInfo`
    for every record it actually finds, in the API process, before any cap can fire. Walking the records is what the claim cannot lie about: each is a fixed
    46-byte header plus three declared lengths, so this reads ~50 bytes per entry and
    allocates nothing.

    Stops at ``limit + 1`` — enough to know the archive is over the cap, and never a walk
    of 300,000 records to find out. ``None`` means the directory could not be followed,
    which is not this function's refusal to make: ``ZipFile`` will say so in its own words.
    """
    offset = central_directory_offset(data)
    if offset is None:
        return None
    count = 0
    position = offset
    while position + _CENTRAL_FIXED <= len(data):
        if data[position:position + 4] != _CENTRAL_SIGNATURE:
            break
        count += 1
        if count > limit:
            return count
        name = int.from_bytes(data[position + 28:position + 30], "little")
        extra = int.from_bytes(data[position + 30:position + 32], "little")
        comment = int.from_bytes(data[position + 32:position + 34], "little")
        position += _CENTRAL_FIXED + name + extra + comment
    return count


def inspect_ooxml(data: bytes, filename: str) -> str:
    """A zip is only acceptable if it is a Word or Excel package, and only if it is sane.

    Nothing is decompressed. ``infolist()`` reads the central directory, which is where a
    bomb declares how large it intends to become — but the ENTRY COUNT is checked before
    that, straight out of the end record, because parsing the directory is itself the
    allocation the cap exists to prevent.
    """
    # Counted first, because the count is the thing an archive can lie about and the
    # allocation is what the lie buys. The declared figure is still read below: an archive
    # that OVER-declares is refused without even walking.
    walked = count_entries(data, MAX_ZIP_ENTRIES)
    if walked is not None and walked > MAX_ZIP_ENTRIES:
        raise ApiError(
            413,
            "archive_too_complex",
            f"{filename} contains more than {MAX_ZIP_ENTRIES:,} internal parts, well "
            f"past anything a real document needs.",
        )
    declared_entries = declared_entry_count(data)
    if declared_entries is not None and declared_entries > MAX_ZIP_ENTRIES:
        raise ApiError(
            413,
            "archive_too_complex",
            f"{filename} says it contains {declared_entries:,} internal parts, well past "
            f"anything a real document needs.",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ApiError(
            422, "unreadable_file", f"{filename} looks damaged and could not be opened."
        ) from exc

    infos = archive.infolist()
    # Kept as well as the pre-scan: the end record is a CLAIM, and an archive that
    # under-declares itself must still be refused on what it actually holds.
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ApiError(
            413,
            "archive_too_complex",
            f"{filename} contains {len(infos):,} internal parts, well past anything a "
            f"real document needs.",
        )

    declared = sum(i.file_size for i in infos)
    if declared > MAX_ZIP_UNCOMPRESSED:
        raise ApiError(
            413,
            "archive_expands_too_far",
            f"{filename} says it expands to {declared / 1024 / 1024:.0f} MB, past the "
            f"{MAX_ZIP_UNCOMPRESSED // 1024 // 1024} MB this demo will unpack.",
        )

    for info in infos:
        if info.compress_size <= 0:
            continue
        ratio = info.file_size / info.compress_size
        if ratio > MAX_ZIP_RATIO and info.file_size > 1024 * 1024:
            raise ApiError(
                413,
                "archive_expands_too_far",
                f"{filename} contains a part that expands {ratio:,.0f} times over, which "
                f"no real document does.",
            )

    names = {i.filename for i in infos}
    # A package can advertise a plain xlsx main type and still carry a VBA project: the
    # main-type spelling is what `macroenabled` matches, and `vbaProject.bin` contains no
    # such word. The part is the code, so the part is what is refused.
    if any(name.rsplit("/", 1)[-1].lower() == VBA_PART for name in names):
        raise ApiError(
            415,
            "macros_not_accepted",
            f"{filename} carries a VBA project. This demo does not open files that can "
            f"carry code — save it as a plain .xlsx or .docx and try again.",
        )
    if "[Content_Types].xml" not in names:
        raise unsupported(filename, "It is a plain archive, not a Word or Excel file.")

    content_types = read_entry(
        archive, "[Content_Types].xml", filename
    ).decode("utf-8", "replace")

    kind = kind_from_content_types(content_types, filename)
    # The kind and the part count, never the name. An uploader's filename routinely
    # carries a property or a client name, and a log line outlives the one-hour sweeper
    # that deletes everything else about the run.
    log.info("intake: accepted a %s package (%d parts)", kind, len(infos))
    return kind


def read_entry(archive: zipfile.ZipFile, name: str, filename: str) -> bytes:
    """Read one archive entry in the parent, but only after its DECLARED size is checked.

    Every entry this process reads goes through here. The check is on the header's own
    ``file_size``, before a byte is decompressed, because the point is to never allocate
    the thing at all — checking afterwards is checking that the damage has been done.
    """
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ApiError(
            422, "unreadable_file", f"{filename} looks damaged and could not be opened."
        ) from exc
    if info.file_size > MAX_PARENT_ENTRY_BYTES:
        raise ApiError(
            413,
            "archive_expands_too_far",
            f"{filename} contains a {info.file_size / 1024 / 1024:.0f} MB "
            f"{name} part. That file is a few kilobytes in any real document, so this "
            f"one is not opened.",
        )
    try:
        data = archive.read(name)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ApiError(
            422, "unreadable_file", f"{filename} looks damaged and could not be opened."
        ) from exc
    # A header can lie. Belt and braces on the bytes actually produced.
    if len(data) > MAX_PARENT_ENTRY_BYTES:
        raise ApiError(
            413,
            "archive_expands_too_far",
            f"{filename} contains a {name} part far larger than it declared.",
        )
    return data


def kind_from_content_types(xml_text: str, filename: str) -> str:
    """Read the package manifest and refuse anything macro-enabled.

    Parsed with ``defusedxml``: this is a stranger's XML, and the manifest is the first
    thing in the file that gets parsed at all.
    """
    try:
        root = DefusedET.fromstring(xml_text)
    except Exception as exc:  # noqa: BLE001 - any XML failure is the same answer here
        raise ApiError(
            422, "unreadable_file", f"{filename} looks damaged and could not be opened."
        ) from exc

    declared = []
    for element in root.iter():
        value = element.attrib.get("ContentType")
        if value:
            declared.append(value)

    if any(MACRO_MARKER in value.lower() or VBA_MARKER in value.lower()
           for value in declared):
        raise ApiError(
            415,
            "macros_not_accepted",
            f"{filename} is a macro-enabled Office file. This demo does not open files "
            f"that can carry code — save it as a plain .xlsx or .docx and try again.",
        )

    for value in declared:
        kind = CT_TO_KIND.get(value)
        if kind:
            return kind

    raise unsupported(filename, "It is an archive, but not a Word or Excel document.")


def resolve_ole_report(report: dict, filename: str) -> str:
    """Turn the child's OLE report into ``doc`` or ``xls``, or refuse.

    The mirror of :func:`check_pdf_report`. This function does no parsing: ``olefile`` runs
    in ``parse_worker``'s ``probe_ole`` job, inside the sandboxed child under its CPU,
    memory and wall-clock limits. Opening an attacker-controlled compound file in the API
    process — synchronously, on the event loop, as root — was the one place left where a
    crafted upload could stall or kill the whole service before any sandbox was reached.
    """
    kind = report.get("ole_kind")
    if kind in ("doc", "xls"):
        return kind
    raise unsupported(
        filename, "It is an old Office file, but not a Word document or an Excel workbook."
    )


def looks_like_csv(data: bytes) -> bool:
    """Text, and text with some structure to it. Deliberately conservative."""
    head = data[:64_000]
    if b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("latin-1")
        except UnicodeDecodeError:
            return False
    if not text.strip():
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    if printable / len(text) < 0.95:
        return False
    sample = "\n".join(text.splitlines()[:20])
    try:
        csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return True
    except csv.Error:
        # A single-column file has no delimiter to find and is still a valid csv.
        return bool(sample.strip()) and "\n" in sample


# --------------------------------------------------------------------------------------
# PDF: the two refusals that need the file opened
# --------------------------------------------------------------------------------------


def check_pdf_report(report: dict, filename: str) -> int:
    """Turn the child's page/encryption report into an acceptance or a refusal.

    This function does no parsing. The report comes from ``parse_worker``'s ``probe_pdf``
    job, which runs pypdf inside the sandboxed child under its CPU, memory and wall-clock
    limits — opening an attacker-controlled PDF in the API process to answer a cap
    question was the one place a malformed file could stall the whole service.
    """
    if report.get("encrypted"):
        raise ApiError(
            422,
            "encrypted_file",
            f"{filename} is password-protected. Save an unprotected copy and try "
            f"again — this demo cannot ask you for the password.",
        )
    pages = int(report.get("pages") or 0)
    if pages > MAX_PDF_PAGES:
        raise ApiError(
            413,
            "too_many_pages",
            f"{filename} has {pages:,} pages and the limit is {MAX_PDF_PAGES}. Send the "
            f"offering memorandum rather than the whole data room.",
        )
    return pages


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


def accept_file(filename: str, data: bytes) -> Accepted:
    """Size and type. Anything needing a parser happens in the child, not here."""
    check_file_size(filename, len(data))
    kind = sniff(data, filename)
    return Accepted(filename=filename, kind=kind, size=len(data), pages=None)


def accept_batch(files: list[tuple[str, bytes]], notes: str) -> list[Accepted]:
    """The whole gate: batch caps, then each file, then the combined size."""
    check_batch(len(files), notes)
    total = 0
    accepted: list[Accepted] = []
    for filename, data in files:
        total += len(data)
        check_total_size(total)
        accepted.append(accept_file(filename, data))
    return accepted


async def read_uploads(files: list) -> list[tuple[str, bytes]]:
    """Read the multipart parts into memory, refusing while reading rather than after.

    The caps are enforced against bytes actually received, not against a ``Content-Length``
    the client supplied: a body that keeps arriving past the limit is stopped at the limit,
    which is the difference between a cap and a suggestion.

    Forty megabytes in memory is deliberate at this size — the alternative is spooling a
    stranger's upload to disk before deciding whether to accept it.
    """
    chunk_size = 1024 * 1024
    out: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        name = (getattr(upload, "filename", None) or "upload").strip() or "upload"
        buffer = bytearray()
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            buffer.extend(chunk)
            total += len(chunk)
            if len(buffer) > MAX_FILE_BYTES:
                raise ApiError(
                    413,
                    "file_too_large",
                    f"{name} is larger than the {MAX_FILE_MB} MB limit for one file.",
                )
            check_total_size(total)
        out.append((name, bytes(buffer)))
    return out
