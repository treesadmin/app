import enum
import random
import uuid
from email.utils import formataddr
from typing import List, Tuple, Optional

import arrow
import sqlalchemy as sa
from arrow import Arrow
from flanker.addresslib import address
from flask import url_for
from flask_login import UserMixin
from sqlalchemy import text, desc, CheckConstraint, Index, Column
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import deferred
from sqlalchemy_utils import ArrowType

from app import s3
from app.config import (
    MAX_NB_EMAIL_FREE_PLAN,
    URL,
    AVATAR_URL_EXPIRATION,
    JOB_ONBOARDING_1,
    JOB_ONBOARDING_2,
    JOB_ONBOARDING_4,
    LANDING_PAGE_URL,
    FIRST_ALIAS_DOMAIN,
    DISABLE_ONBOARDING,
    UNSUBSCRIBER,
    ALIAS_RANDOM_SUFFIX_LENGTH,
)
from app.errors import AliasInTrashError
from app.extensions import db
from app.log import LOG
from app.oauth_models import Scope
from app.pw_models import PasswordOracle
from app.utils import (
    convert_to_id,
    random_string,
    random_words,
    sanitize_email,
    random_word,
)


class TSVector(sa.types.TypeDecorator):
    impl = TSVECTOR


class ModelMixin(object):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(ArrowType, default=arrow.utcnow, nullable=False)
    updated_at = db.Column(ArrowType, default=None, onupdate=arrow.utcnow)

    _repr_hide = ["created_at", "updated_at"]

    @classmethod
    def query(cls):
        return db.session.query(cls)

    @classmethod
    def get(cls, id):
        return cls.query.get(id)

    @classmethod
    def get_by(cls, **kw):
        return cls.query.filter_by(**kw).first()

    @classmethod
    def filter_by(cls, **kw):
        return cls.query.filter_by(**kw)

    @classmethod
    def get_or_create(cls, **kw):
        r = cls.get_by(**kw)
        if not r:
            r = cls(**kw)
            db.session.add(r)

        return r

    @classmethod
    def create(cls, **kw):
        # whether should call db.session.commit
        commit = kw.pop("commit", False)
        flush = kw.pop("flush", False)

        r = cls(**kw)
        db.session.add(r)

        if commit:
            db.session.commit()

        if flush:
            db.session.flush()

        return r

    def save(self):
        db.session.add(self)

    @classmethod
    def delete(cls, obj_id):
        cls.query.filter(cls.id == obj_id).delete()

    @classmethod
    def first(cls):
        return cls.query.first()

    def __repr__(self):
        values = ", ".join(
            "%s=%r" % (n, getattr(self, n))
            for n in self.__table__.c.keys()
            if n not in self._repr_hide
        )
        return f"{self.__class__.__name__}({values})"


class File(db.Model, ModelMixin):
    path = db.Column(db.String(128), unique=True, nullable=False)
    user_id = db.Column(db.ForeignKey("users.id", ondelete="cascade"), nullable=True)

    def get_url(self, expires_in=3600):
        return s3.get_url(self.path, expires_in)

    def __repr__(self):
        return f"<File {self.path}>"


class EnumE(enum.Enum):
    @classmethod
    def has_value(cls, value: int) -> bool:
        return value in {item.value for item in cls}

    @classmethod
    def get_name(cls, value: int) -> Optional[str]:
        return next((item.name for item in cls if item.value == value), None)

    @classmethod
    def has_name(cls, name: str) -> bool:
        return any(item.name == name for item in cls)

    @classmethod
    def get_value(cls, name: str) -> Optional[int]:
        return next((item.value for item in cls if item.name == name), None)


class PlanEnum(EnumE):
    monthly = 2
    yearly = 3


# Specify the format for sender address
class SenderFormatEnum(EnumE):
    AT = 0  # John Wick - john at wick.com
    A = 2  # John Wick - john(a)wick.com


class AliasGeneratorEnum(EnumE):
    word = 1  # aliases are generated based on random words
    uuid = 2  # aliases are generated based on uuid


class AliasSuffixEnum(EnumE):
    word = 0  # Random word from dictionary file
    random_string = 1  # Completely random string


class Hibp(db.Model, ModelMixin):
    __tablename__ = "hibp"
    name = db.Column(db.String(), nullable=False, unique=True, index=True)
    breached_aliases = db.relationship("Alias", secondary="alias_hibp")

    description = db.Column(db.Text)
    date = db.Column(ArrowType, nullable=True)

    def __repr__(self):
        return f"<HIBP Breach {self.id} {self.name}>"


