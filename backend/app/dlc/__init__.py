"""Agent-DLC: criteria, gates, calibration, golden sets, admission and watch.

See docs/agent-dlc-design.md. Services here take a SQLAlchemy session and plain
data; routes in `app/routers/dlc.py` do auth, workspace scoping and AWS clients.
"""
