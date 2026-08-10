"""Deterministic image pipeline: raw frames -> archival master."""

from .run import PipelineConfig, SpreadResult, process_spread, process_session
from . import stages
