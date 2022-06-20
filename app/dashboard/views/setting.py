from io import BytesIO

import arrow
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import StringField, validators
from wtforms.fields.html5 import EmailField

from app import s3, email_utils
from app.config import (
    URL,
    FIRST_ALIAS_DOMAIN,
    JOB_DELETE_ACCOUNT,
    ALIAS_RANDOM_SUFFIX_LENGTH,
)
from app.dashboard.base import dashboard_bp
from app.email_utils import (
    email_can_be_used_as_mailbox,
    personal_email_already_used,
)
from app.extensions import db
from app.log import LOG
from app.models import (
    PlanEnum,
    File,
    ResetPasswordCode,
    EmailChange,
    User,
    Alias,
    CustomDomain,
    AliasGeneratorEnum,
    AliasSuffixEnum,
    ManualSubscription,
    SenderFormatEnum,
    SLDomain,
    CoinbaseSubscription,
    AppleSubscription,
    Job,
    Subscription,
)
from app.utils import random_string, sanitize_email


class SettingForm(FlaskForm):
    name = StringField("Name")
    profile_picture = FileField("Profile Picture")


class ChangeEmailForm(FlaskForm):
    email = EmailField(
        "email", validators=[validators.DataRequired(), validators.Email()]
    )


class PromoCodeForm(FlaskForm):
    code = StringField("Name", validators=[validators.DataRequired()])


