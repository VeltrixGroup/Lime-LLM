"""storeguard cloud control plane (multi-tenant SaaS).

This package is the GPU-free control plane that lets storeguard be sold to
many stores: accounts / login, per-tenant configuration, and (in later
phases) camera config, zones, and event storage.  Detection itself stays on
each store's edge PC (see :mod:`storeguard.dashboard` / :mod:`storeguard.pipeline`)
because cameras are only reachable on the store LAN.

Phase 0 (this commit) is the foundation only: a database, the
``Tenant`` / ``User`` / ``Membership`` models, password auth with signed-cookie
sessions, and tenant-scoped queries.  Nothing here imports torch / ultralytics,
so the control plane is cheap to run and deploy.
"""

from __future__ import annotations