class HibpNotifiedAlias(db.Model, ModelMixin):
    """Contain list of aliases that have been notified to users
    So that we can only notify users of new aliases.
    """

    __tablename__ = "hibp_notified_alias"
    alias_id = db.Column(db.ForeignKey("alias.id", ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey("users.id", ondelete="cascade"), nullable=False)

    notified_at = db.Column(ArrowType, default=arrow.utcnow, nullable=False)


class Fido(db.Model, ModelMixin):
    __tablename__ = "fido"
    credential_id = db.Column(db.String(), nullable=False, unique=True, index=True)
    uuid = db.Column(
        db.ForeignKey("users.fido_uuid", ondelete="cascade"),
        unique=False,
        nullable=False,
    )
    public_key = db.Column(db.String(), nullable=False, unique=True)
    sign_count = db.Column(db.Integer(), nullable=False)
    name = db.Column(db.String(128), nullable=False, unique=False)


class User(db.Model, ModelMixin, UserMixin, PasswordOracle):
    __tablename__ = "users"
    email = db.Column(db.String(256), unique=True, nullable=False)

    name = db.Column(db.String(128), nullable=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    alias_generator = db.Column(
        db.Integer,
        nullable=False,
        default=AliasGeneratorEnum.word.value,
        server_default=str(AliasGeneratorEnum.word.value),
    )
    notification = db.Column(
        db.Boolean, default=True, nullable=False, server_default="1"
    )

    activated = db.Column(db.Boolean, default=False, nullable=False)

    # an account can be disabled if having harmful behavior
    disabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    profile_picture_id = db.Column(db.ForeignKey(File.id), nullable=True)

    otp_secret = db.Column(db.String(16), nullable=True)
    enable_otp = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    last_otp = db.Column(db.String(12), nullable=True, default=False)

    # Fields for WebAuthn
    fido_uuid = db.Column(db.String(), nullable=True, unique=True)

    # the default domain that's used when user creates a new random alias
    # default_alias_custom_domain_id XOR default_alias_public_domain_id
    default_alias_custom_domain_id = db.Column(
        db.ForeignKey("custom_domain.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    default_alias_public_domain_id = db.Column(
        db.ForeignKey("public_domain.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    # some users could have lifetime premium
    lifetime = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    paid_lifetime = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    # user can use all premium features until this date
    trial_end = db.Column(
        ArrowType, default=lambda: arrow.now().shift(days=7, hours=1), nullable=True
    )

    # the mailbox used when create random alias
    # this field is nullable but in practice, it's always set
    # it cannot be set to non-nullable though
    # as this will create foreign key cycle between User and Mailbox
    default_mailbox_id = db.Column(
        db.ForeignKey("mailbox.id"), nullable=True, default=None
    )

    profile_picture = db.relationship(File, foreign_keys=[profile_picture_id])

    # Specify the format for sender address
    # John Wick - john at wick.com  -> 0
    # john@wick.com via SimpleLogin -> 1
    # John Wick - john(a)wick.com     -> 2
    # John Wick - john@wick.com     -> 3
    sender_format = db.Column(
        db.Integer, default="0", nullable=False, server_default="0"
    )
    # to know whether user has explicitly chosen a sender format as opposed to those who use the default ones.
    # users who haven't chosen a sender format and are using 1 or 3 format, their sender format will be set to 0
    sender_format_updated_at = db.Column(ArrowType, default=None)

    replace_reverse_alias = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    referral_id = db.Column(
        db.ForeignKey("referral.id", ondelete="SET NULL"), nullable=True, default=None
    )

    referral = db.relationship("Referral", foreign_keys=[referral_id])

    # whether intro has been shown to user
    intro_shown = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    default_mailbox = db.relationship("Mailbox", foreign_keys=[default_mailbox_id])

    # user can set a more strict max_spam score to block spams more aggressively
    max_spam_score = db.Column(db.Integer, nullable=True)

    # newsletter is sent to this address
    newsletter_alias_id = db.Column(
        db.ForeignKey("alias.id", ondelete="SET NULL"), nullable=True, default=None
    )

    # whether to include the sender address in reverse-alias
    include_sender_in_reverse_alias = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    # whether to use random string or random word as suffix
    # Random word from dictionary file -> 0
    # Completely random string -> 1
    random_alias_suffix = db.Column(
        db.Integer,
        nullable=False,
        default=AliasSuffixEnum.random_string.value,
        server_default=str(AliasSuffixEnum.random_string.value),
    )

    # always expand the alias info, i.e. without needing to press "More"
    expand_alias_info = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    # ignore emails send from a mailbox to its alias. This can happen when replying all to a forwarded email
    # can automatically re-includes the alias
    ignore_loop_email = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    @classmethod
    def create(cls, email, name="", password=None, **kwargs):
        user: User = super(User, cls).create(email=email, name=name, **kwargs)

        if password:
            user.set_password(password)

        db.session.flush()

        mb = Mailbox.create(user_id=user.id, email=user.email, verified=True)
        db.session.flush()
        user.default_mailbox_id = mb.id

        # create a first alias mail to show user how to use when they login
        alias = Alias.create_new(
            user,
            prefix="simplelogin-newsletter",
            mailbox_id=mb.id,
            note="This is your first alias. It's used to receive SimpleLogin communications "
            "like new features announcements, newsletters.",
        )
        db.session.flush()

        user.newsletter_alias_id = alias.id
        db.session.flush()

        if DISABLE_ONBOARDING:
            LOG.d("Disable onboarding emails")
            return user

        # Schedule onboarding emails
        Job.create(
            name=JOB_ONBOARDING_1,
            payload={"user_id": user.id},
            run_at=arrow.now().shift(days=1),
        )
        Job.create(
            name=JOB_ONBOARDING_2,
            payload={"user_id": user.id},
            run_at=arrow.now().shift(days=2),
        )
        Job.create(
            name=JOB_ONBOARDING_4,
            payload={"user_id": user.id},
            run_at=arrow.now().shift(days=3),
        )
        db.session.flush()

        return user

    def lifetime_or_active_subscription(self) -> bool:
        """True if user has lifetime licence or active subscription"""
        if self.lifetime:
            return True

        sub: Subscription = self.get_subscription()
        if sub:
            return True

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and apple_sub.is_valid():
            return True

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        if manual_sub and manual_sub.is_active():
            return True

        coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
            user_id=self.id
        )
        if coinbase_subscription and coinbase_subscription.is_active():
            return True

        return False

    def is_paid(self) -> bool:
        """same as _lifetime_or_active_subscription but not include free manual subscription"""
        sub: Subscription = self.get_subscription()
        if sub:
            return True

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and apple_sub.is_valid():
            return True

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        if manual_sub and not manual_sub.is_giveaway and manual_sub.is_active():
            return True

        coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
            user_id=self.id
        )
        if coinbase_subscription and coinbase_subscription.is_active():
            return True

        return False

    def in_trial(self):
        """return True if user does not have lifetime licence or an active subscription AND is in trial period"""
        if self.lifetime_or_active_subscription():
            return False

        if self.trial_end and arrow.now() < self.trial_end:
            return True

        return False

    def should_show_upgrade_button(self):
        if self.lifetime_or_active_subscription():
            # user who has canceled can also re-subscribe
            sub: Subscription = self.get_subscription()
            if sub and sub.cancelled:
                return True

            return False

        return True

    def can_upgrade(self) -> bool:
        """
        The following users can upgrade:
        - have giveaway lifetime licence
        - have giveaway manual subscriptions
        - have a cancelled Paddle subscription
        - have a expired Apple subscription
        - have a expired Coinbase subscription
        """
        sub: Subscription = self.get_subscription()
        # user who has canceled can also re-subscribe
        if sub and not sub.cancelled:
            return False

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and apple_sub.is_valid():
            return False

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        # user who has giveaway premium can decide to upgrade
        if manual_sub and manual_sub.is_active() and not manual_sub.is_giveaway:
            return False

        coinbase_subscription = CoinbaseSubscription.get_by(user_id=self.id)
        if coinbase_subscription and coinbase_subscription.is_active():
            return False

        return True

    def is_premium(self) -> bool:
        """
        user is premium if they:
        - have a lifetime deal or
        - in trial period or
        - active subscription
        """
        if self.lifetime_or_active_subscription():
            return True

        if self.trial_end and arrow.now() < self.trial_end:
            return True

        return False

    @property
    def upgrade_channel(self) -> str:
        if self.lifetime:
            return "Lifetime"

        sub: Subscription = self.get_subscription()
        if sub:
            if sub.cancelled:
                return f"Cancelled Paddle Subscription {sub.subscription_id} {sub.plan_name()}"
            else:
                return f"Active Paddle Subscription {sub.subscription_id} {sub.plan_name()}"

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and apple_sub.is_valid():
            return "Apple Subscription"

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        if manual_sub and manual_sub.is_active():
            mode = "Giveaway" if manual_sub.is_giveaway else "Paid"
            return f"Manual Subscription {manual_sub.comment} {mode}"

        coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
            user_id=self.id
        )
        if coinbase_subscription and coinbase_subscription.is_active():
            return "Coinbase Subscription"

        if self.trial_end and arrow.now() < self.trial_end:
            return "In Trial"

        return "N/A"

    @property
    def subscription_cancelled(self) -> bool:
        sub: Subscription = self.get_subscription()
        if sub and sub.cancelled:
            return True

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and not apple_sub.is_valid():
            return True

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        if manual_sub and not manual_sub.is_active():
            return True

        coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
            user_id=self.id
        )
        if coinbase_subscription and not coinbase_subscription.is_active():
            return True

        return False

    @property
    def premium_end(self) -> str:
        if self.lifetime:
            return "Forever"

        sub: Subscription = self.get_subscription()
        if sub:
            return str(sub.next_bill_date)

        apple_sub: AppleSubscription = AppleSubscription.get_by(user_id=self.id)
        if apple_sub and apple_sub.is_valid():
            return apple_sub.expires_date.humanize()

        manual_sub: ManualSubscription = ManualSubscription.get_by(user_id=self.id)
        if manual_sub and manual_sub.is_active():
            return manual_sub.end_at.humanize()

        coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
            user_id=self.id
        )
        if coinbase_subscription and coinbase_subscription.is_active():
            return coinbase_subscription.end_at.humanize()

        return "N/A"

    def can_create_new_alias(self) -> bool:
        """
        Whether user can create a new alias. User can't create a new alias if
        - has more than 15 aliases in the free plan, *even in the free trial*
        """
        if self.lifetime_or_active_subscription():
            return True
        else:
            return Alias.filter_by(user_id=self.id).count() < MAX_NB_EMAIL_FREE_PLAN

    def profile_picture_url(self):
        if self.profile_picture_id:
            return self.profile_picture.get_url()
        else:
            return url_for("static", filename="default-avatar.png")

    def suggested_emails(self, website_name) -> (str, [str]):
        """return suggested email and other email choices """
        website_name = convert_to_id(website_name)

        all_aliases = [
            ge.email for ge in Alias.filter_by(user_id=self.id, enabled=True)
        ]
        if self.can_create_new_alias():
            suggested_alias = Alias.create_new(self, prefix=website_name).email
        else:
            # pick an email from the list of gen emails
            suggested_alias = random.choice(all_aliases)

        return (
            suggested_alias,
            list(set(all_aliases).difference({suggested_alias})),
        )

    def suggested_names(self) -> (str, [str]):
        """return suggested name and other name choices """
        other_name = convert_to_id(self.name)

        return self.name, [other_name, "Anonymous", "whoami"]

    def get_name_initial(self) -> str:
        if not self.name:
            return ""
        names = self.name.split(" ")
        return "".join([n[0].upper() for n in names if n])

    def get_subscription(self) -> Optional["Subscription"]:
        """return *active* Paddle subscription
        Return None if the subscription is already expired
        TODO: support user unsubscribe and re-subscribe
        """
        if sub := Subscription.get_by(user_id=self.id):
            # sub is active until the next billing_date + 1
            return sub if sub.next_bill_date >= arrow.now().shift(days=-1).date() else None
        else:
            return sub

    def verified_custom_domains(self) -> List["CustomDomain"]:
        return CustomDomain.query.filter_by(user_id=self.id, verified=True).all()

    def mailboxes(self) -> List["Mailbox"]:
        """list of mailbox that user own"""
        return list(Mailbox.query.filter_by(user_id=self.id, verified=True))

    def nb_directory(self):
        return Directory.query.filter_by(user_id=self.id).count()

    def has_custom_domain(self):
        return CustomDomain.filter_by(user_id=self.id, verified=True).count() > 0

    def custom_domains(self):
        return CustomDomain.filter_by(user_id=self.id, verified=True).all()

    def available_domains_for_random_alias(self) -> List[Tuple[bool, str]]:
        """Return available domains for user to create random aliases
        Each result record contains:
        - whether the domain belongs to SimpleLogin
        - the domain
        """
        res = [(True, domain) for domain in self.available_sl_domains()]
        res.extend(
            (False, custom_domain.domain)
            for custom_domain in self.verified_custom_domains()
        )

        return res

    def default_random_alias_domain(self) -> str:
        """return the domain used for the random alias"""
        if self.default_alias_custom_domain_id:
            custom_domain = CustomDomain.get(self.default_alias_custom_domain_id)
            # sanity check
            if (
                not custom_domain
                or not custom_domain.verified
                or custom_domain.user_id != self.id
            ):
                LOG.w("Problem with %s default random alias domain", self)
                return FIRST_ALIAS_DOMAIN

            return custom_domain.domain

        if self.default_alias_public_domain_id:
            sl_domain = SLDomain.get(self.default_alias_public_domain_id)
            # sanity check
            if not sl_domain:
                LOG.e("Problem with %s public random alias domain", self)
                return FIRST_ALIAS_DOMAIN

            if sl_domain.premium_only and not self.is_premium():
                LOG.w(
                    "%s is not premium and cannot use %s. Reset default random alias domain setting",
                    self,
                    sl_domain,
                )
                self.default_alias_custom_domain_id = None
                self.default_alias_public_domain_id = None
                db.session.commit()
                return FIRST_ALIAS_DOMAIN

            return sl_domain.domain

        return FIRST_ALIAS_DOMAIN

    def fido_enabled(self) -> bool:
        return self.fido_uuid is not None

    def two_factor_authentication_enabled(self) -> bool:
        return self.enable_otp or self.fido_enabled()

    def get_communication_email(self) -> (Optional[str], str, bool):
        """
        Return
        - the email that user uses to receive email communication. None if user unsubscribes from newsletter
        - the unsubscribe URL
        - whether the unsubscribe method is via sending email (mailto:) or Http POST
        """
        if self.notification and self.activated:
            if self.newsletter_alias_id:
                alias = Alias.get(self.newsletter_alias_id)
                if alias.enabled:
                    unsubscribe_link, via_email = alias.unsubscribe_link()
                    return alias.email, unsubscribe_link, via_email
                # alias disabled -> user doesn't want to receive newsletter
                else:
                    return None, None, False
            elif UNSUBSCRIBER:
                # use * as suffix instead of = as for alias unsubscribe
                return self.email, f"mailto:{UNSUBSCRIBER}?subject={self.id}*", True

        return None, None, False

    def available_sl_domains(self) -> [str]:
        """
        Return all SimpleLogin domains that user can use when creating a new alias, including:
        - SimpleLogin public domains, available for all users (ALIAS_DOMAIN)
        - SimpleLogin premium domains, only available for Premium accounts (PREMIUM_ALIAS_DOMAIN)
        """
        return [sl_domain.domain for sl_domain in self.get_sl_domains()]

    def get_sl_domains(self) -> List["SLDomain"]:
        if self.is_premium():
            query = SLDomain.query
        else:
            query = SLDomain.filter_by(premium_only=False)

        return query.all()

    def available_alias_domains(self) -> [str]:
        """return all domains that user can use when creating a new alias, including:
        - SimpleLogin public domains, available for all users (ALIAS_DOMAIN)
        - SimpleLogin premium domains, only available for Premium accounts (PREMIUM_ALIAS_DOMAIN)
        - Verified custom domains

        """
        domains = self.available_sl_domains()

        for custom_domain in self.verified_custom_domains():
            domains.append(custom_domain.domain)

        # can have duplicate where a "root" user has a domain that's also listed in SL domains
        return list(set(domains))

    def should_show_app_page(self) -> bool:
        """whether to show the app page"""
        return (
            # when user has used the "Sign in with SL" button before
            ClientUser.query.filter(ClientUser.user_id == self.id).count()
            # or when user has created an app
            + Client.query.filter(Client.user_id == self.id).count()
            > 0
        )

    def get_random_alias_suffix(self):
        """Get random suffix for an alias based on user's preference.


        Returns:
            str: the random suffix generated
        """
        if self.random_alias_suffix == AliasSuffixEnum.random_string.value:
            return random_string(ALIAS_RANDOM_SUFFIX_LENGTH, include_digits=True)
        return random_word()

    def __repr__(self):
        return f"<User {self.id} {self.name} {self.email}>"


