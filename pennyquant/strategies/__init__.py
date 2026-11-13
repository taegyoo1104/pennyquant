from .gap_breakout import GapBreakoutStrategy
from .momentum import MomentumStrategy
from .pullback import PullbackStrategy
from .vwap import VWAPStrategy

STRATEGIES = {
    "momentum": MomentumStrategy,
    "pullback": PullbackStrategy,
    "gap_breakout": GapBreakoutStrategy,
    "vwap": VWAPStrategy,
}

__all__ = ["STRATEGIES", "MomentumStrategy", "PullbackStrategy", "GapBreakoutStrategy", "VWAPStrategy"]
