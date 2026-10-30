"""Resolve an addcargo ``specId`` by SKU name when the offer has no 1688 skuId.

Offline-only helper: it reads the same read-only ``skuMapOriginal`` block as
:mod:`reverse_audit.mtop_sku_map`, but matches by the human SKU display name
(primary + optional second dimension) or by an explicit spec text string.

Rules are intentionally strict: a candidate must match the *normalized* tuple
of display parts exactly (same length, same order). No prefix / substring /
fuzzy / simplified-traditional folding. Multiple matches are always an error —
we never guess which specId the operator meant.

Never writes golden. Never launches Chrome. Never makes a network request.
"""
from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from reverse_audit.mtop_sku_map import SkuMapError, iter_sku_map_original
from sku_spec import split_spec_dimensions

_MAX_OPTIONS_SHOWN = 60
_MAX_TEXT_SHOWN = 80


class SpecNameError(SkuMapError):
    """Name-based spec resolution could not pick exactly one specId.

    ``reason`` is one of: ``no_rows``, ``no_wanted``, ``second_without_primary``,
    ``no_match``, ``ambiguous``, ``name_text_conflict``. ``candidates`` holds the ambiguous matches.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "no_match",
        candidates: Optional[Sequence["SpecOption"]] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.candidates = list(candidates or [])


@dataclass
class SpecOption:
    """One ``skuMapOriginal`` row that has a usable ``specId``."""

    spec_id: str
    sku_id: str
    spec_text: str
    parts: List[str] = field(default_factory=list)


def _row_spec_id(row: Dict[str, Any]) -> str:
    return str(row.get("specId") or row.get("spec_id") or "").strip()


def _row_sku_id(row: Dict[str, Any]) -> str:
    return str(row.get("skuId") or row.get("sku_id") or "").strip()


def _row_spec_text(row: Dict[str, Any]) -> str:
    raw = (
        row.get("specAttrs")
        or row.get("specText")
        or row.get("spec_text")
        or ""
    )
    return html.unescape(str(raw))


def _options_from_rows(rows: Iterable[Dict[str, Any]]) -> List[SpecOption]:
    options: List[SpecOption] = []
    seen: set[str] = set()
    for row in rows:
        spec_id = _row_spec_id(row)
        if not spec_id or spec_id in seen:
            continue
        seen.add(spec_id)
        spec_text = _row_spec_text(row)
        parts = [str(part).strip() for part in split_spec_dimensions(spec_text)]
        options.append(
            SpecOption(
                spec_id=spec_id,
                sku_id=_row_sku_id(row),
                spec_text=spec_text,
                parts=parts,
            )
        )
    return options


def list_spec_options(html_text: str) -> List[SpecOption]:
    """Return deduped ``SpecOption`` rows (by specId) from detail HTML."""
    return _options_from_rows(iter_sku_map_original(str(html_text or "")))


def normalize_option(text: Any) -> str:
    """NFKC + no-whitespace + casefold. Nothing else (no fuzzy folding)."""
    value = html.unescape(str(text or ""))
    value = unicodedata.normalize("NFKC", value)
    value = "".join(value.split())
    return value.casefold()


def _norm_parts(parts: Sequence[Any]) -> Tuple[str, ...]:
    return tuple(normalize_option(part) for part in parts)


def _truncate(text: str, limit: int = _MAX_TEXT_SHOWN) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _format_option(option: SpecOption) -> str:
    return (
        f"{_truncate(option.spec_text)!r} "
        f"(specId={option.spec_id}, skuId={option.sku_id or '-'})"
    )


def _format_options(options: Sequence[SpecOption]) -> str:
    shown = list(options[:_MAX_OPTIONS_SHOWN])
    rendered = ", ".join(_format_option(opt) for opt in shown)
    if len(options) > _MAX_OPTIONS_SHOWN:
        rendered += f", … (+{len(options) - _MAX_OPTIONS_SHOWN} more)"
    return rendered or "(none)"


def _wanted_description(
    sku_name: Optional[str],
    sku_second_name: Optional[str],
    spec_text: Optional[str],
) -> str:
    wanted: List[str] = []
    if sku_name:
        wanted.append(f"--sku-name={sku_name!r}")
    if sku_second_name:
        wanted.append(f"--sku-second-name={sku_second_name!r}")
    if spec_text:
        wanted.append(f"--spec-text={spec_text!r}")
    return " or ".join(wanted) or "(empty)"


def resolve_spec_by_name(
    html_text: str,
    *,
    sku_name: Optional[str] = None,
    sku_second_name: Optional[str] = None,
    spec_text: Optional[str] = None,
) -> SpecOption:
    """Return the single ``SpecOption`` matching the normalized wanted tuple.

    ``sku_name`` (plus optional ``sku_second_name``) builds one wanted tuple.
    ``spec_text`` is split with :func:`sku_spec.split_spec_dimensions` after
    HTML unescape and builds an alternative wanted tuple; the normalized whole
    raw spec text (separators included) is also accepted. If both are given,
    each must resolve to exactly one option and it must be the same one. 0 matches
    or >1 matches raise :class:`SpecNameError`; we never pick a winner.
    """
    name = str(sku_name or "").strip()
    second = str(sku_second_name or "").strip()
    text = str(spec_text or "").strip()

    if second and not name:
        raise SpecNameError(
            "--sku-second-name needs --sku-name (cannot match a second dimension alone)",
            reason="second_without_primary",
        )
    if not name and not text:
        raise SpecNameError(
            "need --sku-name (with optional --sku-second-name) or --spec-text",
            reason="no_wanted",
        )

    rows = list(iter_sku_map_original(str(html_text or "")))
    if not rows:
        raise SpecNameError(
            "detail HTML has no skuMapOriginal (likely login / x5 punish / captcha "
            "page); do not guess. Use a saved logged-in page or an explicit --spec-id.",
            reason="no_rows",
        )
    options = _options_from_rows(rows)

    name_desc = _wanted_description(name, second, None)
    text_desc = _wanted_description(None, None, text)
    if name and text:
        # Both given: each must resolve on its own to exactly one spec, and it
        # must be the same spec. Never fall back to whichever one matched.
        name_hits = _match_name(options, name, second)
        text_hits = _match_spec_text(options, text)
        if (
            len(name_hits) == 1
            and len(text_hits) == 1
            and name_hits[0].spec_id == text_hits[0].spec_id
        ):
            return name_hits[0]
        raise SpecNameError(
            "--sku-name/--sku-second-name and --spec-text must both resolve to the "
            "same single spec option; refusing to guess. "
            f"{name_desc} -> {_describe_hits(name_hits)}; "
            f"{text_desc} -> {_describe_hits(text_hits)}",
            reason="name_text_conflict",
            candidates=_dedupe(name_hits + text_hits),
        )
    if name:
        return _single(_match_name(options, name, second), name_desc, options)
    return _single(_match_spec_text(options, text), text_desc, options)


def _match_name(
    options: Sequence[SpecOption], name: str, second: str
) -> List[SpecOption]:
    wanted = _norm_parts([name] + ([second] if second else []))
    return [opt for opt in options if _norm_parts(opt.parts) == wanted]


def _match_spec_text(options: Sequence[SpecOption], text: str) -> List[SpecOption]:
    wanted = _norm_parts(split_spec_dimensions(html.unescape(text)))
    # Whole-string alternative: the normalized raw spec text (separators
    # included) must equal the option's normalized raw spec text.
    whole = normalize_option(text)
    return [
        opt
        for opt in options
        if _norm_parts(opt.parts) == wanted or normalize_option(opt.spec_text) == whole
    ]


def _dedupe(options: Sequence[SpecOption]) -> List[SpecOption]:
    seen: set[str] = set()
    out: List[SpecOption] = []
    for opt in options:
        if opt.spec_id not in seen:
            seen.add(opt.spec_id)
            out.append(opt)
    return out


def _describe_hits(hits: Sequence[SpecOption]) -> str:
    if not hits:
        return "no match"
    if len(hits) == 1:
        return _format_option(hits[0])
    return f"{len(hits)} matches: {_format_options(hits)}"


def _single(
    candidates: List[SpecOption], wanted_desc: str, options: Sequence[SpecOption]
) -> SpecOption:
    if not candidates:
        raise SpecNameError(
            f"no spec option matched wanted {wanted_desc}. Available: "
            f"{_format_options(options)}",
            reason="no_match",
        )
    if len(candidates) > 1:
        raise SpecNameError(
            f"wanted {wanted_desc} matched {len(candidates)} spec options; refusing "
            f"to guess. Candidates: {_format_options(candidates)}",
            reason="ambiguous",
            candidates=candidates,
        )
    return candidates[0]
