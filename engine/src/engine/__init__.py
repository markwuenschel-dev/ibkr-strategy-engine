"""IBKR paper-trading engine.

A deliberately small execution layer over ``ib_async``. The point of this
package is not cleverness -- it is that every path which could send an order to
a broker is guarded, observable, and refuses by default.

Reading order, if you are new here:

``config``   the interlocks. Read this first; it is what makes live trading
             unreachable rather than one flag away.
``safety``   the per-order gates: caps, kill switch, arming.
``journal``  the durable order record. Unlike the rest of the repo's logging,
             a failed write here is fatal.
``broker``   the ib_async wrapper. The only module that talks to TWS.
``alerts``   fills and failures out to the collab-kit outbox -> Telegram.
``cli``      ``engine status | quote | preview | trade``.

Milestones this package was built against (see the plan):
M1 connect read-only, M2 market data, M3 order preview, M4 one armed order.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
