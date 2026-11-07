"""Errors shared by the control-plane backends (Railway, Docker)."""

from __future__ import annotations


class BackendError(RuntimeError):
    """The platform that runs the instances (Railway / Docker) failed."""


__all__ = ["BackendError"]
