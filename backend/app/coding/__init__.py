"""Remote web coding agent (parent task 10-19-coding-agent).

One platform-owned AgentCore Runtime per workspace runs ``codex app-server`` behind
a small JSON action protocol (``image/``); members' repos are runtime sessions
on it. This package holds the runtime's image source and build
(``image_build``), its role/runtime provisioning (``infra``), the admin
install/uninstall job (``install``) and status (``status``), the low-level action
call and the repo-level entry with restore folding (``runtime_client``), and
the repos backend: the ledger (``models``), the private-owner choke point and
CRUD (``repos``), turns and their idempotent settlement (``turns``), file
proxies (``files``), presigned S3 URLs and durable snapshots (``s3urls``,
``snapshots``), the SSE event proxy (``stream``), the settling watcher thread
(``watcher``), the byoc deploy handoff (``deploy``) and the ``/api/coding``
routes (``routers``).
"""
