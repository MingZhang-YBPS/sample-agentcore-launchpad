"""Console identity -> Cognito Gateway policy principal."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.core.errors import AppError
from app.services import aws_clients, policy_identity
from tests.conftest import ws_ctx


def _jwt(username: str, groups: list[str], exp: int | None = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "username": username,
                "cognito:groups": groups,
                "exp": exp or int(time.time()) + 3600,
            }
        ).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _settings(password: str | None = "console-secret"):
    return SimpleNamespace(auth_password=SecretStr(password) if password else None)


WS = ws_ctx({"user_pool_id": "pool", "user_pool_client_id": "client"})


@pytest.fixture(autouse=True)
def _clear_cache():
    policy_identity._token_cache.clear()
    yield
    policy_identity._token_cache.clear()


def test_open_console_keeps_m2m_without_cognito(monkeypatch):
    monkeypatch.setattr(policy_identity, "get_settings", lambda: _settings(None))
    monkeypatch.setattr(
        aws_clients,
        "client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AWS call")),
    )
    assert policy_identity.gateway_user_token(WS, "river", "admin") is None


def test_demo_user_uses_bootstrap_password_and_reconciles_group(monkeypatch):
    token = _jwt("demo", ["hr-analyst", "lp-role-member"])
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "demo",
        "UserAttributes": [{"Name": "email", "Value": "demo@launchpad.local"}],
    }
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "platform-admin"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(
        policy_identity,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"demo": "bootstrap-password"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    assert policy_identity.gateway_user_token(WS, "demo", "member") == token
    assert cognito.initiate_auth.call_args.kwargs["AuthParameters"] == {
        "USERNAME": "demo",
        "PASSWORD": "bootstrap-password",
    }
    cognito.admin_remove_user_from_group.assert_called_once_with(
        UserPoolId="pool", Username="demo", GroupName="platform-admin"
    )
    added = [c.kwargs for c in cognito.admin_add_user_to_group.call_args_list]
    assert added == [
        {"UserPoolId": "pool", "Username": "demo", "GroupName": "hr-analyst"},
        {"UserPoolId": "pool", "Username": "demo", "GroupName": "lp-role-member"},
    ]


def test_a_non_default_workspace_ignores_the_hubs_demo_passwords(monkeypatch):
    """`demo_users.passwords` names users only `make bootstrap` creates, in the
    hub's pool. A console-registered workspace has the groups but no lab users, so
    the same caller must get a shadow identity in that pool instead of a 503."""
    token = _jwt("admin", ["platform-admin", "lp-role-admin"])
    cognito = MagicMock()
    cognito.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "missing"}},
        "AdminGetUser",
    )
    cognito.admin_create_user.return_value = {
        "User": {
            "Attributes": [
                {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER}
            ]
        }
    }
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(
        policy_identity,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"admin": "hub-pool-password"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)
    other = ws_ctx({"user_pool_id": "pool", "user_pool_client_id": "client"}, id="acct2")

    assert policy_identity.gateway_user_token(other, "admin", "admin") == token
    assert (
        cognito.initiate_auth.call_args.kwargs["AuthParameters"]["PASSWORD"]
        != "hub-pool-password"
    )
    cognito.admin_create_user.assert_called_once()


def test_new_console_user_creates_marked_shadow_identity(monkeypatch):
    token = _jwt("clare", ["platform-admin", "lp-role-admin"])
    cognito = MagicMock()
    cognito.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "missing"}},
        "AdminGetUser",
    )
    cognito.admin_create_user.return_value = {
        "User": {
            "Attributes": [
                {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER}
            ]
        }
    }
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    assert (
        policy_identity.gateway_user_token(WS, "clare", "admin", "clare@example.com")
        == token
    )
    attributes = cognito.admin_create_user.call_args.kwargs["UserAttributes"]
    assert {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER} in attributes
    password = cognito.admin_set_user_password.call_args.kwargs["Password"]
    assert password != "console-secret" and len(password) >= 12


def test_a_longer_validity_margin_re_mints_a_token_about_to_expire(monkeypatch):
    """The managed COA gateway's consent is completed with the very JWT it was asked
    with, so its caller asks for a token that outlives the consent session: a cached
    token closer to expiry than that is replaced, the default margin keeps it."""
    fresh = _jwt("clare", ["platform-admin", "lp-role-admin"])
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "clare",
        "UserAttributes": [
            {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER}
        ],
    }
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "platform-admin"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": fresh, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)
    # keyed on the project groups too (V3 R10-gw); clare is in no isolated project
    key = (WS.account_id, WS.region, "clare", "admin", ())
    policy_identity._token_cache[key] = {"token": "cached", "expires_at": time.time() + 600}

    assert policy_identity.gateway_user_token(WS, "clare", "admin") == "cached"
    cognito.initiate_auth.assert_not_called()
    assert policy_identity.gateway_user_token(WS, "clare", "admin", min_valid_s=960) == fresh
    cognito.initiate_auth.assert_called_once()
    # the fresh token is what every later caller gets, the completion included
    assert policy_identity.gateway_user_token(WS, "clare", "admin") == fresh
    cognito.initiate_auth.assert_called_once()


def test_unmarked_existing_cognito_user_is_not_adopted(monkeypatch):
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "clare",
        "UserAttributes": [{"Name": "email", "Value": "clare@example.com"}],
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    with pytest.raises(AppError) as error:
        policy_identity.gateway_user_token(WS, "clare", "member")
    assert error.value.code == "policy.identity_conflict"
    cognito.admin_set_user_password.assert_not_called()


def test_mismatched_jwt_claims_are_rejected(monkeypatch):
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "demo",
        "UserAttributes": [],
    }
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "hr-analyst"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": _jwt("river", ["platform-admin"]),
            "ExpiresIn": 3600,
        }
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(
        policy_identity,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"demo": "pw"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    with pytest.raises(AppError) as error:
        policy_identity.gateway_user_token(WS, "demo", "member")
    assert error.value.code == "policy.identity_invalid"


# ── role groups for every platform role (DBUD rule 7) ────────────────────────


def test_every_platform_role_maps_to_its_group_and_only_admin_member_keep_legacy():
    from app.services.users import ROLES

    assert set(policy_identity.PLATFORM_ROLES) == set(ROLES)
    assert policy_identity.role_groups("admin") == {"lp-role-admin", "platform-admin"}
    assert policy_identity.role_groups("member") == {"lp-role-member", "hr-analyst"}
    # least privilege: no legacy group, so no existing policy admits them by accident
    assert policy_identity.role_groups("operator") == {"lp-role-operator"}
    assert policy_identity.role_groups("steward") == {"lp-role-steward"}
    assert policy_identity.role_groups("root") == frozenset()
    # bootstrap creates every one of them (workspace_bootstrap iterates POLICY_GROUPS)
    assert {"platform-admin", "hr-analyst", "lp-role-admin", "lp-role-operator",
            "lp-role-member", "lp-role-steward"} == policy_identity.POLICY_GROUPS


@pytest.mark.parametrize("role", ["operator", "steward"])
def test_operator_and_steward_get_a_token_instead_of_a_500(monkeypatch, role):
    token = _jwt("olive", [f"lp-role-{role}"])
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "olive",
        "UserAttributes": [
            {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER}
        ],
    }
    # a stale legacy group from when olive was an admin, and an unmanaged one
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "platform-admin"}, {"GroupName": "lp-role-admin"},
                   {"GroupName": "someone-elses"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    assert policy_identity.gateway_user_token(WS, "olive", role) == token
    removed = {c.kwargs["GroupName"] for c in cognito.admin_remove_user_from_group.call_args_list}
    added = {c.kwargs["GroupName"] for c in cognito.admin_add_user_to_group.call_args_list}
    assert removed == {"platform-admin", "lp-role-admin"}
    assert added == {f"lp-role-{role}"}


def test_a_missing_role_group_is_created_once_then_joined():
    cognito = MagicMock()
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    missing = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
                          "AdminAddUserToGroup")
    calls: list[str] = []

    def add(**kwargs):
        calls.append(kwargs["GroupName"])
        if kwargs["GroupName"] == "lp-role-operator" and calls.count("lp-role-operator") == 1:
            raise missing

    cognito.admin_add_user_to_group.side_effect = add
    assert policy_identity._reconcile_group(cognito, pool_id="p", username="olive",
                                            role="operator") == {"lp-role-operator"}
    cognito.create_group.assert_called_once_with(
        UserPoolId="p", GroupName="lp-role-operator",
        Description="Launchpad role: lp-role-operator")
    assert calls == ["lp-role-operator", "lp-role-operator"]
    # a missing legacy / project group is the bootstrap's: not created here
    cognito.reset_mock()
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    cognito.admin_add_user_to_group.side_effect = missing
    with pytest.raises(ClientError):
        policy_identity._reconcile_group(cognito, pool_id="p", username="demo", role="member")
    cognito.create_group.assert_not_called()


def test_an_unknown_role_is_still_refused():
    with pytest.raises(AppError) as error:
        policy_identity._reconcile_group(MagicMock(), pool_id="p", username="x", role="root")
    assert error.value.code == "policy.identity_role_invalid"


def test_a_token_missing_the_role_group_is_rejected(monkeypatch):
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "demo", "UserAttributes": [],
    }
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": _jwt("demo", ["hr-analyst"]), "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config",
                        lambda: {"demo_users": {"passwords": {"demo": "pw"}}})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)
    with pytest.raises(AppError) as error:
        policy_identity.gateway_user_token(WS, "demo", "member")
    assert error.value.code == "policy.identity_invalid"
