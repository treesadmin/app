from flask import g
from flask import jsonify, request

from app.api.base import api_bp, require_api_auth
from app.api.serializer import (
    get_alias_info_v2,
    serialize_alias_info_v2,
)
from app.config import MAX_NB_EMAIL_FREE_PLAN, ALIAS_LIMIT
from app.extensions import db, limiter
from app.log import LOG
from app.models import Alias, AliasUsedOn, AliasGeneratorEnum


@api_bp.route("/alias/random/new", methods=["POST"])
@limiter.limit(ALIAS_LIMIT)
@require_api_auth
def new_random_alias():
    """
    Create a new random alias
    Input:
        (Optional) note
    Output:
        201 if success

    """
    user = g.user
    if not user.can_create_new_alias():
        LOG.d("user %s cannot create new random alias", user)
        return (
            jsonify(
                error=f"You have reached the limitation of a free account with the maximum of "
                f"{MAX_NB_EMAIL_FREE_PLAN} aliases, please upgrade your plan to create more aliases"
            ),
            400,
        )

    note = data.get("note") if (data := request.get_json(silent=True)) else None
    scheme = user.alias_generator
    if mode := request.args.get("mode"):
        if mode == "word":
            scheme = AliasGeneratorEnum.word.value
        elif mode == "uuid":
            scheme = AliasGeneratorEnum.uuid.value
        else:
            return jsonify(error=f"{mode} must be either word or uuid"), 400

    alias = Alias.create_new_random(user=user, scheme=scheme, note=note)
    db.session.commit()

    if hostname := request.args.get("hostname"):
        AliasUsedOn.create(alias_id=alias.id, hostname=hostname, user_id=alias.user_id)
        db.session.commit()

    return (
        jsonify(alias=alias.email, **serialize_alias_info_v2(get_alias_info_v2(alias))),
        201,
    )
