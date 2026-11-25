"""EM2 shopping runs: em/harness_tinker.py, reused unchanged, for this phase's four arms.

em/harness_tinker.py checks the arm name against the EM phase's arms (base, insecure, secure)
inside `user_agent`, which `run_one` and `make_fetch` call. Rather than copy those two
functions, they are rebound here to the very same code objects with one name changed:
`user_agent` accepts this phase's arms. Tool, prompt, caps, error texts, parsing, retries and
the transcript shape are therefore byte-for-byte the EM phase's.

Program name, as TEST-PLAN-EM.md fixes it: `listing-test-harness/1.0 (+tinker:<arm>)`.
"""
import types

import em2
from em import harness_tinker as _em

HARNESS_VERSION = _em.HARNESS_VERSION
TEMPERATURE = _em.TEMPERATURE
MAX_TOKENS_PER_CALL = _em.MAX_TOKENS_PER_CALL
BAD_ARGS = _em.BAD_ARGS
TOOL_SPECS = _em.TOOL_SPECS
make_api = _em.make_api                 # checks only "base has no checkpoint, others do"
cost_usd = _em.cost_usd


def user_agent(arm):
    if arm not in em2.ARMS:
        raise ValueError("unknown arm %r" % arm)
    return "listing-test-harness/1.0 (+tinker:%s)" % arm


_globals = dict(vars(_em), user_agent=user_agent)


def _rebind(fn):
    return types.FunctionType(fn.__code__, _globals, fn.__name__, fn.__defaults__, fn.__closure__)


run_one = _rebind(_em.run_one)
make_fetch = _rebind(_em.make_fetch)