def _expiration_1h():
    return arrow.now().shift(hours=1)


def _expiration_12h():
    return arrow.now().shift(hours=12)


def _expiration_5m():
    return arrow.now().shift(minutes=5)


def _expiration_7d():
    return arrow.now().shift(days=7)


class ActivationCode(db.Model, ModelMixin):
    """For activate user account"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)

    user = db.relationship(User)

    expired = db.Column(ArrowType, nullable=False, default=_expiration_1h)

    def is_expired(self):
        return self.expired < arrow.now()


class ResetPasswordCode(db.Model, ModelMixin):
    """For resetting password"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)

    user = db.relationship(User)

    expired = db.Column(ArrowType, nullable=False, default=_expiration_1h)

    def is_expired(self):
        return self.expired < arrow.now()


class SocialAuth(db.Model, ModelMixin):
    """Store how user authenticates with social login"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    # name of the social login used, could be facebook, google or github
    social = db.Column(db.String(128), nullable=False)

    __table_args__ = (db.UniqueConstraint("user_id", "social", name="uq_social_auth"),)


# <<< OAUTH models >>>


def generate_oauth_client_id(client_name) -> str:
    oauth_client_id = f"{convert_to_id(client_name)}-{random_string()}"

    # check that the client does not exist yet
    if not Client.get_by(oauth_client_id=oauth_client_id):
        LOG.d("generate oauth_client_id %s", oauth_client_id)
        return oauth_client_id

    # Rerun the function
    LOG.w("client_id %s already exists, generate a new client_id", oauth_client_id)
    return generate_oauth_client_id(client_name)


class MfaBrowser(db.Model, ModelMixin):
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    token = db.Column(db.String(64), default=False, unique=True, nullable=False)
    expires = db.Column(ArrowType, default=False, nullable=False)

    user = db.relationship(User)

    @classmethod
    def create_new(cls, user, token_length=64) -> "MfaBrowser":
        found = False
        while not found:
            token = random_string(token_length)

            if not cls.get_by(token=token):
                found = True

        return MfaBrowser.create(
            user_id=user.id,
            token=token,
            expires=arrow.now().shift(days=30),
        )

    @classmethod
    def delete(cls, token):
        cls.query.filter(cls.token == token).delete()
        db.session.commit()

    @classmethod
    def delete_expired(cls):
        cls.query.filter(cls.expires < arrow.now()).delete()
        db.session.commit()

    def is_expired(self):
        return self.expires < arrow.now()

    def reset_expire(self):
        self.expires = arrow.now().shift(days=30)


class Client(db.Model, ModelMixin):
    oauth_client_id = db.Column(db.String(128), unique=True, nullable=False)
    oauth_client_secret = db.Column(db.String(128), nullable=False)

    name = db.Column(db.String(128), nullable=False)
    home_url = db.Column(db.String(1024))

    # user who created this client
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    icon_id = db.Column(db.ForeignKey(File.id), nullable=True)

    # an app needs to be approved by SimpleLogin team
    approved = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    description = db.Column(db.Text, nullable=True)

    icon = db.relationship(File)
    user = db.relationship(User)

    def nb_user(self):
        return ClientUser.filter_by(client_id=self.id).count()

    def get_scopes(self) -> [Scope]:
        # todo: client can choose which scopes they want to have access
        return [Scope.NAME, Scope.EMAIL, Scope.AVATAR_URL]

    @classmethod
    def create_new(cls, name, user_id) -> "Client":
        # generate a client-id
        oauth_client_id = generate_oauth_client_id(name)
        oauth_client_secret = random_string(40)
        return Client.create(
            name=name,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            user_id=user_id,
        )

    def get_icon_url(self):
        if self.icon_id:
            return self.icon.get_url()
        else:
            return f"{URL}/static/default-icon.svg"

    def last_user_login(self) -> "ClientUser":
        if client_user := (
            ClientUser.query.filter(ClientUser.client_id == self.id)
            .order_by(ClientUser.updated_at)
            .first()
        ):
            return client_user
        return None


class RedirectUri(db.Model, ModelMixin):
    """Valid redirect uris for a client"""

    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    uri = db.Column(db.String(1024), nullable=False)

    client = db.relationship(Client, backref="redirect_uris")


class AuthorizationCode(db.Model, ModelMixin):
    code = db.Column(db.String(128), unique=True, nullable=False)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    scope = db.Column(db.String(128))
    redirect_uri = db.Column(db.String(1024))

    # what is the input response_type, e.g. "code", "code,id_token", ...
    response_type = db.Column(db.String(128))

    nonce = db.Column(db.Text, nullable=True, default=None, server_default=text("NULL"))

    user = db.relationship(User, lazy=False)
    client = db.relationship(Client, lazy=False)

    expired = db.Column(ArrowType, nullable=False, default=_expiration_5m)

    def is_expired(self):
        return self.expired < arrow.now()


class OauthToken(db.Model, ModelMixin):
    access_token = db.Column(db.String(128), unique=True)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    scope = db.Column(db.String(128))
    redirect_uri = db.Column(db.String(1024))

    # what is the input response_type, e.g. "token", "token,id_token", ...
    response_type = db.Column(db.String(128))

    user = db.relationship(User)
    client = db.relationship(Client)

    expired = db.Column(ArrowType, nullable=False, default=_expiration_1h)

    def is_expired(self):
        return self.expired < arrow.now()


def generate_email(
    scheme: int = AliasGeneratorEnum.word.value,
    in_hex: bool = False,
    alias_domain=FIRST_ALIAS_DOMAIN,
) -> str:
    """generate an email address that does not exist before
    :param alias_domain: the domain used to generate the alias.
    :param scheme: int, value of AliasGeneratorEnum, indicate how the email is generated
    :type in_hex: bool, if the generate scheme is uuid, is hex favorable?
    """
    if scheme == AliasGeneratorEnum.uuid.value:
        name = uuid.uuid4().hex if in_hex else uuid.uuid4().__str__()
        random_email = f"{name}@{alias_domain}"
    else:
        random_email = f"{random_words()}@{alias_domain}"

    random_email = random_email.lower().strip()

    # check that the client does not exist yet
    if not Alias.get_by(email=random_email) and not DeletedAlias.get_by(
        email=random_email
    ):
        LOG.d("generate email %s", random_email)
        return random_email

    # Rerun the function
    LOG.w("email %s already exists, generate a new email", random_email)
    return generate_email(scheme=scheme, in_hex=in_hex)


class Alias(db.Model, ModelMixin):
    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, index=True
    )
    email = db.Column(db.String(128), unique=True, nullable=False)

    # the name to use when user replies/sends from alias
    name = db.Column(db.String(128), nullable=True, default=None)

    enabled = db.Column(db.Boolean(), default=True, nullable=False)

    custom_domain_id = db.Column(
        db.ForeignKey("custom_domain.id", ondelete="cascade"), nullable=True
    )

    custom_domain = db.relationship("CustomDomain", foreign_keys=[custom_domain_id])

    # To know whether an alias is created "on the fly", i.e. via the custom domain catch-all feature
    automatic_creation = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # to know whether an alias belongs to a directory
    directory_id = db.Column(
        db.ForeignKey("directory.id", ondelete="cascade"), nullable=True
    )

    note = db.Column(db.Text, default=None, nullable=True)

    # an alias can be owned by another mailbox
    mailbox_id = db.Column(
        db.ForeignKey("mailbox.id", ondelete="cascade"), nullable=False, index=True
    )

    # prefix _ to avoid this object being used accidentally.
    # To have the list of all mailboxes, should use AliasInfo instead
    _mailboxes = db.relationship("Mailbox", secondary="alias_mailbox", lazy="joined")

    # If the mailbox has PGP-enabled, user can choose disable the PGP on the alias
    # this is useful when some senders already support PGP
    disable_pgp = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # a way to bypass the bounce automatic disable mechanism
    cannot_be_disabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # when a mailbox wants to send an email on behalf of the alias via the reverse-alias
    # several checks are performed to avoid email spoofing
    # this option allow disabling these checks
    disable_email_spoofing_check = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # to know whether an alias is added using a batch import
    batch_import_id = db.Column(
        db.ForeignKey("batch_import.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )

    # set in case of alias transfer.
    original_owner_id = db.Column(
        db.ForeignKey(User.id, ondelete="SET NULL"), nullable=True
    )

    # alias is pinned on top
    pinned = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # used to transfer an alias to another user
    transfer_token = db.Column(db.String(64), default=None, unique=True, nullable=True)

    # have I been pwned
    hibp_last_check = db.Column(ArrowType, default=None)
    hibp_breaches = db.relationship("Hibp", secondary="alias_hibp")

    # to use Postgres full text search. Only applied on "note" column for now
    # this is a generated Postgres column
    ts_vector = db.Column(
        TSVector(), db.Computed("to_tsvector('english', note)", persisted=True)
    )

    __table_args__ = (
        Index("ix_video___ts_vector__", ts_vector, postgresql_using="gin"),
        # index on note column using pg_trgm
        Index(
            "note_pg_trgm_index",
            "note",
            postgresql_ops={"note": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
    )

    user = db.relationship(User, foreign_keys=[user_id])
    mailbox = db.relationship("Mailbox", lazy="joined")

    @property
    def mailboxes(self):
        ret = [self.mailbox]
        ret.extend(iter(self._mailboxes))
        ret = [mb for mb in ret if mb.verified]
        ret = sorted(ret, key=lambda mb: mb.email)

        return ret

    def mailbox_support_pgp(self) -> bool:
        """return True of one of the mailboxes support PGP"""
        return any(mb.pgp_enabled() for mb in self.mailboxes)

    def pgp_enabled(self) -> bool:
        return bool(self.mailbox_support_pgp() and not self.disable_pgp)

    @classmethod
    def create(cls, **kw):
        # whether should call db.session.commit
        commit = kw.pop("commit", False)

        r = cls(**kw)

        email = kw["email"]
        # make sure email is lowercase and doesn't have any whitespace
        email = sanitize_email(email)

        # make sure alias is not in global trash, i.e. DeletedAlias table
        if DeletedAlias.get_by(email=email):
            raise AliasInTrashError

        if DomainDeletedAlias.get_by(email=email):
            raise AliasInTrashError

        db.session.add(r)
        if commit:
            db.session.commit()
        return r

    @classmethod
    def create_new(cls, user, prefix, note=None, mailbox_id=None):
        prefix = prefix.lower().strip().replace(" ", "")

        if not prefix:
            raise Exception("alias prefix cannot be empty")

        # find the right suffix - avoid infinite loop by running this at max 1000 times
        for _ in range(1000):
            suffix = user.get_random_alias_suffix()
            email = f"{prefix}.{suffix}@{FIRST_ALIAS_DOMAIN}"

            if not cls.get_by(email=email) and not DeletedAlias.get_by(email=email):
                break

        return Alias.create(
            user_id=user.id,
            email=email,
            note=note,
            mailbox_id=mailbox_id or user.default_mailbox_id,
        )

    @classmethod
    def delete(cls, obj_id):
        raise Exception("should use delete_alias(alias,user) instead")

    @classmethod
    def create_new_random(
        cls,
        user,
        scheme: int = AliasGeneratorEnum.word.value,
        in_hex: bool = False,
        note: str = None,
    ):
        """create a new random alias"""
        custom_domain = None

        random_email = None

        if user.default_alias_custom_domain_id:
            custom_domain = CustomDomain.get(user.default_alias_custom_domain_id)
            random_email = generate_email(
                scheme=scheme, in_hex=in_hex, alias_domain=custom_domain.domain
            )
        elif user.default_alias_public_domain_id:
            sl_domain: SLDomain = SLDomain.get(user.default_alias_public_domain_id)
            if sl_domain.premium_only and not user.is_premium():
                LOG.w("%s not premium, cannot use %s", user, sl_domain)
            else:
                random_email = generate_email(
                    scheme=scheme, in_hex=in_hex, alias_domain=sl_domain.domain
                )

        if not random_email:
            random_email = generate_email(scheme=scheme, in_hex=in_hex)

        alias = Alias.create(
            user_id=user.id,
            email=random_email,
            mailbox_id=user.default_mailbox_id,
            note=note,
        )

        if custom_domain:
            alias.custom_domain_id = custom_domain.id

        return alias

    def mailbox_email(self):
        return self.mailbox.email if self.mailbox_id else self.user.email

    def unsubscribe_link(self) -> (str, bool):
        """return the unsubscribe link along with whether this is via email (mailto:) or Http POST
        The mailto: method is preferred
        """
        if UNSUBSCRIBER:
            return f"mailto:{UNSUBSCRIBER}?subject={self.id}=", True
        else:
            return f"{URL}/dashboard/unsubscribe/{self.id}", False

    def __repr__(self):
        return f"<Alias {self.id} {self.email}>"


class ClientUser(db.Model, ModelMixin):
    __table_args__ = (
        db.UniqueConstraint("user_id", "client_id", name="uq_client_user"),
    )

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    client_id = db.Column(db.ForeignKey(Client.id, ondelete="cascade"), nullable=False)

    # Null means client has access to user original email
    alias_id = db.Column(db.ForeignKey(Alias.id, ondelete="cascade"), nullable=True)

    # user can decide to send to client another name
    name = db.Column(
        db.String(128), nullable=True, default=None, server_default=text("NULL")
    )

    # user can decide to send to client a default avatar
    default_avatar = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    alias = db.relationship(Alias, backref="client_users")

    user = db.relationship(User)
    client = db.relationship(Client)

    def get_email(self):
        return self.alias.email if self.alias_id else self.user.email

    def get_user_name(self):
        return self.name or self.user.name

    def get_user_info(self) -> dict:
        """return user info according to client scope
        Return dict with key being scope name. For now all the fields are the same for all clients:

        {
          "client": "Demo",
          "email": "test-avk5l@mail-tester.com",
          "email_verified": true,
          "id": 1,
          "name": "Son GM",
          "avatar_url": "http://s3..."
        }

        """
        res = {
            "id": self.id,
            "client": self.client.name,
            "email_verified": True,
            "sub": str(self.id),
        }

        for scope in self.client.get_scopes():
            if scope == Scope.NAME:
                res[Scope.NAME.value] = self.name or "" if self.name else self.user.name or ""
            elif scope == Scope.AVATAR_URL:
                if self.user.profile_picture_id:
                    if self.default_avatar:
                        res[Scope.AVATAR_URL.value] = f"{URL}/static/default-avatar.png"
                    else:
                        res[Scope.AVATAR_URL.value] = self.user.profile_picture.get_url(
                            AVATAR_URL_EXPIRATION
                        )
                else:
                    res[Scope.AVATAR_URL.value] = None
            elif scope == Scope.EMAIL:
                # Use generated email
                if self.alias_id:
                    LOG.d(
                        "Use gen email for user %s, client %s", self.user, self.client
                    )
                    res[Scope.EMAIL.value] = self.alias.email
                # Use user original email
                else:
                    res[Scope.EMAIL.value] = self.user.email

        return res


class Contact(db.Model, ModelMixin):
    """
    Store configuration of sender (website-email) and alias.
    """

    __table_args__ = (
        db.UniqueConstraint("alias_id", "website_email", name="uq_contact"),
    )

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, index=True
    )
    alias_id = db.Column(
        db.ForeignKey(Alias.id, ondelete="cascade"), nullable=False, index=True
    )

    name = db.Column(
        db.String(512), nullable=True, default=None, server_default=text("NULL")
    )

    website_email = db.Column(db.String(512), nullable=False)

    # the email from header, e.g. AB CD <ab@cd.com>
    # nullable as this field is added after website_email
    website_from = db.Column(db.String(1024), nullable=True)

    # when user clicks on "reply", they will reply to this address.
    # This address allows to hide user personal email
    # this reply email is created every time a website sends an email to user
    # it has the prefix "reply+" or "ra+" to distinguish with other email
    reply_email = db.Column(db.String(512), nullable=False, index=True)

    # whether a contact is created via CC
    is_cc = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    pgp_public_key = db.Column(db.Text, nullable=True)
    pgp_finger_print = db.Column(db.String(512), nullable=True)

    alias = db.relationship(Alias, backref="contacts")
    user = db.relationship(User)

    # the latest reply sent to this contact
    latest_reply: Optional[Arrow] = None

    # to investigate why the website_email is sometimes not correctly parsed
    # the envelope mail_from
    mail_from = db.Column(db.Text, nullable=True, default=None)
    # the message["From"] header
    from_header = db.Column(db.Text, nullable=True, default=None)

    # a contact can have an empty email address, in this case it can't receive emails
    invalid_email = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    @property
    def email(self):
        return self.website_email

    def website_send_to(self):
        """return the email address with name.
        to use when user wants to send an email from the alias
        Return
        "First Last | email at example.com" <ra+random_string@SL>
        """

        # Prefer using contact name if possible
        user = self.user
        name = self.name
        email = self.website_email

        if (
            not user
            or not SenderFormatEnum.has_value(user.sender_format)
            or user.sender_format == SenderFormatEnum.AT.value
        ):
            email = email.replace("@", " at ")
        elif user.sender_format == SenderFormatEnum.A.value:
            email = email.replace("@", "(a)")

        # if no name, try to parse it from website_from
        if not name and self.website_from:
            try:
                name = address.parse(self.website_from).display_name
            except Exception:
                # Skip if website_from is wrongly formatted
                LOG.e(
                    "Cannot parse contact %s website_from %s", self, self.website_from
                )
                name = ""

        # remove all double quote
        if name:
            name = name.replace('"', "")

        name = f"{name} | {email}" if name else email
        # cannot use formataddr here as this field is for email client, not for MTA
        return f'"{name}" <{self.reply_email}>'

    def new_addr(self):
        """
        Replace original email by reply_email. Possible formats:
        - first@example.com via SimpleLogin <reply_email> OR
        - First Last - first at example.com <reply_email> OR
        - First Last - first(a)example.com <reply_email> OR
        - First Last - first@example.com <reply_email> OR
        And return new address with RFC 2047 format

        `new_email` is a special reply address
        """
        user = self.user
        sender_format = user.sender_format if user else SenderFormatEnum.AT.value

        if sender_format == SenderFormatEnum.AT.value:
            formatted_email = self.website_email.replace("@", " at ").strip()
        else:
            formatted_email = self.website_email.replace("@", "(a)").strip()

        # Prefix name to formatted email if available
        new_name = (
            f"{self.name} - {formatted_email}"
            if self.name and self.name != self.website_email.strip()
            else formatted_email
        )


        new_addr = formataddr((new_name, self.reply_email)).strip()
        return new_addr.strip()

    def last_reply(self) -> "EmailLog":
        """return the most recent reply"""
        return (
            EmailLog.query.filter_by(contact_id=self.id, is_reply=True)
            .order_by(desc(EmailLog.created_at))
            .first()
        )

    def __repr__(self):
        return f"<Contact {self.id} {self.website_email} {self.alias_id}>"


class EmailLog(db.Model, ModelMixin):
    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, index=True
    )
    contact_id = db.Column(
        db.ForeignKey(Contact.id, ondelete="cascade"), nullable=False, index=True
    )
    alias_id = db.Column(
        db.ForeignKey(Alias.id, ondelete="cascade"), nullable=True, index=True
    )

    # whether this is a reply
    is_reply = db.Column(db.Boolean, nullable=False, default=False)

    # for ex if alias is disabled, this forwarding is blocked
    blocked = db.Column(db.Boolean, nullable=False, default=False)

    # can happen when user mailbox refuses the forwarded email
    # usually because the forwarded email is too spammy
    bounced = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # happen when an email with auto (holiday) reply
    auto_replied = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # SpamAssassin result
    is_spam = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    spam_score = db.Column(db.Float, nullable=True)
    spam_status = db.Column(db.Text, nullable=True, default=None)
    # do not load this column
    spam_report = deferred(db.Column(db.JSON, nullable=True))

    # Point to the email that has been refused
    refused_email_id = db.Column(
        db.ForeignKey("refused_email.id", ondelete="SET NULL"), nullable=True
    )

    # in forward phase, this is the mailbox that will receive the email
    # in reply phase, this is the mailbox (or a mailbox's authorized address) that sends the email
    mailbox_id = db.Column(
        db.ForeignKey("mailbox.id", ondelete="cascade"), nullable=True
    )

    # in case of bounce, record on what mailbox the email has been bounced
    # useful when an alias has several mailboxes
    bounced_mailbox_id = db.Column(
        db.ForeignKey("mailbox.id", ondelete="cascade"), nullable=True
    )

    refused_email = db.relationship("RefusedEmail")
    forward = db.relationship(Contact)

    contact = db.relationship(Contact, backref="email_logs")
    mailbox = db.relationship("Mailbox", lazy="joined", foreign_keys=[mailbox_id])
    user = db.relationship(User)

    def bounced_mailbox(self) -> str:
        if self.bounced_mailbox_id:
            return Mailbox.get(self.bounced_mailbox_id).email
        # retro-compatibility
        return self.contact.alias.mailboxes[0].email

    def get_action(self) -> str:
        """return the action name: forward|reply|block|bounced"""
        if self.is_reply:
            return "reply"
        elif self.bounced:
            return "bounced"
        elif self.blocked:
            return "block"
        else:
            return "forward"

    def get_phase(self) -> str:
        return "reply" if self.is_reply else "forward"

    def __repr__(self):
        return f"<EmailLog {self.id}>"


class Subscription(db.Model, ModelMixin):
    """Paddle subscription"""

    # Come from Paddle
    cancel_url = db.Column(db.String(1024), nullable=False)
    update_url = db.Column(db.String(1024), nullable=False)
    subscription_id = db.Column(db.String(1024), nullable=False, unique=True)
    event_time = db.Column(ArrowType, nullable=False)
    next_bill_date = db.Column(db.Date, nullable=False)

    cancelled = db.Column(db.Boolean, nullable=False, default=False)

    plan = db.Column(db.Enum(PlanEnum), nullable=False)

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, unique=True
    )

    user = db.relationship(User)

    def plan_name(self):
        if self.plan == PlanEnum.monthly:
            return "Monthly ($4/month)"
        else:
            return "Yearly ($30/year)"

    def __repr__(self):
        return f"<Subscription {self.plan} {self.next_bill_date}>"


class ManualSubscription(db.Model, ModelMixin):
    """
    For users who use other forms of payment and therefore not pass by Paddle
    """

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, unique=True
    )

    # an reminder is sent several days before the subscription ends
    end_at = db.Column(ArrowType, nullable=False)

    # for storing note about this subscription
    comment = db.Column(db.Text, nullable=True)

    # manual subscription are also used for Premium giveaways
    is_giveaway = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    user = db.relationship(User)

    def is_active(self):
        return self.end_at > arrow.now()


class CoinbaseSubscription(db.Model, ModelMixin):
    """
    For subscriptions using Coinbase Commerce
    """

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, unique=True
    )

    # an reminder is sent several days before the subscription ends
    end_at = db.Column(ArrowType, nullable=False)

    # the Coinbase code
    code = db.Column(db.String(64), nullable=True)

    user = db.relationship(User)

    def is_active(self):
        return self.end_at > arrow.now()


# https://help.apple.com/app-store-connect/#/dev58bda3212
_APPLE_GRACE_PERIOD_DAYS = 16


class AppleSubscription(db.Model, ModelMixin):
    """
    For users who have subscribed via Apple in-app payment
    """

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, unique=True
    )

    expires_date = db.Column(ArrowType, nullable=False)

    # to avoid using "Restore Purchase" on another account
    original_transaction_id = db.Column(db.String(256), nullable=False, unique=True)
    receipt_data = db.Column(db.Text(), nullable=False)

    plan = db.Column(db.Enum(PlanEnum), nullable=False)

    user = db.relationship(User)

    def is_valid(self):
        # Todo: take into account grace period?
        return self.expires_date > arrow.now().shift(days=-_APPLE_GRACE_PERIOD_DAYS)


class DeletedAlias(db.Model, ModelMixin):
    """Store all deleted alias to make sure they are NOT reused"""

    email = db.Column(db.String(256), unique=True, nullable=False)

    @classmethod
    def create(cls, **kw):
        raise Exception("should use delete_alias(alias,user) instead")

    def __repr__(self):
        return f"<Deleted Alias {self.email}>"


class EmailChange(db.Model, ModelMixin):
    """Used when user wants to update their email"""

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"),
        nullable=False,
        unique=True,
        index=True,
    )
    new_email = db.Column(db.String(256), unique=True, nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)
    expired = db.Column(ArrowType, nullable=False, default=_expiration_12h)

    user = db.relationship(User)

    def is_expired(self):
        return self.expired < arrow.now()

    def __repr__(self):
        return f"<EmailChange {self.id} {self.new_email} {self.user_id}>"


class AliasUsedOn(db.Model, ModelMixin):
    """Used to know where an alias is created"""

    __table_args__ = (
        db.UniqueConstraint("alias_id", "hostname", name="uq_alias_used"),
    )

    alias_id = db.Column(db.ForeignKey(Alias.id, ondelete="cascade"), nullable=False)
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    alias = db.relationship(Alias)

    hostname = db.Column(db.String(1024), nullable=False)


class ApiKey(db.Model, ModelMixin):
    """used in browser extension to identify user"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(128), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=True)
    last_used = db.Column(ArrowType, default=None)
    times = db.Column(db.Integer, default=0, nullable=False)

    user = db.relationship(User)

    @classmethod
    def create(cls, user_id, name=None, **kwargs):
        code = random_string(60)
        if cls.get_by(code=code):
            code = str(uuid.uuid4())

        return super().create(user_id=user_id, name=name, code=code, **kwargs)


