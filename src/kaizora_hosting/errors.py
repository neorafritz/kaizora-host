"""Errors that are safe to present directly to CLI users."""


class KaizoraError(Exception):
    """An expected, user-correctable operational error."""
