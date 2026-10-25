"""Pick the torch device for local models (Apple GPU first on a Mac)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def pick_device(preference: str = "auto") -> str:
    """Return "mps", "cuda" or "cpu". An explicit preference other than "auto" wins."""
    if preference and preference != "auto":
        return preference
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception as exc:  # torch missing or broken
        logger.debug("torch device probe failed: %s", exc)
    return "cpu"


def use_fp16(device: str, enabled: bool) -> bool:
    """fp16 only pays off (and is only safe) on a GPU."""
    return enabled and device in ("mps", "cuda")
