from flask import g

from app.alias_utils import delete_alias
from app.config import EMAIL_DOMAIN, MAX_NB_EMAIL_FREE_PLAN
from app.dashboard.views.custom_alias import signer
from app.extensions import db
from app.models import Alias, CustomDomain, Mailbox, AliasUsedOn
from app.utils import random_word
from tests.utils import login


def test_v2(flask_client):
    login(flask_client)

    word = random_word()
    suffix = f".{word}@{EMAIL_DOMAIN}"
    signed_suffix = signer.sign(suffix).decode()

    r = flask_client.post(
        "/api/v2/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
        },
    )

    assert r.status_code == 201
    assert r.json["alias"] == f"prefix.{word}@{EMAIL_DOMAIN}"

    res = r.json
    assert "id" in res
    assert "email" in res
    assert "creation_date" in res
    assert "creation_timestamp" in res
    assert "nb_forward" in res
    assert "nb_block" in res
    assert "nb_reply" in res
    assert "enabled" in res

    new_alias: Alias = Alias.get_by(email=r.json["alias"])
    assert len(new_alias.mailboxes) == 1


def test_minimal_payload(flask_client):
    user = login(flask_client)

    word = random_word()
    suffix = f".{word}@{EMAIL_DOMAIN}"
    signed_suffix = signer.sign(suffix).decode()

    r = flask_client.post(
        "/api/v3/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "mailbox_ids": [user.default_mailbox_id],
        },
    )

    assert r.status_code == 201
    assert r.json["alias"] == f"prefix.{word}@{EMAIL_DOMAIN}"

    res = r.json
    assert "id" in res
    assert "email" in res
    assert "creation_date" in res
    assert "creation_timestamp" in res
    assert "nb_forward" in res
    assert "nb_block" in res
    assert "nb_reply" in res
    assert "enabled" in res

    new_alias: Alias = Alias.get_by(email=r.json["alias"])
    assert len(new_alias.mailboxes) == 1


def test_full_payload(flask_client):
    """Create alias with:
    - additional mailbox
    - note
    - name
    - hostname (in URL)
    """

    user = login(flask_client)

    # create another mailbox
    mb = Mailbox.create(user_id=user.id, email="abcd@gmail.com", verified=True)
    db.session.commit()

    word = random_word()
    suffix = f".{word}@{EMAIL_DOMAIN}"
    signed_suffix = signer.sign(suffix).decode()

    assert AliasUsedOn.query.count() == 0

    r = flask_client.post(
        "/api/v3/alias/custom/new?hostname=example.com",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "note": "test note",
            "mailbox_ids": [user.default_mailbox_id, mb.id],
            "name": "your name",
        },
    )

    assert r.status_code == 201
    assert r.json["alias"] == f"prefix.{word}@{EMAIL_DOMAIN}"

    # assert returned field
    res = r.json
    assert res["note"] == "test note"
    assert res["name"] == "your name"

    new_alias: Alias = Alias.get_by(email=r.json["alias"])
    assert new_alias.note == "test note"
    assert len(new_alias.mailboxes) == 2

    alias_used_on = AliasUsedOn.first()
    assert alias_used_on.alias_id == new_alias.id
    assert alias_used_on.hostname == "example.com"


def test_custom_domain_alias(flask_client):
    user = login(flask_client)

    # create a custom domain
    CustomDomain.create(user_id=user.id, domain="ab.cd", verified=True, commit=True)

    signed_suffix = signer.sign("@ab.cd").decode()

    r = flask_client.post(
        "/api/v3/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "mailbox_ids": [user.default_mailbox_id],
        },
    )

    assert r.status_code == 201
    assert r.json["alias"] == "prefix@ab.cd"


def test_out_of_quota(flask_client):
    user = login(flask_client)
    user.trial_end = None
    db.session.commit()

    # create MAX_NB_EMAIL_FREE_PLAN custom alias to run out of quota
    for _ in range(MAX_NB_EMAIL_FREE_PLAN):
        Alias.create_new(user, prefix="test")

    word = random_word()
    suffix = f".{word}@{EMAIL_DOMAIN}"
    signed_suffix = signer.sign(suffix).decode()

    r = flask_client.post(
        "/api/v3/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "note": "test note",
            "mailbox_ids": [user.default_mailbox_id],
            "name": "your name",
        },
    )

    assert r.status_code == 400
    assert r.json == {
        "error": "You have reached the limitation of a "
        "free account with the maximum of 3 aliases, please upgrade your plan to create more aliases"
    }


def test_cannot_create_alias_in_trash(flask_client):
    user = login(flask_client)

    # create a custom domain
    CustomDomain.create(user_id=user.id, domain="ab.cd", verified=True, commit=True)

    signed_suffix = signer.sign("@ab.cd").decode()

    r = flask_client.post(
        "/api/v3/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "mailbox_ids": [user.default_mailbox_id],
        },
    )

    assert r.status_code == 201
    assert r.json["alias"] == "prefix@ab.cd"

    # delete alias: it's going to be moved to ab.cd trash
    alias = Alias.get_by(email="prefix@ab.cd")
    assert alias.custom_domain_id
    delete_alias(alias, user)

    # try to create the same alias, will fail as the alias is in trash
    r = flask_client.post(
        "/api/v3/alias/custom/new",
        json={
            "alias_prefix": "prefix",
            "signed_suffix": signed_suffix,
            "mailbox_ids": [user.default_mailbox_id],
        },
    )
    assert r.status_code == 409


def test_too_many_requests(flask_client):
    user = login(flask_client)

    # create a custom domain
    CustomDomain.create(user_id=user.id, domain="ab.cd", verified=True, commit=True)

    # to make flask-limiter work with unit test
    # https://github.com/alisaifee/flask-limiter/issues/147#issuecomment-642683820
    g._rate_limiting_complete = False
    for i in range(7):
        signed_suffix = signer.sign("@ab.cd").decode()

        r = flask_client.post(
            "/api/v3/alias/custom/new",
            json={
                "alias_prefix": f"prefix{i}",
                "signed_suffix": signed_suffix,
                "mailbox_ids": [user.default_mailbox_id],
            },
        )

    # last request
    assert r.status_code == 429
    assert r.json == {"error": "Rate limit exceeded"}
