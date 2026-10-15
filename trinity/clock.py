"""Persistable active execution time, using an injected epoch clock."""

import math
import time


def timestamp(now=time.time):
    value = now()
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("clock must return finite epoch seconds")
    return float(value)


def accrue(state, current):
    anchor = state["active_since"]
    if state["status"] == "running" and anchor is not None:
        if current < anchor:
            raise ValueError("clock moved backwards")
        state["active_seconds"] += current - anchor
        state["active_since"] = current
