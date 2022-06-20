import os

# use the tests/test.env config fle
# flake8: noqa: E402
import sqlalchemy

os.environ["CONFIG"] = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests/test.env")
)

from psycopg2 import errors
from psycopg2.errorcodes import DEPENDENT_OBJECTS_STILL_EXIST

import pytest

from app.extensions import db
from server import create_app
from init_app import add_sl_domains

app = create_app()
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
app.config["SERVER_NAME"] = "sl.test"

with app.app_context():
    # enable pg_trgm extension
    with db.engine.connect() as conn:
        try:
            conn.execute("DROP EXTENSION if exists pg_trgm")
            conn.execute("CREATE EXTENSION pg_trgm")
        except sqlalchemy.exc.InternalError as e:
            if isinstance(e.orig, errors.lookup(DEPENDENT_OBJECTS_STILL_EXIST)):
                print(">>> pg_trgm can't be dropped, ignore")
            conn.execute("Rollback")

    db.create_all()

    add_sl_domains()


@pytest.fixture
def flask_app():
    yield app


@pytest.fixture
def flask_client():
    with app.app_context():
        # replace db.session to that we can rollback all commits that can be made during a test
        # inspired from http://alexmic.net/flask-sqlalchemy-pytest/
        connection = db.engine.connect()
        transaction = connection.begin()
        options = dict(bind=connection, binds={})
        session = db.create_scoped_session(options=options)
        db.session = session

        try:
            yield app.test_client()
        finally:
            # roll back all commits made during a test
            transaction.rollback()
            connection.close()
            session.remove()
