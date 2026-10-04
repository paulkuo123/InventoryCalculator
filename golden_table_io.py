"""Shared write guard for ``golden_table.json``.

The home page handlers in ``main.py`` and the SKU Mapping workbench service
run in the same threaded HTTP server and both read-modify-write the Golden
Table.  Every such sequence must hold ``GOLDEN_TABLE_LOCK`` from the read to
the final ``os.replace`` so one writer cannot silently drop the other's
changes, and writes must be atomic so a concurrent reader never sees a
half-written file.
"""

import functools
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

# Re-entrant: a workbench decision holds the lock while its helpers write and,
# on failure, restore the previous bytes.
GOLDEN_TABLE_LOCK = threading.RLock()

F = TypeVar("F", bound=Callable[..., Any])


def write_json_atomic(path: Any, data: Any) -> None:
    """Write JSON next to ``path`` and atomically replace it."""
    path = Path(path)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def golden_write(method: F) -> F:
    """Run a SkuMappingService method under the Golden lock.

    The service mutates its cached Golden dict in place before writing.  If
    the method fails, drop the cache so the next read reflects the file on
    disk instead of the half-applied in-memory change.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with GOLDEN_TABLE_LOCK:
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                invalidate = getattr(self, "_invalidate_golden_cache", None)
                if invalidate is not None:
                    invalidate()
                raise

    return wrapper  # type: ignore[return-value]


# Fields that only describe a reviewed SKU choice.  A manual name edit outside
# the workbench makes them stale, so they must not survive the edit.
REVIEWED_MAPPING_FIELDS = (
    "1688_spec_text",
    "1688_dimension_count",
    "1688_mapping_fingerprint",
    "1688_verified_at",
)
UNREVIEWED_EDIT_SOURCE = "manual_edit_unreviewed"


def apply_unreviewed_mapping_edit(model: dict, sku_name: str, sku_second_name: str, sku_id: str = "") -> None:
    """Write a hand-typed 1688 SKU choice as *pending*, never approved.

    Only the SKU Mapping workbench approves mappings.  The restocker falls back
    to ``1688_sku_id`` when a name pair is not on the live page, so a stale ID
    left behind by a rename would silently buy the old SKU; drop it unless the
    editor supplied a new one.
    """
    sku_name = str(sku_name or "").strip()
    sku_second_name = str(sku_second_name or "").strip()
    sku_id = str(sku_id or "").strip()
    for key, value in (
        ("1688_sku_name", sku_name),
        ("1688_sku_second_name", sku_second_name),
        ("1688_sku_id", sku_id),
    ):
        if value:
            model[key] = value
        else:
            model.pop(key, None)
    for key in REVIEWED_MAPPING_FIELDS:
        model.pop(key, None)
    model["1688_mapping_status"] = "pending" if sku_name else "missing"
    model["1688_mapping_source"] = UNREVIEWED_EDIT_SOURCE
