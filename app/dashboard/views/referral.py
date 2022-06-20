import re2 as re

from flask import render_template, request, flash, redirect, url_for
from flask_login import login_required, current_user

from app.dashboard.base import dashboard_bp
from app.extensions import db
from app.models import Referral, Payout

_REFERRAL_PATTERN = r"[0-9a-z-_]{3,}"


@dashboard_bp.route("/referral", methods=["GET", "POST"])
@login_required
def referral_route():
    if request.form.get("form-name") == "create":
        if request.method == "POST":
            code = request.form.get("code")
            if re.fullmatch(_REFERRAL_PATTERN, code) is None:
                flash(
                    "At least 3 characters. Only lowercase letters, "
                    "numbers, dashes (-) and underscores (_) are currently supported.",
                    "error",
                )
                return redirect(url_for("dashboard.referral_route"))

            if Referral.get_by(code=code):
                flash("Code already used", "error")
                return redirect(url_for("dashboard.referral_route"))

            name = request.form.get("name")
            referral = Referral.create(user_id=current_user.id, code=code, name=name)
            db.session.commit()
            flash("A new referral code has been created", "success")
            return redirect(
                url_for("dashboard.referral_route", highlight_id=referral.id)
            )
    elif request.form.get("form-name") == "update":
        if request.method == "POST":
            referral_id = request.form.get("referral-id")
            referral = Referral.get(referral_id)
            if referral and referral.user_id == current_user.id:
                referral.name = request.form.get("name")
                db.session.commit()
                flash("Referral name updated", "success")
                return redirect(
                    url_for("dashboard.referral_route", highlight_id=referral.id)
                )
    elif request.form.get("form-name") == "delete":
        if request.method == "POST":
            referral_id = request.form.get("referral-id")
            referral = Referral.get(referral_id)
            if referral and referral.user_id == current_user.id:
                Referral.delete(referral.id)
                db.session.commit()
                flash("Referral deleted", "success")
                return redirect(url_for("dashboard.referral_route"))

    # Highlight a referral
    highlight_id = request.args.get("highlight_id")
    if highlight_id:
        highlight_id = int(highlight_id)

    referrals = Referral.query.filter_by(user_id=current_user.id).all()
    if highlight_index := next(
        (
            ix
            for ix, referral in enumerate(referrals)
            if referral.id == highlight_id
        ),
        None,
    ):
        referrals.insert(0, referrals.pop(highlight_index))

    payouts = Payout.query.filter_by(user_id=current_user.id).all()

    return render_template("dashboard/referral.html", **locals())
