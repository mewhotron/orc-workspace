from dataclasses import dataclass
from pathlib import Path
from datetime import datetime


@dataclass(frozen=True)
class SourceFile:
    """A raw source object, optionally a member of an archive.

    For archived sources original_path is the real container path, while
    checksum_sha256 identifies the member bytes. The separate archive checksum
    and ZIP-relative member name preserve its location without a fictitious
    filesystem path. Existing standalone sources leave both optional fields None.
    Observation storage keeps archive provenance in import receipts.
    """
    source: str
    original_path: Path
    discovered_at: datetime
    checksum_sha256: str
    archive_member: str | None = None
    archive_checksum_sha256: str | None = None