class CustomDomain(db.Model, ModelMixin):
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    domain = db.Column(db.String(128), unique=True, nullable=False)

    # default name to use when user replies/sends from alias
    name = db.Column(db.String(128), nullable=True, default=None)

    # mx verified
    verified = db.Column(db.Boolean, nullable=False, default=False)
    dkim_verified = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    spf_verified = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )
    dmarc_verified = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    _mailboxes = db.relationship("Mailbox", secondary="domain_mailbox", lazy="joined")

    # an alias is created automatically the first time it receives an email
    catch_all = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # option to generate random prefix version automatically
    random_prefix_generation = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # incremented when a check is failed on the domain
    # alert when the number exceeds a threshold
    # used in check_custom_domain()
    nb_failed_checks = db.Column(
        db.Integer, default=0, server_default="0", nullable=False
    )

    # only domain has the ownership verified can go the next DNS step
    # MX verified domains before this change don't have to do the TXT check
    # and therefore have ownership_verified=True
    ownership_verified = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    # randomly generated TXT value for verifying domain ownership
    # the TXT record should be sl-verification=txt_token
    ownership_txt_token = db.Column(db.String(128), nullable=True)

    __table_args__ = (
        Index(
            "ix_unique_domain",  # Index name
            "domain",  # Columns which are part of the index
            unique=True,
            postgresql_where=Column("ownership_verified"),
        ),  # The condition
    )

    user = db.relationship(User, foreign_keys=[user_id])

    @property
    def mailboxes(self):
        return self._mailboxes or [self.user.default_mailbox]

    def nb_alias(self):
        return Alias.filter_by(custom_domain_id=self.id).count()

    def get_trash_url(self):
        return f"{URL}/dashboard/domains/{self.id}/trash"

    def get_ownership_dns_txt_value(self):
        return f"sl-verification={self.ownership_txt_token}"

    @classmethod
    def create(cls, **kw):
        domain: CustomDomain = super(CustomDomain, cls).create(**kw)

        # generate a domain ownership txt token
        if not domain.ownership_txt_token:
            domain.ownership_txt_token = random_string(30)
            db.session.commit()

        return domain

    @property
    def auto_create_rules(self):
        return sorted(self._auto_create_rules, key=lambda rule: rule.order)

    def __repr__(self):
        return f"<Custom Domain {self.domain}>"


