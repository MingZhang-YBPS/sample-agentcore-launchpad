"""SCIM 2.0 provisioning (docs/design sso §2.4; SSO-3, RFC 7643 / 7644).

The IdP pushes accounts and groups ahead of sign-in, so a departure is disabled
**now** rather than at the account's next login, and group membership is in place
before the first one:

* **auth** — one Bearer token per provider, minted by an administrator (shown once,
  stored as a SHA-256 hash, rotatable / revocable). The token *is* the provider: the
  same base URL (`<console>/api/scim/v2`) serves every IdP.
* **Users** — create / replace / patch / delete. An account is keyed exactly as at
  sign-in, `(idp, subject)`, with the subject the provider's `scim_subject` attribute
  (`userName` by default — what the SAML NameID / OIDC `sub` carries for most IdPs —
  or `externalId`). A JIT account the IdP later provisions is adopted, not
  duplicated. `active: false` disables the account and bumps its `session_epoch`, so
  every open session ends on its next request; DELETE does the same, drops its SCIM
  group memberships (and so the access they mapped) and answers 404 from then on.
* **Groups** — displayName, externalId, members. Every membership change re-applies
  the provider's group mappings to the members it touched (`sso_mapping.apply`, the
  login's path: only memberships a mapping produced change, manual ones win).

Errors are SCIM error documents (`ScimError`); the router renders them.
"""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.ledger import User
from app.models.sso import (
    ConsoleIdentityProvider,
    SamlSession,
    ScimGroup,
    ScimGroupMember,
    ScimUser,
)
from app.services import sso_mapping
from app.services import users as users_service

SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_SPC = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SUBJECT_ATTRIBUTES = ("userName", "externalId")
TOKEN_PREFIX = "lpscim_"
MAX_PAGE = 200
MAX_MEMBERS = 5000


