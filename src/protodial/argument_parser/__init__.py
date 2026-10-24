"""Focused command-line parsing implementation."""

from .construction import build_parser
from .parsing import parse_arguments
from .target import parse_target

__all__ = ["build_parser", "parse_arguments", "parse_target"]