class AutoCreateRule(db.Model, ModelMixin):
    """Alias auto creation rule for custom domain"""

    __table_args__ = (
        db.UniqueConstraint(
            "custom_domain_id", "order", name="uq_auto_create_rule_order"
        ),
    )

    custom_domain_id = db.Column(
        db.ForeignKey(CustomDomain.id, ondelete="cascade"), nullable=False
    )
    # an alias is auto created if it matches the regex
    regex = db.Column(db.String(512), nullable=False)

    # the order in which rules are evaluated in case there are multiple rules
    order = db.Column(db.Integer, default=0, nullable=False)

    custom_domain = db.relationship(CustomDomain, backref="_auto_create_rules")

    mailboxes = db.relationship(
        "Mailbox", secondary="auto_create_rule__mailbox", lazy="joined"
    )


class AutoCreateRuleMailbox(db.Model, ModelMixin):
    """store auto create rule - mailbox association"""

    __tablename__ = "auto_create_rule__mailbox"
    __table_args__ = (
        db.UniqueConstraint(
            "auto_create_rule_id", "mailbox_id", name="uq_auto_create_rule_mailbox"
        ),
    )

    auto_create_rule_id = db.Column(
        db.ForeignKey(AutoCreateRule.id, ondelete="cascade"), nullable=False
    )
    mailbox_id = db.Column(
        db.ForeignKey("mailbox.id", ondelete="cascade"), nullable=False
    )


