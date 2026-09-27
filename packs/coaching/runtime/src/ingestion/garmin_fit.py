"""Read-only FIT semantic decoding; no activity normalization or persistence.

Messages are private athlete data, including coordinates. They are deliberately
excluded from repr and must not be logged wholesale. SDK errors fail closed:
partial messages are never returned. Unknown definitions remain visible as
numeric diagnostics, without claiming their fields have known semantics.
SDK 21.214 rejects compressed timestamp messages; that limitation is propagated
as a safe decode failure, never worked around by returning partial data.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from garmin_fit_sdk import Decoder, Profile, Stream


class GarminFitError(ValueError):
    """A fixed diagnostic code, never an SDK exception or private source value."""


@dataclass(frozen=True, repr=False)
class FitProvenance:
    checksum_sha256: str
    original_path: Path | None = None
    archive_member: str | None = None
    archive_checksum_sha256: str | None = None
    decoded_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc), compare=False,
    )


@dataclass(frozen=True)
class FitDecodeResult:
    provenance: FitProvenance = field(repr=False)
    messages: dict = field(repr=False)
    sdk_version: str
    unknown_message_numbers: tuple[int, ...]
    unknown_fields: tuple[tuple[int, int], ...]
    developer_field_definitions: tuple[tuple[int, int, int], ...]


def decode_fit_bytes(
    content: bytes, *, source_path: Path | None = None,
    archive_member: str | None = None, archive_checksum_sha256: str | None = None,
) -> FitDecodeResult:
    """Decode one FIT binary, with SI scales and SDK semantic field names.

    Optional archive provenance is supplied by the caller that read the container;
    its checksum identifies container bytes, while checksum_sha256 identifies the
    FIT member bytes. This function does not reopen that container. Decode time is
    excluded from equality; identical content and provenance compare equal.

    Elapsed, timer, moving, creation and session times retain their SDK field names.
    Heart-rate stream merging is disabled to avoid deriving a replacement stream.
    """
    if not isinstance(content, bytes):
        raise GarminFitError("fit_input_must_be_bytes")
    if (archive_member is None) != (archive_checksum_sha256 is None):
        raise GarminFitError("incomplete_fit_archive_provenance")
    if archive_member is not None and (
        not isinstance(archive_member, str) or not archive_member
        or source_path is None
        or not isinstance(archive_checksum_sha256, str)
        or len(archive_checksum_sha256) != 64
        or any(c not in "0123456789abcdef" for c in archive_checksum_sha256)
    ):
        raise GarminFitError("invalid_fit_archive_provenance")
    if source_path is not None and not isinstance(source_path, Path):
        raise GarminFitError("invalid_fit_source_path")
    unknown_messages, unknown_fields, developers = set(), set(), set()

    def definition_listener(definition):
        number = definition["global_mesg_num"]
        profile = Profile["messages"].get(number)
        if profile is None:
            unknown_messages.add(number)
        known = profile["fields"] if profile else {}
        for definition_field in definition["field_definitions"]:
            number_field = definition_field["field_id"]
            if number_field not in known:
                unknown_fields.add((number, number_field))
        for definition_field in definition["developer_field_defs"]:
            developers.add((number, definition_field["developer_data_index"],
                            definition_field["field_definition_number"]))

    try:
        with closing(Stream.from_byte_array(bytearray(content))) as stream:
            decoder = Decoder(stream)
            if not decoder.is_fit():
                raise GarminFitError("invalid_fit_header")
            # The SDK integrity method checks the first frame. This boundary
            # deliberately accepts one file only, rather than chained binaries.
            size = content[0] + int.from_bytes(content[4:8], "little") + 2
            if size != len(content) or not decoder.check_integrity():
                raise GarminFitError("fit_integrity_failed")
            # check_integrity consumes the stream; read must start at zero.
            stream.seek(0)
            messages, errors = decoder.read(
                enable_crc_check=True, apply_scale_and_offset=True,
                convert_datetimes_to_dates=True, convert_types_to_strings=True,
                expand_sub_fields=True, expand_components=True,
                merge_heart_rates=False,
                mesg_definition_listener=definition_listener,
            )
            if errors:
                raise GarminFitError("fit_decode_failed")
    except GarminFitError:
        raise
    except Exception:
        raise GarminFitError("fit_decode_failed") from None
    return FitDecodeResult(
        FitProvenance(hashlib.sha256(content).hexdigest(), source_path,
                      archive_member, archive_checksum_sha256),
        messages, version("garmin-fit-sdk"), tuple(sorted(unknown_messages)),
        tuple(sorted(unknown_fields)), tuple(sorted(developers)),
    )


def decode_fit_file(path: Path) -> FitDecodeResult:
    """Read source bytes without writes; errors never include the private path."""
    try:
        content = path.read_bytes()
    except (OSError, ValueError):
        raise GarminFitError("fit_source_read_failed") from None
    return decode_fit_bytes(content, source_path=path)
