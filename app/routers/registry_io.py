"""Bulk camera onboarding and export.

The tender's Model 1 asks for "bulk import, manual entry, and API-based camera
onboarding". Manual entry and the API already existed; onboarding a department's
spreadsheet of several thousand cameras one HTTP request at a time did not.

Design decisions worth stating:

* **Dry run first.** The default is to validate and report, not to write. An
  import of 4,000 rows that fails on row 3,817 must not leave 3,816 cameras
  half-registered, and the operator needs the error list before committing.
* **All or nothing.** The commit runs in one transaction. Partial imports are
  how registries end up with duplicates nobody can find.
* **Existing rows are skipped, not silently overwritten.** ``global_camera_code``
  is the fleet-wide identity; re-importing last month's file should not quietly
  revert a correction someone made since. `update_existing` opts into that.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Annotated, Any, Final

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.auth.dependencies import Principal, get_audited_connection, get_current_principal, require_role
from app.database import get_connection
from app.schemas import BulkImportResult, BulkRowError, CameraCreate

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cameras", tags=["registry io"])

# 10 MB: comfortably more than 80,000 rows of CSV, small enough that a mistaken
# upload of a video file is rejected before it reaches memory.
_MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024

# Column order of both the template and the export. Chosen so the mandatory
# fields come first and an operator filling it by hand is not hunting columns.
_TEMPLATE_COLUMNS: Final = [
    "global_camera_code",
    "department_id",
    "latitude",
    "longitude",
    "stream_url",
    "site_name",
    "camera_type",
    "make",
    "model",
    "status",
    "azimuth_angle",
    "fov_degrees",
    "resolution",
    "codec",
    "frame_rate",
    "ip_address",
    "serial_number",
    "installed_on",
    "owner_org",
    "custodian_name",
    "custodian_contact",
    "vms_vendor",
    "nvr_reference",
    "nvr_channel",
    "retention_days",
    "address",
    "ward",
    "zone",
    "district",
    "connectivity_type",
    "maintenance_state",
    "last_serviced_on",
    "next_service_due",
    "work_order_ref",
    "notes",
]

_SAMPLE_ROW: Final = {
    "global_camera_code": "AHM-TRF-00101",
    "department_id": "POLICE",
    "latitude": "23.022500",
    "longitude": "72.571400",
    "stream_url": "rtsp://10.20.30.40:554/stream1",
    "site_name": "CG Road / Panchvati Circle",
    "camera_type": "FIXED",
    "make": "Hikvision",
    "model": "DS-2CD2T47G2",
    "status": "ACTIVE",
    "azimuth_angle": "135",
    "fov_degrees": "70",
    "resolution": "1920x1080",
    "codec": "H264",
    "frame_rate": "25",
    "ip_address": "10.20.30.40",
    "installed_on": "2021-06-15",
    "owner_org": "Ahmedabad Municipal Corporation",
    "retention_days": "30",
    "ward": "Navrangpura",
    "connectivity_type": "FIBRE",
}

_INTEGER_FIELDS: Final = {"frame_rate", "retention_days"}
_FLOAT_FIELDS: Final = {"latitude", "longitude", "azimuth_angle", "fov_degrees"}
_DATE_FIELDS: Final = {"installed_on", "last_serviced_on", "next_service_due"}
_BOOL_FIELDS: Final = {"recording_enabled"}


def _coerce(field: str, raw: str) -> Any:
    """Turn one spreadsheet cell into the type the model expects.

    Spreadsheets have no types: everything arrives as text, and an empty cell
    must become None rather than an empty string, or every optional field fails
    validation for the wrong reason.
    """
    value = (raw or "").strip()
    if value == "":
        return None

    if field in _INTEGER_FIELDS:
        return int(float(value))  # tolerate "30.0" from Excel
    if field in _FLOAT_FIELDS:
        return float(value)
    if field in _DATE_FIELDS:
        # Accept the ISO form and the two spellings Indian operators type most.
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"{field}: '{value}' is not a date (use YYYY-MM-DD)")
    if field in _BOOL_FIELDS:
        return value.lower() in {"1", "true", "yes", "y"}
    return value


def _read_rows(upload: UploadFile, content: bytes) -> list[dict[str, str]]:
    """Parse CSV or XLSX into a list of dictionaries keyed by header."""
    name = (upload.filename or "").lower()

    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Excel import needs openpyxl installed on the server",
            ) from exc

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        try:
            header = [str(cell or "").strip() for cell in next(rows)]
        except StopIteration:
            return []
        parsed = []
        for values in rows:
            if values is None or all(v is None or str(v).strip() == "" for v in values):
                continue
            parsed.append(
                {
                    header[i]: ("" if values[i] is None else str(values[i]).strip())
                    for i in range(min(len(header), len(values)))
                }
            )
        workbook.close()
        return parsed

    # CSV. utf-8-sig strips the BOM Excel writes, which otherwise corrupts the
    # first header name and makes global_camera_code look missing.
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


@router.get(
    "/import-template.csv",
    summary="Download the bulk-import template",
    response_class=StreamingResponse,
)
async def import_template(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> StreamingResponse:
    """A CSV with the expected headers and one worked example row."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_TEMPLATE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerow(_SAMPLE_ROW)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trinetra-camera-import-template.csv"'},
    )