class DomainDeletedAlias(db.Model, ModelMixin):
    """Store all deleted alias for a domain"""

    __table_args__ = (
        db.UniqueConstraint("domain_id", "email", name="uq_domain_trash"),
    )

    email = db.Column(db.String(256), nullable=False)
    domain_id = db.Column(
        db.ForeignKey("custom_domain.id", ondelete="cascade"), nullable=False
    )
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    domain = db.relationship(CustomDomain)

    @classmethod
    def create(cls, **kw):
        raise Exception("should use delete_alias(alias,user) instead")

    def __repr__(self):
        return f"<DomainDeletedAlias {self.id} {self.email}>"


class LifetimeCoupon(db.Model, ModelMixin):
    code = db.Column(db.String(128), nullable=False, unique=True)
    nb_used = db.Column(db.Integer, nullable=False)
    paid = db.Column(db.Boolean, default=False, server_default="0", nullable=False)


class Coupon(db.Model, ModelMixin):
    code = db.Column(db.String(128), nullable=False, unique=True)

    # by default a coupon is for 1 year
    nb_year = db.Column(db.Integer, nullable=False, server_default="1", default=1)

    # whether the coupon has been used
    used = db.Column(db.Boolean, default=False, server_default="0", nullable=False)

    # the user who uses the code
    # non-null when the coupon is used
    used_by_user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=True
    )

    is_giveaway = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )


