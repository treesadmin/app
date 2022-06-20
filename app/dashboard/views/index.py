from dataclasses import dataclass

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app import alias_utils
from app.api.serializer import get_alias_infos_with_pagination_v3, get_alias_info_v3
from app.config import PAGE_LIMIT, ALIAS_LIMIT
from app.dashboard.base import dashboard_bp
from app.extensions import db, limiter
from app.log import LOG
from app.models import (
    Alias,
    AliasGeneratorEnum,
    User,
    EmailLog,
)


@dataclass
class Stats:
    nb_alias: int
    nb_forward: int
    nb_reply: int
    nb_block: int


def get_stats(user: User) -> Stats:
    nb_alias = Alias.query.filter_by(user_id=user.id).count()
    nb_forward = (
        db.session.query(EmailLog)
        .filter_by(user_id=user.id, is_reply=False, blocked=False, bounced=False)
        .count()
    )
    nb_reply = (
        db.session.query(EmailLog)
        .filter_by(user_id=user.id, is_reply=True, blocked=False, bounced=False)
        .count()
    )
    nb_block = (
        db.session.query(EmailLog)
        .filter_by(user_id=user.id, is_reply=False, blocked=True, bounced=False)
        .count()
    )

    return Stats(
        nb_alias=nb_alias, nb_forward=nb_forward, nb_reply=nb_reply, nb_block=nb_block
    )


@dashboard_bp.route("/", methods=["GET", "POST"])
@limiter.limit(
    ALIAS_LIMIT,
    methods=["POST"],
    exempt_when=lambda: request.form.get("form-name") != "create-random-email",
)
@login_required
def index():
    query = request.args.get("query") or ""
    sort = request.args.get("sort") or ""
    alias_filter = request.args.get("filter") or ""

    page = int(request.args.get("page")) if request.args.get("page") else 0
    highlight_alias_id = None
    if request.args.get("highlight_alias_id"):
        try:
            highlight_alias_id = int(request.args.get("highlight_alias_id"))
        except ValueError:
            LOG.w(
                "highlight_alias_id must be a number, received %s",
                request.args.get("highlight_alias_id"),
            )

    if request.method == "POST":
        if request.form.get("form-name") == "create-custom-email":
            if current_user.can_create_new_alias():
                return redirect(url_for("dashboard.custom_alias"))
            else:
                flash("You need to upgrade your plan to create new alias.", "warning")

        elif request.form.get("form-name") == "create-random-email":
            if current_user.can_create_new_alias():
                scheme = int(
                    request.form.get("generator_scheme") or current_user.alias_generator
                )
                if not scheme or not AliasGeneratorEnum.has_value(scheme):
                    scheme = current_user.alias_generator
                alias = Alias.create_new_random(user=current_user, scheme=scheme)

                alias.mailbox_id = current_user.default_mailbox_id

                db.session.commit()

                LOG.d("create new random alias %s for user %s", alias, current_user)
                flash(f"Alias {alias.email} has been created", "success")

                return redirect(
                    url_for(
                        "dashboard.index",
                        highlight_alias_id=alias.id,
                        query=query,
                        sort=sort,
                        filter=alias_filter,
                    )
                )
            else:
                flash("You need to upgrade your plan to create new alias.", "warning")

        elif request.form.get("form-name") in ("delete-alias", "disable-alias"):
            alias_id = request.form.get("alias-id")
            alias: Alias = Alias.get(alias_id)
            if not alias or alias.user_id != current_user.id:
                flash("Unknown error, sorry for the inconvenience", "error")
                return redirect(
                    url_for(
                        "dashboard.index",
                        query=query,
                        sort=sort,
                        filter=alias_filter,
                    )
                )

            if request.form.get("form-name") == "delete-alias":
                LOG.d("delete alias %s", alias)
                email = alias.email
                alias_utils.delete_alias(alias, current_user)
                flash(f"Alias {email} has been deleted", "success")
            elif request.form.get("form-name") == "disable-alias":
                alias.enabled = False
                db.session.commit()
                flash(f"Alias {alias.email} has been disabled", "success")

        return redirect(
            url_for("dashboard.index", query=query, sort=sort, filter=alias_filter)
        )

    mailboxes = current_user.mailboxes()

    show_intro = False
    if not current_user.intro_shown:
        LOG.d("Show intro to %s", current_user)
        show_intro = True

        # to make sure not showing intro to user again
        current_user.intro_shown = True
        db.session.commit()

    stats = get_stats(current_user)

    mailbox_id = None
    if alias_filter and alias_filter.startswith("mailbox:"):
        mailbox_id = int(alias_filter[len("mailbox:") :])

    directory_id = None
    if alias_filter and alias_filter.startswith("directory:"):
        directory_id = int(alias_filter[len("directory:") :])

    alias_infos = get_alias_infos_with_pagination_v3(
        current_user, page, query, sort, alias_filter, mailbox_id, directory_id
    )
    last_page = len(alias_infos) < PAGE_LIMIT

    # add highlighted alias in case it's not included
    if highlight_alias_id and highlight_alias_id not in [
        alias_info.alias.id for alias_info in alias_infos
    ]:
        if highlight_alias_info := get_alias_info_v3(
            current_user, alias_id=highlight_alias_id
        ):
            alias_infos.insert(0, highlight_alias_info)

    return render_template(
        "dashboard/index.html",
        alias_infos=alias_infos,
        highlight_alias_id=highlight_alias_id,
        query=query,
        AliasGeneratorEnum=AliasGeneratorEnum,
        mailboxes=mailboxes,
        show_intro=show_intro,
        page=page,
        last_page=last_page,
        sort=sort,
        filter=alias_filter,
        stats=stats,
    )
