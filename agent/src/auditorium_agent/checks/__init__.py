"""The checks. Each takes a transaction and the books, and returns one CheckResult."""

from .period import check_period

__all__ = ["check_period"]
