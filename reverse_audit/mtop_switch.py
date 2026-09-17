"""Explicit switch: full-page restock / reverse_audit mutate via signed mtop.

Default OFF = existing DOM / CDP click path (safe fallback).
ON only when env ``ALIBABA_RESTOCK_VIA_MTOP=1`` and/or CLI ``--via-mtop``.

This switch selects transport only. It does **not** approve a send.
Existing ``--i-approve-*`` / ``--i-approve-watchlist-restock`` flags still
gate real POSTs. Checkout / clear-cart / Golden writes / auto_approve stay
forbidden.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

ENV_VIA_MTOP = "ALIBABA_RESTOCK_VIA_MTOP"
CLI_VIA_MTOP = "--via-mtop"
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def via_mtop_enabled(
    *,
    env: Optional[Mapping[str, str]] = None,
    flag: Optional[bool] = None,
) -> bool:
    """Return True only when the operator turned the mtop path on.

    ``flag=True`` (CLI ``--via-mtop``) wins. ``flag=False`` forces DOM.
    Otherwise read ``ALIBABA_RESTOCK_VIA_MTOP``. Missing / empty / unknown
    values stay OFF so the default path is unchanged.
    """
    if flag is True:
        return True
    if flag is False:
        return False
    raw = str((env if env is not None else os.environ).get(ENV_VIA_MTOP) or "")
    text = raw.strip().lower()
    if text in _FALSE:
        return False
    return text in _TRUE


def apply_via_mtop_cli(flag: bool) -> None:
    """Persist ``--via-mtop`` into the env so child processes see it."""
    if flag:
        os.environ[ENV_VIA_MTOP] = "1"