class Directory(db.Model, ModelMixin):
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    name = db.Column(db.String(128), unique=True, nullable=False)
    # when a directory is disabled, new alias can't be created on the fly
    disabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    user = db.relationship(User, backref="directories")

    _mailboxes = db.relationship(
        "Mailbox", secondary="directory_mailbox", lazy="joined"
    )

    @property
    def mailboxes(self):
        return self._mailboxes or [self.user.default_mailbox]

    def nb_alias(self):
        return Alias.filter_by(directory_id=self.id).count()

    @classmethod
    def delete(cls, obj_id):
        obj: Directory = cls.get(obj_id)
        user = obj.user
        # Put all aliases belonging to this directory to global or domain trash
        for alias in Alias.query.filter_by(directory_id=obj_id):
            from app import alias_utils

            alias_utils.delete_alias(alias, user)

        cls.query.filter(cls.id == obj_id).delete()
        db.session.commit()

    def __repr__(self):
        return f"<Directory {self.name}>"


class Job(db.Model, ModelMixin):
    """Used to schedule one-time job in the future"""

    name = db.Column(db.String(128), nullable=False)
    payload = db.Column(db.JSON)

    # whether the job has been taken by the job runner
    taken = db.Column(db.Boolean, default=False, nullable=False)
    run_at = db.Column(ArrowType)

    def __repr__(self):
        return f"<Job {self.id} {self.name} {self.payload}>"


class Mailbox(db.Model, ModelMixin):
    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, index=True
    )
    email = db.Column(db.String(256), nullable=False, index=True)
    verified = db.Column(db.Boolean, default=False, nullable=False)
    force_spf = db.Column(db.Boolean, default=True, server_default="1", nullable=False)

    # used when user wants to update mailbox email
    new_email = db.Column(db.String(256), unique=True)

    pgp_public_key = db.Column(db.Text, nullable=True)
    pgp_finger_print = db.Column(db.String(512), nullable=True)
    disable_pgp = db.Column(
        db.Boolean, default=False, nullable=False, server_default="0"
    )

    # incremented when a check is failed on the mailbox
    # alert when the number exceeds a threshold
    # used in sanity_check()
    nb_failed_checks = db.Column(
        db.Integer, default=0, server_default="0", nullable=False
    )

    # a mailbox can be disabled if it can't be reached
    disabled = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

    generic_subject = db.Column(db.String(78), nullable=True)

    __table_args__ = (db.UniqueConstraint("user_id", "email", name="uq_mailbox_user"),)

    user = db.relationship(User, foreign_keys=[user_id])

    def pgp_enabled(self) -> bool:
        return bool(self.pgp_finger_print and not self.disable_pgp)

    def nb_alias(self):
        return (
            AliasMailbox.filter_by(mailbox_id=self.id).count()
            + Alias.filter_by(mailbox_id=self.id).count()
        )

    @classmethod
    def delete(cls, obj_id):
        mailbox: Mailbox = cls.get(obj_id)
        user = mailbox.user

        # Put all aliases belonging to this mailbox to global or domain trash
        for alias in Alias.query.filter_by(mailbox_id=obj_id):
            # special handling for alias that has several mailboxes and has mailbox_id=obj_id
            if len(alias.mailboxes) > 1:
                # use the first mailbox found in alias._mailboxes
                first_mb = alias._mailboxes[0]
                alias.mailbox_id = first_mb.id
                alias._mailboxes.remove(first_mb)
            else:
                from app import alias_utils

                # only put aliases that have mailbox as a single mailbox into trash
                alias_utils.delete_alias(alias, user)
            db.session.commit()

        cls.query.filter(cls.id == obj_id).delete()
        db.session.commit()

    @property
    def aliases(self) -> [Alias]:
        ret = Alias.filter_by(mailbox_id=self.id).all()

        for am in AliasMailbox.filter_by(mailbox_id=self.id):
            ret.append(am.alias)

        return ret

    def __repr__(self):
        return f"<Mailbox {self.id} {self.email}>"


class AccountActivation(db.Model, ModelMixin):
    """contains code to activate the user account when they sign up on mobile"""

    user_id = db.Column(
        db.ForeignKey(User.id, ondelete="cascade"), nullable=False, unique=True
    )
    # the activation code is usually 6 digits
    code = db.Column(db.String(10), nullable=False)

    # nb tries decrements each time user enters wrong code
    tries = db.Column(db.Integer, default=3, nullable=False)

    __table_args__ = (
        CheckConstraint(tries >= 0, name="account_activation_tries_positive"),
        {},
    )


class RefusedEmail(db.Model, ModelMixin):
    """Store emails that have been refused, i.e. bounced or classified as spams"""

    # Store the full report, including logs from Sending & Receiving MTA
    full_report_path = db.Column(db.String(128), unique=True, nullable=False)

    # The original email, to display to user
    path = db.Column(db.String(128), unique=True, nullable=True)

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)

    # the email content will be deleted at this date
    delete_at = db.Column(ArrowType, nullable=False, default=_expiration_7d)

    # toggle this when email content (stored at full_report_path & path are deleted)
    deleted = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    def get_url(self, expires_in=3600):
        if self.path:
            return s3.get_url(self.path, expires_in)
        else:
            return s3.get_url(self.full_report_path, expires_in)

    def __repr__(self):
        return f"<Refused Email {self.id} {self.path} {self.delete_at}>"