class ScimError(Exception):
    """A SCIM error answer: HTTP status, `scimType` (RFC 7644 §3.12) and detail."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"schemas": [SCHEMA_ERROR], "status": str(self.status),
                               "detail": self.detail}
        if self.scim_type:
            doc["scimType"] = self.scim_type
        return doc


def _invalid(detail: str) -> ScimError:
    return ScimError(400, detail, "invalidValue")


# --- tokens -------------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rotate_token(row: ConsoleIdentityProvider) -> str:
    """A new bearer token (the previous one stops working); returned once."""
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    row.scim_token_hash = token_hash(token)
    row.scim_token_hint = token[-4:]
    row.scim_token_created_at = datetime.now(UTC)
    return token


def revoke_token(row: ConsoleIdentityProvider) -> None:
    row.scim_token_hash = row.scim_token_hint = None
    row.scim_token_created_at = None


def authenticate(db: Session, authorization: str | None) -> ConsoleIdentityProvider:
    """The provider whose token the `Authorization: Bearer` header carries; 401
    otherwise. Looked up by hash, so no stored value is ever compared in the clear."""
    scheme, _, token = (authorization or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(TOKEN_PREFIX):
        raise ScimError(401, "a SCIM bearer token is required")
    row = db.scalars(select(ConsoleIdentityProvider).where(
        ConsoleIdentityProvider.scim_token_hash == token_hash(token))).first()
    if row is None:
        raise ScimError(401, "the SCIM bearer token is not valid")
    return row


# --- shared helpers -------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    value = users_service.as_utc(value)
    return value.isoformat() if value else None


def _bool(value: Any) -> bool:
    """Entra sends `"False"` / `"True"` strings for booleans."""
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1"}:
            return True
        if value.strip().lower() in {"false", "0"}:
            return False
        raise _invalid(f"'{value}' is not a boolean")
    return bool(value)


def base_url(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).public_base_url.rstrip('/')}/api/scim/v2"


@dataclass(frozen=True)
class Page:
    start: int
    count: int


def page(start_index: int | None, count: int | None) -> Page:
    start = max(1, int(start_index or 1))
    size = MAX_PAGE if count is None else max(0, min(int(count), MAX_PAGE))
    return Page(start, size)


_FILTER_RE = re.compile(r'^\s*([A-Za-z][\w.:]*)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


def parse_filter(text: str | None, allowed: dict[str, str]) -> tuple[str, str] | None:
    """`<attr> eq "<value>"` — the one form IdPs send for matching (RFC 7644 §3.4.2.2);
    returns `(canonical attribute, value)`. Anything else is 400 invalidFilter."""
    if not text or not text.strip():
        return None
    match = _FILTER_RE.match(text)
    key = match.group(1).lower() if match else ""
    if not match or key not in allowed:
        raise ScimError(400, f"unsupported filter: {text[:200]}", "invalidFilter")
    return allowed[key], match.group(2).replace('\\"', '"').replace("\\\\", "\\")


def list_response(resources: list[dict[str, Any]], total: int, start: int) -> dict[str, Any]:
    return {"schemas": [SCHEMA_LIST], "totalResults": total, "startIndex": start,
            "itemsPerPage": len(resources), "Resources": resources}


def service_provider_config(settings: Settings | None = None) -> dict[str, Any]:
    return {
        "schemas": [SCHEMA_SPC],
        "documentationUri": "https://github.com/aws-samples/sample-agentcore-launchpad",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "Bearer token", "primary": True,
            "description": "The provider's SCIM token from Configure → Single sign-on",
        }],
        "meta": {"resourceType": "ServiceProviderConfig",
                 "location": f"{base_url(settings)}/ServiceProviderConfig"},
    }


# --- patch operations --------------------------------------------------------------------------

_VALUE_FILTER_RE = re.compile(r'^(\w+)\[\s*(\w+)\s+eq\s+"([^"]*)"\s*\](?:\.(\w+))?$', re.IGNORECASE)


def _operations(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("Operations"), list):
        raise _invalid("a PatchOp needs an Operations list")
    ops = []
    for op in body["Operations"]:
        if not isinstance(op, dict):
            raise _invalid("every operation must be an object")
        kind = str(op.get("op") or "").lower()
        if kind not in {"add", "replace", "remove"}:
            raise ScimError(400, f"unsupported op '{op.get('op')}'", "invalidSyntax")
        ops.append({"op": kind, "path": str(op.get("path") or "").strip(),
                    "value": op.get("value")})
    return ops


def _set_path(doc: dict[str, Any], path: str, value: Any, remove: bool) -> None:
    """Apply one add / replace / remove to a resource document (`a`, `a.b`,
    `emails[type eq "work"].value`; a schema-URN prefix is dropped)."""
    if path.lower().startswith("urn:"):
        path = path.rsplit(":", 1)[-1]
    match = _VALUE_FILTER_RE.match(path)
    if match:
        attr, key, wanted, sub = match.groups()
        items = [i for i in (doc.get(attr) or []) if isinstance(i, dict)]
        hit = next((i for i in items if str(i.get(key, "")).lower() == wanted.lower()), None)
        if remove:
            doc[attr] = [i for i in items if i is not hit]
            return
        if hit is None:
            hit = {key: wanted}
            items.append(hit)
        if sub:
            hit[sub] = value
        elif isinstance(value, dict):
            hit.update(value)
        doc[attr] = items
        return
    head, _, rest = path.partition(".")
    if rest:
        inner = doc.get(head) if isinstance(doc.get(head), dict) else {}
        _set_path(inner, rest, value, remove)
        doc[head] = inner
        return
    if remove:
        doc.pop(head, None)
    else:
        doc[head] = value


def _patched(doc: dict[str, Any], ops: list[dict[str, Any]],
             skip: set[str] = frozenset()) -> dict[str, Any]:
    """The document with the operations applied, attribute names matched without case
    (Entra writes `Active`); `skip` paths are the caller's (group members)."""
    names = {k.lower(): k for k in doc}

    def canon(path: str) -> str:
        head, dot, rest = path.partition(".")
        bracket = head.find("[")
        bare = head[:bracket] if bracket >= 0 else head
        return names.get(bare.lower(), bare) + head[len(bare):] + dot + rest

    for op in ops:
        path = op["path"]
        if path.split("[")[0].lower() in skip:
            continue
        if not path:
            if not isinstance(op["value"], dict):
                raise _invalid("an operation without a path needs an object value")
            for key, value in op["value"].items():
                if key.lower() in skip:
                    continue
                _set_path(doc, canon(key), value, op["op"] == "remove")
            continue
        _set_path(doc, canon(path), op["value"], op["op"] == "remove")
    return doc


