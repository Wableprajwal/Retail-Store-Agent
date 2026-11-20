"""
Seeded fault injection for write tools.

Wraps a dispatch_fn(name, input, ctx) and randomly misbehaves on write tools:
  lost_ack            run the tool, then report a timeout (outcome unknown)
  duplicate_delivery  run the tool twice with the same ctx, return the second result
  malformed           do not run the tool, report a malformed response (safe to retry)
"""

import copy
import random

try:
    from guard.dispatch import WRITE_TOOLS
except ImportError:
    # Same set as guard.dispatch.WRITE_TOOLS (state-changing tools).
    WRITE_TOOLS = {"create_order", "create_return", "create_restock_orders",
                   "receive_purchase_order", "create_promotion"}


class FaultInjector:
    def __init__(self, seed, p_lost_ack=0.25, p_duplicate_delivery=0.15, p_malformed=0.10):
        self.rng = random.Random(seed)
        self.p_lost_ack = p_lost_ack
        self.p_duplicate_delivery = p_duplicate_delivery
        self.p_malformed = p_malformed
        self.stats = {"lost_ack": 0, "duplicate_delivery": 0, "malformed": 0}

    def wrap(self, dispatch_fn):
        def faulty(name, tool_input, ctx):
            if name not in WRITE_TOOLS:
                return dispatch_fn(name, tool_input, ctx)
            r = self.rng.random()
            if r < self.p_malformed:
                self.stats["malformed"] += 1
                return {"error": "malformed response from tool backend"}
            r -= self.p_malformed
            if r < self.p_lost_ack:
                self.stats["lost_ack"] += 1
                dispatch_fn(name, copy.deepcopy(tool_input), ctx)
                return {"error": "timeout: tool did not respond in 10s; outcome unknown"}
            r -= self.p_lost_ack
            if r < self.p_duplicate_delivery:
                self.stats["duplicate_delivery"] += 1
                dispatch_fn(name, copy.deepcopy(tool_input), ctx)
                return dispatch_fn(name, copy.deepcopy(tool_input), ctx)
            return dispatch_fn(name, tool_input, ctx)
        return faulty

    def total(self):
        return sum(self.stats.values())
