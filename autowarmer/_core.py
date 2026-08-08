"""Locate the warm engine.

AutoWarmer runs in two shapes: from this repo (the engine lives in the sibling
`autowarmer` package) and from a packaged build (the engine modules are copied in
beside this file). Both are normal; this module hides the difference so nothing
else has to care.
"""
from __future__ import annotations


def _load():
    try:                                   # packaged build: core sits beside us
        from . import device, engine, fleet, humanize, incubation, trace
    except ImportError:                    # repo: core lives in autowarmer/
        from autowarmer import (device, engine, fleet,  # type: ignore
                              humanize, incubation, trace)
    return device, engine, fleet, humanize, incubation, trace


device, engine, fleet, humanize, incubation, trace = _load()

Config = device.Config
GoIos = device.GoIos
Engine = engine.Engine
