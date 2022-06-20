import uuid

from flask import url_for, g

from app.config import EMAIL_DOMAIN, MAX_NB_EMAIL_FREE_PLAN
from app.extensions import db
from app.models import Alias
from tests.utils import login


def test_success(flask_client):
    login(flask_client)

    r = flask_client.post(
        url_for("api.new_random_alias", hostname="www.test.com"),
    )

    assert r.status_code == 201
    assert r.json["alias"].endswith(EMAIL_DOMAIN)

    # assert returned field
    res = r.json
    assert "id" in res
    assert "email" in res
    assert "creation_date" in res
    assert "creation_timestamp" in res
    assert "nb_forward" in res
    assert "nb_block" in res
    assert "nb_reply" in res
    assert "enabled" in res
    assert "note" in res


def test_custom_mode(flask_client):
    login(flask_client)

    # without note
    r = flask_client.post(
        url_for("api.new_random_alias", hostname="www.test.com", mode="uuid"),
    )

    assert r.status_code == 201
    # extract the uuid part
    alias = r.json["alias"]
    uuid_part = alias[: len(alias) - len(EMAIL_DOMAIN) - 1]
    assert is_valid_uuid(uuid_part)

    # with note
    r = flask_client.post(
        url_for("api.new_random_alias", hostname="www.test.com", mode="uuid"),
        json={"note": "test note"},
    )

    assert r.status_code == 201
    alias = r.json["alias"]
    ge = Alias.get_by(email=alias)
    assert ge.note == "test note"


def test_out_of_quota(flask_client):
    user = login(flask_client)
    user.trial_end = None
    db.session.commit()

    # create MAX_NB_EMAIL_FREE_PLAN random alias to run out of quota
    for _ in range(MAX_NB_EMAIL_FREE_PLAN):
        Alias.create_new(user, prefix="test1")

    r = flask_client.post(
        url_for("api.new_random_alias", hostname="www.test.com"),
    )

    assert r.status_code == 400
    assert (
        r.json["error"] == "You have reached the limitation of a free account with "
        "the maximum of 3 aliases, please upgrade your plan to create more aliases"
    )


def test_too_many_requests(flask_client):
    login(flask_client)

    # to make flask-limiter work with unit test
    # https://github.com/alisaifee/flask-limiter/issues/147#issuecomment-642683820
    g._rate_limiting_complete = False
    for _ in range(7):
        r = flask_client.post(
            url_for("api.new_random_alias", hostname="www.test.com", mode="uuid"),
        )
    # last request
    assert r.status_code == 429
    assert r.json == {"error": "Rate limit exceeded"}


def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False
