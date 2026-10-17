"""Synthetic world generator (design §3): forward simulation, ground truth, vendor feeds."""

from radar.sim.feed import generate
from radar.sim.names import INJECTION_ACCOUNT_NAME, VP_CASE_NAMES
from radar.sim.world import simulate, staff

__all__ = ["INJECTION_ACCOUNT_NAME", "VP_CASE_NAMES", "generate", "simulate", "staff"]