# --- users -------------------------------------------------------------------------------------


def _scim_user(db: Session, row: ConsoleIdentityProvider, user_id: str) -> tuple[User, ScimUser]:
    record = db.get(ScimUser, user_id)
    user = db.get(User, user_id) if record is not None else None
    if (record is None or user is None or record.idp_id != row.id
            or record.deleted_at is not None or user.external_idp_id != row.id):
        # another provider's account, a local one, a deleted one: not this IdP's to see
        raise ScimError(404, f"User {user_id} not found")
    return user, record


def user_resource(db: Session, user: User, record: ScimUser,
                  settings: Settings | None = None) -> dict[str, Any]:
    groups = db.execute(
        select(ScimGroup.id, ScimGroup.display_name)
        .join(ScimGroupMember, ScimGroupMember.group_id == ScimGroup.id)
        .where(ScimGroupMember.user_id == user.id, ScimGroup.idp_id == record.idp_id)
        .order_by(ScimGroup.display_name)
    ).all()
    doc: dict[str, Any] = {
        "schemas": [SCHEMA_USER],
        "id": user.id,
        "userName": record.user_name,
        "displayName": record.display_name or None,
        "name": dict(record.name or {}) or None,
        "emails": [{"value": user.email, "type": "work", "primary": True}] if user.email else [],
        "active": user.status == users_service.STATUS_ACTIVE,
        "groups": [{"value": gid, "display": name} for gid, name in groups],
        "meta": {"resourceType": "User", "created": _iso(record.created_at),
                 "lastModified": _iso(record.updated_at),
                 "location": f"{base_url(settings)}/Users/{user.id}"},
    }
    if record.external_id:
        doc["externalId"] = record.external_id
    return {k: v for k, v in doc.items() if v is not None}


def _primary_email(doc: dict[str, Any]) -> str:
    emails = [e for e in (doc.get("emails") or []) if isinstance(e, dict) and e.get("value")]
    chosen = (next((e for e in emails if _bool(e.get("primary", False))), None)
              or next((e for e in emails if str(e.get("type", "")).lower() == "work"), None)
              or (emails[0] if emails else None))
    if chosen:
        return str(chosen["value"]).strip().lower()
    user_name = str(doc.get("userName") or "")
    return user_name.strip().lower() if "@" in user_name else ""


def _fields(row: ConsoleIdentityProvider, doc: dict[str, Any]) -> dict[str, Any]:
    """The attributes this console keeps, validated."""
    user_name = str(doc.get("userName") or "").strip()
    external_id = doc.get("externalId")
    external_id = str(external_id).strip() if external_id not in (None, "") else None
    if not user_name or len(user_name) > 255:
        raise _invalid("userName is required (at most 255 characters)")
    subject = user_name if row.scim_subject != "externalId" else external_id
    if not subject:
        raise _invalid(f"{row.scim_subject} is required: it is the account's subject")
    name = doc.get("name") if isinstance(doc.get("name"), dict) else {}
    display = str(doc.get("displayName") or name.get("formatted") or "").strip()[:255]
    return {
        "user_name": user_name, "external_id": external_id, "subject": subject[:255],
        "email": _primary_email(doc), "display_name": display,
        "name": {k: str(v)[:255] for k, v in name.items()
                 if k in {"givenName", "familyName", "formatted", "middleName"} and v},
        "active": _bool(doc.get("active", True)),
    }


def _email(db: Session, row: ConsoleIdentityProvider, email: str, settings: Settings,
           user: User | None = None) -> str:
    try:
        checked = sso_mapping.check_email(row, email, settings)
    except AppError as err:
        raise _invalid(f"{err.message} ({err.code})") from err
    holder = users_service.find_by_email(db, checked)
    if holder is not None and (user is None or holder.id != user.id):
        raise ScimError(409, "another console account already uses this email address",
                        "uniqueness")
    return checked