@dashboard_bp.route("/setting", methods=["GET", "POST"])
@login_required
def setting():
    form = SettingForm()
    promo_form = PromoCodeForm()
    change_email_form = ChangeEmailForm()

    if email_change := EmailChange.get_by(user_id=current_user.id):
        pending_email = email_change.new_email
    else:
        pending_email = None

    if request.method == "POST":
        if (
            request.form.get("form-name") == "update-email"
            and change_email_form.validate()
        ):
            # whether user can proceed with the email update
            new_email_valid = True
            if (
                sanitize_email(change_email_form.email.data) != current_user.email
                and not pending_email
            ):
                new_email = sanitize_email(change_email_form.email.data)

                # check if this email is not already used
                if personal_email_already_used(new_email) or Alias.get_by(
                    email=new_email
                ):
                    flash(f"Email {new_email} already used", "error")
                    new_email_valid = False
                elif not email_can_be_used_as_mailbox(new_email):
                    flash(
                        "You cannot use this email address as your personal inbox.",
                        "error",
                    )
                    new_email_valid = False
                # a pending email change with the same email exists from another user
                elif EmailChange.get_by(new_email=new_email):
                    other_email_change: EmailChange = EmailChange.get_by(
                        new_email=new_email
                    )
                    LOG.w(
                        "Another user has a pending %s with the same email address. Current user:%s",
                        other_email_change,
                        current_user,
                    )

                    if other_email_change.is_expired():
                        LOG.d(
                            "delete the expired email change %s", other_email_change
                        )
                        EmailChange.delete(other_email_change.id)
                        db.session.commit()
                    else:
                        flash(
                            "You cannot use this email address as your personal inbox.",
                            "error",
                        )
                        new_email_valid = False

                if new_email_valid:
                    email_change = EmailChange.create(
                        user_id=current_user.id,
                        code=random_string(
                            60
                        ),  # todo: make sure the code is unique
                        new_email=new_email,
                    )
                    db.session.commit()
                    send_change_email_confirmation(current_user, email_change)
                    flash(
                        "A confirmation email is on the way, please check your inbox",
                        "success",
                    )
                    return redirect(url_for("dashboard.setting"))
        if request.form.get("form-name") == "update-profile":
            if form.validate():
                profile_updated = False
                # update user info
                if form.name.data != current_user.name:
                    current_user.name = form.name.data
                    db.session.commit()
                    profile_updated = True

                if form.profile_picture.data:
                    file_path = random_string(30)
                    file = File.create(user_id=current_user.id, path=file_path)

                    s3.upload_from_bytesio(
                        file_path, BytesIO(form.profile_picture.data.read())
                    )

                    db.session.flush()
                    LOG.d("upload file %s to s3", file)

                    current_user.profile_picture_id = file.id
                    db.session.commit()
                    profile_updated = True

                if profile_updated:
                    flash("Your profile has been updated", "success")
                    return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-password":
            flash(
                "You are going to receive an email containing instructions to change your password",
                "success",
            )
            send_reset_password_email(current_user)
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "notification-preference":
            choose = request.form.get("notification")
            current_user.notification = choose == "on"
            db.session.commit()
            flash("Your notification preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "delete-account":
            sub: Subscription = current_user.get_subscription()
            # user who has canceled can also re-subscribe
            if sub and not sub.cancelled:
                flash("Please cancel your current subscription first", "warning")
                return redirect(url_for("dashboard.setting"))

            # Schedule delete account job
            LOG.w("schedule delete account job for %s", current_user)
            Job.create(
                name=JOB_DELETE_ACCOUNT,
                payload={"user_id": current_user.id},
                run_at=arrow.now(),
                commit=True,
            )

            flash(
                "Your account deletion has been scheduled. "
                "You'll receive an email when the deletion is finished",
                "success",
            )
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-alias-generator":
            scheme = int(request.form.get("alias-generator-scheme"))
            if AliasGeneratorEnum.has_value(scheme):
                current_user.alias_generator = scheme
                db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-random-alias-default-domain":
            if default_domain := request.form.get(
                "random-alias-default-domain"
            ):
                sl_domain: SLDomain = SLDomain.get_by(domain=default_domain)
                if sl_domain:
                    if sl_domain.premium_only and not current_user.is_premium():
                        flash("You cannot use this domain", "error")
                        return redirect(url_for("dashboard.setting"))

                    current_user.default_alias_public_domain_id = sl_domain.id
                    current_user.default_alias_custom_domain_id = None
                elif custom_domain := CustomDomain.get_by(domain=default_domain):
                        # sanity check
                    if (
                        custom_domain.user_id == current_user.id
                        and custom_domain.verified
                    ):
                        current_user.default_alias_custom_domain_id = (
                            custom_domain.id
                        )
                        current_user.default_alias_public_domain_id = None

                    else:
                        LOG.e(
                            "%s cannot use domain %s", current_user, default_domain
                        )
            else:
                current_user.default_alias_custom_domain_id = None
                current_user.default_alias_public_domain_id = None

            db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "random-alias-suffix":
            scheme = int(request.form.get("random-alias-suffix-generator"))
            if AliasSuffixEnum.has_value(scheme):
                current_user.random_alias_suffix = scheme
                db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "change-sender-format":
            sender_format = int(request.form.get("sender-format"))
            if SenderFormatEnum.has_value(sender_format):
                current_user.sender_format = sender_format
                current_user.sender_format_updated_at = arrow.now()
                db.session.commit()
                flash("Your sender format preference has been updated", "success")
            db.session.commit()
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "replace-ra":
            choose = request.form.get("replace-ra")
            current_user.replace_reverse_alias = choose == "on"
            db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "sender-in-ra":
            choose = request.form.get("enable")
            current_user.include_sender_in_reverse_alias = choose == "on"
            db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "expand-alias-info":
            choose = request.form.get("enable")
            current_user.expand_alias_info = choose == "on"
            db.session.commit()
            flash("Your preference has been updated", "success")
            return redirect(url_for("dashboard.setting"))

        elif request.form.get("form-name") == "export-data":
            return redirect(url_for("api.export_data"))
        elif request.form.get("form-name") == "export-alias":
            return redirect(url_for("api.export_aliases"))

    manual_sub = ManualSubscription.get_by(user_id=current_user.id)
    apple_sub = AppleSubscription.get_by(user_id=current_user.id)
    coinbase_sub = CoinbaseSubscription.get_by(user_id=current_user.id)

    return render_template(
        "dashboard/setting.html",
        form=form,
        PlanEnum=PlanEnum,
        SenderFormatEnum=SenderFormatEnum,
        promo_form=promo_form,
        change_email_form=change_email_form,
        pending_email=pending_email,
        AliasGeneratorEnum=AliasGeneratorEnum,
        manual_sub=manual_sub,
        apple_sub=apple_sub,
        coinbase_sub=coinbase_sub,
        FIRST_ALIAS_DOMAIN=FIRST_ALIAS_DOMAIN,
        ALIAS_RAND_SUFFIX_LENGTH=ALIAS_RANDOM_SUFFIX_LENGTH,
    )


def send_reset_password_email(user):
    """
    generate a new ResetPasswordCode and send it over email to user
    """
    # the activation code is valid for 1h
    reset_password_code = ResetPasswordCode.create(
        user_id=user.id, code=random_string(60)
    )
    db.session.commit()

    reset_password_link = f"{URL}/auth/reset_password?code={reset_password_code.code}"

    email_utils.send_reset_password_email(user.email, reset_password_link)


def send_change_email_confirmation(user: User, email_change: EmailChange):
    """
    send confirmation email to the new email address
    """

    link = f"{URL}/auth/change_email?code={email_change.code}"

    email_utils.send_change_email(email_change.new_email, user.email, link)


@dashboard_bp.route("/resend_email_change", methods=["GET", "POST"])
@login_required
def resend_email_change():
    if email_change := EmailChange.get_by(user_id=current_user.id):
        # extend email change expiration
        email_change.expired = arrow.now().shift(hours=12)
        db.session.commit()

        send_change_email_confirmation(current_user, email_change)
        flash("A confirmation email is on the way, please check your inbox", "success")
    else:
        flash(
            "You have no pending email change. Redirect back to Setting page", "warning"
        )
    return redirect(url_for("dashboard.setting"))


@dashboard_bp.route("/cancel_email_change", methods=["GET", "POST"])
@login_required
def cancel_email_change():
    if email_change := EmailChange.get_by(user_id=current_user.id):
        EmailChange.delete(email_change.id)
        db.session.commit()
        flash("Your email change is cancelled", "success")
    else:
        flash(
            "You have no pending email change. Redirect back to Setting page", "warning"
        )
    return redirect(url_for("dashboard.setting"))
