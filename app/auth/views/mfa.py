import pyotp
from flask import (
    render_template,
    redirect,
    url_for,
    flash,
    session,
    make_response,
    request,
    g,
)
from flask_login import login_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, validators

from app.auth.base import auth_bp
from app.config import MFA_USER_ID, URL
from app.extensions import db, limiter
from app.models import User, MfaBrowser


class OtpTokenForm(FlaskForm):
    token = StringField("Token", validators=[validators.DataRequired()])
    remember = BooleanField(
        "attr", default=False, description="Remember this browser for 30 days"
    )


@auth_bp.route("/mfa", methods=["GET", "POST"])
@limiter.limit(
    "10/minute", deduct_when=lambda r: hasattr(g, "deduct_limit") and g.deduct_limit
)
def mfa():
    # passed from login page
    user_id = session.get(MFA_USER_ID)

    # user access this page directly without passing by login page
    if not user_id:
        flash("Unknown error, redirect back to main page", "warning")
        return redirect(url_for("auth.login"))

    user = User.get(user_id)

    if not (user and user.enable_otp):
        flash("Only user with MFA enabled should go to this page", "warning")
        return redirect(url_for("auth.login"))

    otp_token_form = OtpTokenForm()
    next_url = request.args.get("next")

    if request.cookies.get("mfa"):
        browser = MfaBrowser.get_by(token=request.cookies.get("mfa"))
        if browser and not browser.is_expired() and browser.user_id == user.id:
            login_user(user)
            flash("Welcome back!", "success")
            # Redirect user to correct page
            return redirect(next_url or url_for("dashboard.index"))
        else:
            # Trigger rate limiter
            g.deduct_limit = True

    if otp_token_form.validate_on_submit():
        totp = pyotp.TOTP(user.otp_secret)

        token = otp_token_form.token.data.replace(" ", "")

        if totp.verify(token) and user.last_otp != token:
            del session[MFA_USER_ID]
            user.last_otp = token
            db.session.commit()

            login_user(user)
            flash("Welcome back!", "success")

            # Redirect user to correct page
            response = make_response(redirect(next_url or url_for("dashboard.index")))

            if otp_token_form.remember.data:
                browser = MfaBrowser.create_new(user=user)
                db.session.commit()
                response.set_cookie(
                    "mfa",
                    value=browser.token,
                    expires=browser.expires.datetime,
                    secure=bool(URL.startswith("https")),
                    httponly=True,
                    samesite="Lax",
                )


            return response

        else:
            flash("Incorrect token", "warning")
            # Trigger rate limiter
            g.deduct_limit = True
            otp_token_form.token.data = None

    return render_template(
        "auth/mfa.html",
        otp_token_form=otp_token_form,
        enable_fido=(user.fido_enabled()),
        next_url=next_url,
    )