@router.post(
    "/bulk",
    response_model=BulkImportResult,
    summary="Bulk-onboard cameras from CSV or Excel",
)
async def bulk_import(
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
    file: Annotated[UploadFile, File(description="CSV or XLSX matching the template")],
    dry_run: Annotated[bool, Query(description="Validate only; write nothing")] = True,
    update_existing: Annotated[
        bool, Query(description="Overwrite rows whose camera code already exists")
    ] = False,
) -> BulkImportResult:
    """Validate an upload row by row, then optionally commit it atomically."""
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
        )
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="the uploaded file is empty"
        )

    try:
        raw_rows = _read_rows(file, content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"could not parse the file: {exc}",
        ) from exc

    if not raw_rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the file has a header but no data rows",
        )

    valid: list[CameraCreate] = []
    failures: list[BulkRowError] = []

    for index, raw in enumerate(raw_rows, start=2):  # row 1 is the header
        code = raw.get("global_camera_code") or None
        payload: dict[str, Any] = {}
        errors: list[str] = []

        for field, value in raw.items():
            if not field or field not in _TEMPLATE_COLUMNS:
                continue  # ignore extra columns rather than failing the row
            try:
                coerced = _coerce(field, value)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if coerced is not None:
                payload[field] = coerced

        if errors:
            failures.append(BulkRowError(row_number=index, global_camera_code=code, errors=errors))
            continue

        try:
            camera = CameraCreate(**payload)
        except ValidationError as exc:
            failures.append(
                BulkRowError(
                    row_number=index,
                    global_camera_code=code,
                    errors=[
                        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                        for err in exc.errors()
                    ],
                )
            )
            continue

        # Department scope is enforced per row: a POLICE admin cannot smuggle an
        # RTO camera in through a spreadsheet column.
        try:
            principal.assert_can_access_department(camera.department_id)
        except HTTPException as exc:
            failures.append(
                BulkRowError(row_number=index, global_camera_code=code, errors=[exc.detail])
            )
            continue

        valid.append(camera)

    # Duplicate codes inside the file itself: the database would catch this on
    # insert, but the operator deserves the row numbers rather than one opaque
    # unique-violation for the whole upload.
    seen: dict[str, int] = {}
    deduped: list[CameraCreate] = []
    for camera in valid:
        first = seen.get(camera.global_camera_code)
        if first is not None:
            failures.append(
                BulkRowError(
                    row_number=0,
                    global_camera_code=camera.global_camera_code,
                    errors=[f"duplicate of an earlier row in this file (first seen at row {first})"],
                )
            )
            continue
        seen[camera.global_camera_code] = len(deduped) + 2
        deduped.append(camera)

    codes = [c.global_camera_code for c in deduped]
    existing = set()
    if codes:
        rows = await connection.fetch(
            "SELECT global_camera_code FROM cameras WHERE global_camera_code = ANY($1::text[])",
            codes,
        )
        existing = {row["global_camera_code"] for row in rows}

    to_insert = [c for c in deduped if c.global_camera_code not in existing]
    to_update = [c for c in deduped if c.global_camera_code in existing] if update_existing else []
    skipped = len(existing) - len(to_update)

    if dry_run:
        return BulkImportResult(
            dry_run=True,
            total_rows=len(raw_rows),
            valid_rows=len(deduped),
            inserted=0,
            updated=0,
            skipped_existing=len(existing),
            failed_rows=failures,
            message=(
                f"Validated {len(raw_rows)} row(s): {len(to_insert)} would be created, "
                f"{len(to_update)} updated, {skipped} skipped as already registered, "
                f"{len(failures)} rejected. Nothing was written."
            ),
        )

    inserted = 0
    updated = 0

    # One transaction for the whole file. get_audited_connection has already
    # opened it, so every row lands or none does, and the audit trigger records
    # each one against this operator.
    for camera in to_insert:
        await connection.execute(
            """
            INSERT INTO cameras (
                global_camera_code, department_id, location_geom, azimuth_angle,
                fov_degrees, stream_url, vms_vendor, status, camera_type, make,
                model, serial_number, ip_address, resolution, codec, frame_rate,
                installed_on, owner_org, custodian_name, custodian_contact,
                nvr_reference, nvr_channel, retention_days, site_name, address,
                ward, zone, district, connectivity_type, maintenance_state,
                last_serviced_on, next_service_due, work_order_ref, notes
            )
            VALUES (
                $1, $2, ST_SetSRID(ST_MakePoint($3, $4), 4326), $5,
                $6, $7, $8, $9, $10, $11,
                $12, $13, $14::inet, $15, $16, $17,
                $18, $19, $20, $21,
                $22, $23, $24, $25, $26,
                $27, $28, $29, $30, COALESCE($31, 'OK'),
                $32, $33, $34, $35
            )
            """,
            camera.global_camera_code,
            camera.department_id,
            camera.longitude,
            camera.latitude,
            camera.azimuth_angle,
            camera.fov_degrees,
            camera.stream_url,
            camera.vms_vendor,
            camera.status,
            camera.camera_type,
            camera.make,
            camera.model,
            camera.serial_number,
            camera.ip_address,
            camera.resolution,
            camera.codec,
            camera.frame_rate,
            camera.installed_on,
            camera.owner_org,
            camera.custodian_name,
            camera.custodian_contact,
            camera.nvr_reference,
            camera.nvr_channel,
            camera.retention_days,
            camera.site_name,
            camera.address,
            camera.ward,
            camera.zone,
            camera.district,
            camera.connectivity_type,
            camera.maintenance_state,
            camera.last_serviced_on,
            camera.next_service_due,
            camera.work_order_ref,
            camera.notes,
        )
        inserted += 1

    for camera in to_update:
        await connection.execute(
            """
            UPDATE cameras SET
                location_geom = ST_SetSRID(ST_MakePoint($2, $3), 4326),
                azimuth_angle = COALESCE($4, azimuth_angle),
                fov_degrees   = COALESCE($5, fov_degrees),
                stream_url    = COALESCE($6, stream_url),
                camera_type   = COALESCE($7, camera_type),
                make          = COALESCE($8, make),
                model         = COALESCE($9, model),
                site_name     = COALESCE($10, site_name),
                installed_on  = COALESCE($11, installed_on),
                ward          = COALESCE($12, ward)
            WHERE global_camera_code = $1
            """,
            camera.global_camera_code,
            camera.longitude,
            camera.latitude,
            camera.azimuth_angle,
            camera.fov_degrees,
            camera.stream_url,
            camera.camera_type,
            camera.make,
            camera.model,
            camera.site_name,
            camera.installed_on,
            camera.ward,
        )
        updated += 1

    logger.info(
        "bulk import committed",
        extra={
            "actor": principal.label,
            "inserted": inserted,
            "updated": updated,
            "rejected": len(failures),
        },
    )

    return BulkImportResult(
        dry_run=False,
        total_rows=len(raw_rows),
        valid_rows=len(deduped),
        inserted=inserted,
        updated=updated,
        skipped_existing=skipped,
        failed_rows=failures,
        message=(
            f"Imported {inserted} camera(s), updated {updated}, skipped {skipped} "
            f"already registered, rejected {len(failures)}."
        ),
    )


