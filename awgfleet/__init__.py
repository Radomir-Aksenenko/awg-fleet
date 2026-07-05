"""awg-fleet — one AmneziaWG config, a whole fleet of servers behind it.

A control plane that turns several AmneziaWG nodes into a single logical VPN:
one client config points at one domain, and the fleet is health-checked and
steered via DNS so traffic lands on a live, unloaded node with automatic
failover.
"""

__version__ = "0.1.0"
