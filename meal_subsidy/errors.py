"""Compatibility shim for callers that import domain errors from this module.

The canonical definitions live in :mod:`meal_subsidy.services`; this module
only re-exports them under their own import path.
"""

from __future__ import annotations

from .services import (
    BatchConflict,
    BatchConflictError,
    BatchRejected,
    BatchStaleError,
    ConflictError,
    StaleBatchError,
    StaleBatchRejected,
)

__all__ = [
    "BatchRejected",
    "StaleBatchRejected",
    "StaleBatchError",
    "BatchStaleError",
    "BatchConflict",
    "BatchConflictError",
    "ConflictError",
]