class Referral(db.Model, ModelMixin):
    """Referral code so user can invite others"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    name = db.Column(db.String(512), nullable=True, default=None)

    code = db.Column(db.String(128), unique=True, nullable=False)

    user = db.relationship(User, foreign_keys=[user_id])

    @property
    def nb_user(self) -> int:
        return User.filter_by(referral_id=self.id, activated=True).count()

    @property
    def nb_paid_user(self) -> int:
        return sum(
            bool(user.is_paid())
            for user in User.filter_by(referral_id=self.id, activated=True)
        )

    def link(self):
        return f"{LANDING_PAGE_URL}?slref={self.code}"


class SentAlert(db.Model, ModelMixin):
    """keep track of alerts sent to user.
    User can receive an alert when there's abnormal activity on their aliases such as
    - reverse-alias not used by the owning mailbox
    - SPF fails when using the reverse-alias
    - bounced email
    - ...

    Different rate controls can then be implemented based on SentAlert:
    - only once alert: an alert type should be sent only once
    - max number of sent per 24H: an alert type should not be sent more than X times in 24h
    """

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    to_email = db.Column(db.String(256), nullable=False)
    alert_type = db.Column(db.String(256), nullable=False)


class AliasMailbox(db.Model, ModelMixin):
    __table_args__ = (
        db.UniqueConstraint("alias_id", "mailbox_id", name="uq_alias_mailbox"),
    )

    alias_id = db.Column(
        db.ForeignKey(Alias.id, ondelete="cascade"), nullable=False, index=True
    )
    mailbox_id = db.Column(
        db.ForeignKey(Mailbox.id, ondelete="cascade"), nullable=False, index=True
    )

    alias = db.relationship(Alias)


class AliasHibp(db.Model, ModelMixin):
    __tablename__ = "alias_hibp"

    __table_args__ = (db.UniqueConstraint("alias_id", "hibp_id", name="uq_alias_hibp"),)

    alias_id = db.Column(
        db.Integer(), db.ForeignKey("alias.id", ondelete="cascade"), index=True
    )
    hibp_id = db.Column(
        db.Integer(), db.ForeignKey("hibp.id", ondelete="cascade"), index=True
    )

    alias = db.relationship(
        "Alias", backref=db.backref("alias_hibp", cascade="all, delete-orphan")
    )
    hibp = db.relationship(
        "Hibp", backref=db.backref("alias_hibp", cascade="all, delete-orphan")
    )


class DirectoryMailbox(db.Model, ModelMixin):
    __table_args__ = (
        db.UniqueConstraint("directory_id", "mailbox_id", name="uq_directory_mailbox"),
    )

    directory_id = db.Column(
        db.ForeignKey(Directory.id, ondelete="cascade"), nullable=False
    )
    mailbox_id = db.Column(
        db.ForeignKey(Mailbox.id, ondelete="cascade"), nullable=False
    )


class DomainMailbox(db.Model, ModelMixin):
    """store the owning mailboxes for a domain"""

    __table_args__ = (
        db.UniqueConstraint("domain_id", "mailbox_id", name="uq_domain_mailbox"),
    )

    domain_id = db.Column(
        db.ForeignKey(CustomDomain.id, ondelete="cascade"), nullable=False
    )
    mailbox_id = db.Column(
        db.ForeignKey(Mailbox.id, ondelete="cascade"), nullable=False
    )


_NB_RECOVERY_CODE = 8
_RECOVERY_CODE_LENGTH = 8


class RecoveryCode(db.Model, ModelMixin):
    """allow user to login in case you lose any of your authenticators"""

    __table_args__ = (db.UniqueConstraint("user_id", "code", name="uq_recovery_code"),)

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    code = db.Column(db.String(16), nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)
    used_at = db.Column(ArrowType, nullable=True, default=None)

    user = db.relationship(User)

    @classmethod
    def generate(cls, user):
        """generate recovery codes for user"""
        # delete all existing codes
        cls.query.filter_by(user_id=user.id).delete()
        db.session.flush()

        nb_code = 0
        while nb_code < _NB_RECOVERY_CODE:
            code = random_string(_RECOVERY_CODE_LENGTH)
            if not cls.get_by(user_id=user.id, code=code):
                cls.create(user_id=user.id, code=code)
                nb_code += 1

        LOG.d("Create recovery codes for %s", user)
        db.session.commit()

    @classmethod
    def empty(cls, user):
        """Delete all recovery codes for user"""
        cls.query.filter_by(user_id=user.id).delete()
        db.session.commit()


class Notification(db.Model, ModelMixin):
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    message = db.Column(db.Text, nullable=False)

    # whether user has marked the notification as read
    read = db.Column(db.Boolean, nullable=False, default=False)


class SLDomain(db.Model, ModelMixin):
    """SimpleLogin domains"""

    __tablename__ = "public_domain"

    domain = db.Column(db.String(128), unique=True, nullable=False)

    # only available for premium accounts
    premium_only = db.Column(
        db.Boolean, nullable=False, default=False, server_default="0"
    )

    def __repr__(self):
        return f"<SLDomain {self.domain} {'Premium' if self.premium_only else 'Free'}"


class Monitoring(db.Model, ModelMixin):
    """
    Store different host information over the time in order to
    - alert issues in (almost) real time
    - analyze data trending
    """

    host = db.Column(db.String(256), nullable=False)

    # Postfix stats
    incoming_queue = db.Column(db.Integer, nullable=False)
    active_queue = db.Column(db.Integer, nullable=False)
    deferred_queue = db.Column(db.Integer, nullable=False)


class BatchImport(db.Model, ModelMixin):
    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    file_id = db.Column(db.ForeignKey(File.id, ondelete="cascade"), nullable=False)
    processed = db.Column(db.Boolean, nullable=False, default=False)
    summary = db.Column(db.Text, nullable=True, default=None)

    file = db.relationship(File)
    user = db.relationship(User)

    def nb_alias(self):
        return Alias.query.filter_by(batch_import_id=self.id).count()

    def __repr__(self):
        return f"<BatchImport {self.id}>"


class AuthorizedAddress(db.Model, ModelMixin):
    """Authorize other addresses to send emails from aliases that are owned by a mailbox"""

    user_id = db.Column(db.ForeignKey(User.id, ondelete="cascade"), nullable=False)
    mailbox_id = db.Column(
        db.ForeignKey(Mailbox.id, ondelete="cascade"), nullable=False
    )
    email = db.Column(db.String(256), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("mailbox_id", "email", name="uq_authorize_address"),
    )

    mailbox = db.relationship(Mailbox, backref="authorized_addresses")

    def __repr__(self):
        return f"<AuthorizedAddress {self.id} {self.email} {self.mailbox_id}>"


class Metric2(db.Model, ModelMixin):
    """
    For storing different metrics like number of users, etc
    Store each metric as a column as opposed to having different rows as in Metric
    """

    date = db.Column(ArrowType, default=arrow.utcnow, nullable=False)

    nb_user = db.Column(db.Float, nullable=True)
    nb_activated_user = db.Column(db.Float, nullable=True)

    nb_premium = db.Column(db.Float, nullable=True)
    nb_apple_premium = db.Column(db.Float, nullable=True)
    nb_cancelled_premium = db.Column(db.Float, nullable=True)
    nb_manual_premium = db.Column(db.Float, nullable=True)
    nb_coinbase_premium = db.Column(db.Float, nullable=True)

    # nb users who have been referred
    nb_referred_user = db.Column(db.Float, nullable=True)
    nb_referred_user_paid = db.Column(db.Float, nullable=True)

    nb_alias = db.Column(db.Float, nullable=True)

    # Obsolete as only for the last 14 days
    nb_forward = db.Column(db.Float, nullable=True)
    nb_block = db.Column(db.Float, nullable=True)
    nb_reply = db.Column(db.Float, nullable=True)
    nb_bounced = db.Column(db.Float, nullable=True)
    nb_spam = db.Column(db.Float, nullable=True)

    # should be used instead
    nb_forward_last_24h = db.Column(db.Float, nullable=True)
    nb_block_last_24h = db.Column(db.Float, nullable=True)
    nb_reply_last_24h = db.Column(db.Float, nullable=True)
    nb_bounced_last_24h = db.Column(db.Float, nullable=True)

    nb_verified_custom_domain = db.Column(db.Float, nullable=True)

    nb_app = db.Column(db.Float, nullable=True)


class Bounce(db.Model, ModelMixin):
    """Record all bounces. Deleted after 7 days"""

    email = db.Column(db.String(256), nullable=False, index=True)


class TransactionalEmail(db.Model, ModelMixin):
    """Storing all email addresses that receive transactional emails, including account email and mailboxes.
    Deleted after 7 days
    """

    email = db.Column(db.String(256), nullable=False, unique=False)


class Payout(db.Model, ModelMixin):
    """Referral payouts"""

    user_id = db.Column(db.ForeignKey("users.id", ondelete="cascade"), nullable=False)

    # in USD
    amount = db.Column(db.Float, nullable=False)

    # BTC, PayPal, etc
    payment_method = db.Column(db.String(256), nullable=False)

    # number of upgraded user included in this payout
    number_upgraded_account = db.Column(db.Integer, nullable=False)

    comment = db.Column(db.Text)

    user = db.relationship(User)


class IgnoredEmail(db.Model, ModelMixin):
    """If an email has mail_from and rcpt_to present in this table, discard it by returning 250 status."""

    mail_from = db.Column(db.String(512), nullable=False)
    rcpt_to = db.Column(db.String(512), nullable=False)


class IgnoreBounceSender(db.Model, ModelMixin):
    """Ignore sender that doesn't correctly handle bounces, for example noreply@github.com"""

    mail_from = db.Column(db.String(512), nullable=False, unique=True)

    def __repr__(self):
        return f"<NoReplySender {self.mail_from}"