@router.get(
    "/export.csv",
    summary="Export the registry as CSV",
    response_class=StreamingResponse,
)
async def export_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    department_id: Annotated[str | None, Query(max_length=32)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> StreamingResponse:
    """Export every camera the caller may see, in the import template's shape.

    Export and import share a column set on purpose: an operator can export,
    correct in a spreadsheet, and re-import with `update_existing=true`.
    """
    params: list[Any] = []
    predicates: list[str] = []

    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        predicates.append(f"department_id = ${len(params)}")
        if department_id and department_id.upper() != scope.upper():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"your account is scoped to department '{scope}'",
            )
    elif department_id:
        params.append(department_id.upper())
        predicates.append(f"department_id = ${len(params)}")

    if status_filter:
        params.append(status_filter.upper())
        predicates.append(f"status = ${len(params)}")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    records = await connection.fetch(
        f"""
        SELECT
            global_camera_code, department_id,
            ST_Y(location_geom) AS latitude,
            ST_X(location_geom) AS longitude,
            stream_url, site_name, camera_type, make, model, status,
            azimuth_angle, fov_degrees, resolution, codec, frame_rate,
            ip_address, serial_number, installed_on, owner_org, custodian_name,
            custodian_contact, vms_vendor, nvr_reference, nvr_channel,
            retention_days, address, ward, zone, district, connectivity_type,
            maintenance_state, last_serviced_on, next_service_due,
            work_order_ref, notes
        FROM cameras {where}
        ORDER BY global_camera_code
        """,
        *params,
    )

    redact = not principal.at_least("DEPT_ADMIN")

    def rows():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_TEMPLATE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()

        for record in records:
            buffer.seek(0)
            buffer.truncate(0)
            row = {}
            for column in _TEMPLATE_COLUMNS:
                value = record.get(column)
                if column == "stream_url" and redact:
                    value = "[redacted]"
                if isinstance(value, (date, datetime)):
                    value = value.isoformat()
                row[column] = "" if value is None else str(value)
            writer.writerow(row)
            yield buffer.getvalue()

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="trinetra-cameras-{stamp}.csv"'
        },
    )