def _set_active(user: User, active: bool, row: ConsoleIdentityProvider) -> bool:
    """Returns whether the account's status changed. Deactivation bumps
    `session_epoch`: every open session ends on its next request (§2.4)."""
    if active and user.status != users_service.STATUS_ACTIVE:
        user.status = users_service.STATUS_ACTIVE
        now = datetime.now(UTC)
        if users_service.is_expired(user):
            # validity lapsed while disabled: a fresh window, as a login would give
            user.expires_at = now + timedelta(days=max(1, int(row.valid_days or 30)))
        else:
            sso_mapping._renew(user, row, now)
        return True
    if not active and user.status == users_service.STATUS_ACTIVE:
        user.status = users_service.STATUS_DISABLED
        user.session_epoch = int(user.session_epoch or 0) + 1
        return True
    return False


def create_user(db: Session, row: ConsoleIdentityProvider, doc: Any,
                settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    """POST /Users. Returns `(resource, journal summary)`."""
    current = settings or get_settings()
    if not isinstance(doc, dict):
        raise _invalid("a User resource must be an object")
    fields = _fields(row, doc)
    user = sso_mapping.find_account(db, row.id, fields["subject"])
    record = db.get(ScimUser, user.id) if user is not None else None
    if record is not None and record.deleted_at is None:
        raise ScimError(409, f"User {fields['user_name']} already exists", "uniqueness")
    if user is None:
        email = _email(db, row, fields["email"], current)
        profile = sso_mapping.ExternalProfile(
            subject=fields["subject"], username=fields["user_name"], email=email,
            name=fields["display_name"])
        try:
            user = sso_mapping.create_account(
                db, row, profile, email, current,
                status=(users_service.STATUS_ACTIVE if fields["active"]
                        else users_service.STATUS_DISABLED))
        except AppError as err:
            raise ScimError(409, err.message, "uniqueness") from err
        # like a JIT login: the mapping owns the new account's role from the start
        # (the default role now, a group's once SCIM adds it to one)
        sso_mapping.apply(db, row, user, [], created=True)
        verb = "create"
    else:
        # a JIT account (or one SCIM deleted) the IdP provisions now: adopt it
        if fields["email"] and fields["email"] != user.email:
            user.email = _email(db, row, fields["email"], current, user)
        _set_active(user, fields["active"], row)
        verb = "adopt"
    if record is None:
        record = ScimUser(user_id=user.id, idp_id=row.id)
        db.add(record)
    record.deleted_at = None
    record.user_name, record.external_id = fields["user_name"], fields["external_id"]
    record.display_name, record.name = fields["display_name"], fields["name"]
    db.commit()
    db.refresh(record)
    return user_resource(db, user, record, current), f"{verb} user:{user.username}"


def get_user(db: Session, row: ConsoleIdentityProvider, user_id: str,
             settings: Settings | None = None) -> dict[str, Any]:
    user, record = _scim_user(db, row, user_id)
    return user_resource(db, user, record, settings)


_USER_FILTERS = {"username": "userName", "externalid": "externalId", "id": "id",
                 "emails.value": "email", "emails": "email"}


def list_users(db: Session, row: ConsoleIdentityProvider, filter_text: str | None,
               start_index: int | None, count: int | None,
               settings: Settings | None = None) -> dict[str, Any]:
    wanted = parse_filter(filter_text, _USER_FILTERS)
    query = (select(User, ScimUser).join(ScimUser, ScimUser.user_id == User.id)
             .where(ScimUser.idp_id == row.id, ScimUser.deleted_at.is_(None)))
    if wanted is not None:
        attr, value = wanted
        column = {"userName": func.lower(ScimUser.user_name),
                  "externalId": ScimUser.external_id, "id": User.id,
                  "email": func.lower(User.email)}[attr]
        query = query.where(column == (value.lower() if attr in {"userName", "email"}
                                       else value))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    at = page(start_index, count)
    rows = db.execute(query.order_by(ScimUser.created_at, User.id)
                      .offset(at.start - 1).limit(at.count)).all()
    return list_response([user_resource(db, u, r, settings) for u, r in rows], total,
                         at.start)


def _update_user(db: Session, row: ConsoleIdentityProvider, user: User, record: ScimUser,
                 doc: dict[str, Any], settings: Settings) -> list[str]:
    fields = _fields(row, doc)
    changed: list[str] = []
    if fields["subject"] != user.external_subject:
        other = sso_mapping.find_account(db, row.id, fields["subject"])
        if other is not None and other.id != user.id:
            raise ScimError(409, "another account already has this subject", "uniqueness")
        user.external_subject = fields["subject"]
        changed.append("subject")
    if fields["email"] and fields["email"] != user.email:
        user.email = _email(db, row, fields["email"], settings, user)
        changed.append("email")
    if _set_active(user, fields["active"], row):
        changed.append("active" if fields["active"] else "deactivated")
    for key in ("user_name", "external_id", "display_name", "name"):
        if getattr(record, key) != fields[key]:
            setattr(record, key, fields[key])
            changed.append(key)
    return changed


def replace_user(db: Session, row: ConsoleIdentityProvider, user_id: str, doc: Any,
                 settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    current = settings or get_settings()
    user, record = _scim_user(db, row, user_id)
    if not isinstance(doc, dict):
        raise _invalid("a User resource must be an object")
    changed = _update_user(db, row, user, record, doc, current)
    db.commit()
    db.refresh(record)
    return (user_resource(db, user, record, current),
            f"update user:{user.username} fields:{','.join(changed) or '-'}")


def patch_user(db: Session, row: ConsoleIdentityProvider, user_id: str, body: Any,
               settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    current = settings or get_settings()
    user, record = _scim_user(db, row, user_id)
    doc = user_resource(db, user, record, current)
    doc = _patched(doc, _operations(body), skip={"groups", "id", "meta", "schemas"})
    changed = _update_user(db, row, user, record, doc, current)
    db.commit()
    db.refresh(record)
    return (user_resource(db, user, record, current),
            f"update user:{user.username} fields:{','.join(changed) or '-'}")


def delete_user(db: Session, row: ConsoleIdentityProvider, user_id: str) -> str:
    """Disable the account, end its sessions, drop its SCIM groups and the access
    those mapped. The row stays (the journal names it); the resource is gone."""
    user, record = _scim_user(db, row, user_id)
    _set_active(user, False, row)
    record.deleted_at = datetime.now(UTC)
    db.execute(delete(ScimGroupMember).where(ScimGroupMember.user_id == user.id))
    session = db.get(SamlSession, user.id)
    if session is not None:
        db.delete(session)
    db.commit()
    sso_mapping.apply(db, row, user, [])
    return f"delete user:{user.username}"


# --- groups ------------------------------------------------------------------------------------


def _group(db: Session, row: ConsoleIdentityProvider, group_id: str) -> ScimGroup:
    group = db.get(ScimGroup, group_id)
    if group is None or group.idp_id != row.id:
        raise ScimError(404, f"Group {group_id} not found")
    return group


def _member_ids(db: Session, group_id: str) -> set[str]:
    return set(db.scalars(select(ScimGroupMember.user_id)
                          .where(ScimGroupMember.group_id == group_id)))


def group_resource(db: Session, group: ScimGroup, settings: Settings | None = None,
                   *, members: bool = True) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemas": [SCHEMA_GROUP],
        "id": group.id,
        "displayName": group.display_name,
        "meta": {"resourceType": "Group", "created": _iso(group.created_at),
                 "lastModified": _iso(group.updated_at),
                 "location": f"{base_url(settings)}/Groups/{group.id}"},
    }
    if group.external_id:
        doc["externalId"] = group.external_id
    if members:
        rows = db.execute(
            select(User.id, User.username)
            .join(ScimGroupMember, ScimGroupMember.user_id == User.id)
            .where(ScimGroupMember.group_id == group.id).order_by(User.username)
        ).all()
        doc["members"] = [{"value": uid, "display": name,
                           "$ref": f"{base_url(settings)}/Users/{uid}"} for uid, name in rows]
    return doc


def _known_users(db: Session, row: ConsoleIdentityProvider, values: Any) -> set[str]:
    """Member ids that are this provider's live SCIM users (others are ignored: an IdP
    may name a member it has not pushed yet)."""
    if values is None:
        return set()
    if not isinstance(values, list):
        values = [values]
    if len(values) > MAX_MEMBERS:
        raise _invalid(f"at most {MAX_MEMBERS} members per request")
    ids = {str(v.get("value")) for v in values if isinstance(v, dict) and v.get("value")}
    if not ids:
        return set()
    return set(db.scalars(select(ScimUser.user_id).where(
        ScimUser.user_id.in_(ids), ScimUser.idp_id == row.id, ScimUser.deleted_at.is_(None))))


def _set_members(db: Session, group: ScimGroup, wanted: set[str]) -> set[str]:
    """Make the group's members `wanted`; returns the users whose membership changed."""
    current = _member_ids(db, group.id)
    for user_id in wanted - current:
        db.add(ScimGroupMember(group_id=group.id, user_id=user_id))
    if current - wanted:
        db.execute(delete(ScimGroupMember).where(
            ScimGroupMember.group_id == group.id,
            ScimGroupMember.user_id.in_(current - wanted)))
    return wanted ^ current


def _reapply(db: Session, row: ConsoleIdentityProvider, user_ids: set[str]) -> None:
    """Membership drives the mappings: re-apply them to every member touched."""
    for user_id in sorted(user_ids):
        user = db.get(User, user_id)
        if user is None or user.external_idp_id != row.id:
            continue
        sso_mapping.apply(db, row, user, sso_mapping.scim_groups(db, row.id, user.id))


def _display_name(db: Session, row: ConsoleIdentityProvider, value: Any,
                  group: ScimGroup | None = None) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 256:
        raise _invalid("displayName is required (at most 256 characters)")
    clash = db.scalars(select(ScimGroup).where(
        ScimGroup.idp_id == row.id, func.lower(ScimGroup.display_name) == name.lower())).first()
    if clash is not None and (group is None or clash.id != group.id):
        raise ScimError(409, f"Group {name} already exists", "uniqueness")
    return name


def create_group(db: Session, row: ConsoleIdentityProvider, doc: Any,
                 settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    if not isinstance(doc, dict):
        raise _invalid("a Group resource must be an object")
    group = ScimGroup(id=uuid.uuid4().hex, idp_id=row.id,
                      display_name=_display_name(db, row, doc.get("displayName")),
                      external_id=(str(doc["externalId"])[:255]
                                   if doc.get("externalId") else None))
    db.add(group)
    db.flush()
    touched = _set_members(db, group, _known_users(db, row, doc.get("members")))
    db.commit()
    _reapply(db, row, touched)
    db.refresh(group)
    return (group_resource(db, group, settings),
            f"create group:{group.display_name} members:{len(touched)}")


def get_group(db: Session, row: ConsoleIdentityProvider, group_id: str,
              settings: Settings | None = None, *, members: bool = True) -> dict[str, Any]:
    return group_resource(db, _group(db, row, group_id), settings, members=members)


_GROUP_FILTERS = {"displayname": "displayName", "externalid": "externalId", "id": "id"}


def list_groups(db: Session, row: ConsoleIdentityProvider, filter_text: str | None,
                start_index: int | None, count: int | None,
                settings: Settings | None = None, *, members: bool = True) -> dict[str, Any]:
    wanted = parse_filter(filter_text, _GROUP_FILTERS)
    query = select(ScimGroup).where(ScimGroup.idp_id == row.id)
    if wanted is not None:
        attr, value = wanted
        if attr == "displayName":
            query = query.where(func.lower(ScimGroup.display_name) == value.lower())
        elif attr == "externalId":
            query = query.where(ScimGroup.external_id == value)
        else:
            query = query.where(ScimGroup.id == value)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    at = page(start_index, count)
    groups = db.scalars(query.order_by(ScimGroup.display_name)
                        .offset(at.start - 1).limit(at.count)).all()
    return list_response([group_resource(db, g, settings, members=members) for g in groups],
                         total, at.start)


def replace_group(db: Session, row: ConsoleIdentityProvider, group_id: str, doc: Any,
                  settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    group = _group(db, row, group_id)
    if not isinstance(doc, dict):
        raise _invalid("a Group resource must be an object")
    renamed = _rename(db, row, group, doc)
    touched = _set_members(db, group, _known_users(db, row, doc.get("members")))
    db.commit()
    _reapply(db, row, touched | (_member_ids(db, group.id) if renamed else set()))
    db.refresh(group)
    return (group_resource(db, group, settings),
            f"update group:{group.display_name} members:{len(touched)}")


def _rename(db: Session, row: ConsoleIdentityProvider, group: ScimGroup,
            doc: dict[str, Any]) -> bool:
    """displayName / externalId from a document; True when a mapping key changed."""
    changed = False
    if "displayName" in doc:
        name = _display_name(db, row, doc.get("displayName"), group)
        changed |= name != group.display_name
        group.display_name = name
    if "externalId" in doc:
        external = str(doc["externalId"])[:255] if doc.get("externalId") else None
        changed |= external != group.external_id
        group.external_id = external
    return changed


def patch_group(db: Session, row: ConsoleIdentityProvider, group_id: str, body: Any,
                settings: Settings | None = None) -> tuple[dict[str, Any], str]:
    """Members by `add` / `remove` (`members[value eq "id"]`, or a value list) /
    `replace`; displayName / externalId by path or as a path-less object."""
    group = _group(db, row, group_id)
    members = _member_ids(db, group.id)
    attrs: dict[str, Any] = {}
    for op in _operations(body):
        path, value = op["path"], op["value"]
        lowered = path.lower()
        if not path and isinstance(value, dict):
            for key, item in value.items():
                if key.lower() == "members":
                    ids = _known_users(db, row, item)
                    members = (members | ids if op["op"] == "add"
                               else members - ids if op["op"] == "remove" else ids)
                elif key.lower() in {"displayname", "externalid"}:
                    attrs[{"displayname": "displayName", "externalid": "externalId"}[
                        key.lower()]] = item
            continue
        if lowered == "members":
            if op["op"] == "remove" and value is None:
                members = set()
                continue
            ids = _known_users(db, row, value)
            members = (members | ids if op["op"] == "add"
                       else members - ids if op["op"] == "remove" else ids)
        elif lowered.startswith("members["):
            match = _VALUE_FILTER_RE.match(path)
            if not match or op["op"] != "remove" or match.group(2).lower() != "value":
                raise ScimError(400, f"unsupported path {path}", "invalidPath")
            members.discard(match.group(3))
        elif lowered in {"displayname", "externalid"}:
            attrs[{"displayname": "displayName", "externalid": "externalId"}[lowered]] = (
                None if op["op"] == "remove" else value)
        else:
            raise ScimError(400, f"unsupported path {path}", "invalidPath")
    if attrs.get("displayName", group.display_name) is None:
        raise _invalid("displayName cannot be removed")
    renamed = _rename(db, row, group, attrs)
    touched = _set_members(db, group, members)
    db.commit()
    _reapply(db, row, touched | (_member_ids(db, group.id) if renamed else set()))
    db.refresh(group)
    return (group_resource(db, group, settings),
            f"update group:{group.display_name} members:{len(touched)}")


def delete_group(db: Session, row: ConsoleIdentityProvider, group_id: str) -> str:
    group = _group(db, row, group_id)
    touched = _member_ids(db, group.id)
    name = group.display_name
    db.execute(delete(ScimGroupMember).where(ScimGroupMember.group_id == group.id))
    db.delete(group)
    db.commit()
    _reapply(db, row, touched)
    return f"delete group:{name} members:{len(touched)}"


def stats(db: Session, row: ConsoleIdentityProvider) -> dict[str, int]:
    """What SCIM has pushed for the provider (the admin page's numbers)."""
    users = db.scalar(select(func.count()).select_from(ScimUser).where(
        ScimUser.idp_id == row.id, ScimUser.deleted_at.is_(None))) or 0
    groups = db.scalar(select(func.count()).select_from(ScimGroup).where(
        ScimGroup.idp_id == row.id)) or 0
    return {"users": int(users), "groups": int(groups)}


def forget_provider(db: Session, idp_id: str) -> None:
    """A deleted provider's SCIM rows (its accounts are gone already: delete refuses
    while any links)."""
    group_ids = select(ScimGroup.id).where(ScimGroup.idp_id == idp_id)
    db.execute(delete(ScimGroupMember).where(ScimGroupMember.group_id.in_(group_ids)))
    db.execute(delete(ScimGroup).where(ScimGroup.idp_id == idp_id))
    db.execute(delete(ScimUser).where(ScimUser.idp_id == idp_id))
