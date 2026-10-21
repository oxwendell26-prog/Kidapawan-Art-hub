import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config.update(
    # In production, set SECRET_KEY as an environment variable.
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    SQLALCHEMY_DATABASE_URI="sqlite:///kidapawan.db",  # stored in the instance/ folder
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5 MB upload limit
    UPLOAD_FOLDER=os.path.join(app.static_folder, "uploads"),
)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}
POST_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
POST_VIDEO_EXT = {"mp4", "webm"}

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access your dashboard."
login_manager.login_message_category = "info"


# ---------- Models ----------
class Artist(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    focus = db.Column(db.String(80), default="")
    experience = db.Column(db.String(80), default="")
    bio = db.Column(db.Text, default="")
    account_type = db.Column(db.String(20), nullable=False, default="artist", server_default="artist")
    is_approved = db.Column(db.Boolean, nullable=False, default=False, server_default=db.text("0"))
    avatar = db.Column(db.String(120))  # profile picture file in static/uploads
    # Privacy and notification preferences
    show_in_directory = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    show_member_since = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    notify_new_orders = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    # Administration
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default=db.text("0"))
    suspended = db.Column(db.Boolean, nullable=False, default=False, server_default=db.text("0"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    works = db.relationship("Artwork", backref="artist", cascade="all, delete-orphan",
                            order_by="Artwork.created_at.desc()")
    conversations = db.relationship(
        "Conversation", backref="artist", cascade="all, delete-orphan",
        foreign_keys="Conversation.artist_id",
    )

    @property
    def initials(self):
        parts = self.name.split()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def slug(self):
        return f"artist-{self.id}"

    @property
    def is_active(self):  # Flask-Login refuses to log in inactive users
        return not self.suspended

    @property
    def public_works(self):
        """Artworks visitors may see from an active, approved artist."""
        return [] if self.suspended or not self.is_approved else [w for w in self.works if not w.hidden]


class Artwork(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    size = db.Column(db.String(60), default="")
    note = db.Column(db.String(300), default="")
    image = db.Column(db.String(120))  # filename in static/uploads
    available = db.Column(db.Boolean, default=True)
    price = db.Column(db.Integer)  # whole pesos; None = price on request
    hidden = db.Column(db.Boolean, nullable=False, default=False, server_default=db.text("0"))  # hidden by an admin
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False)

    @property
    def artist_name(self):
        return self.artist.name

    @property
    def price_label(self):
        return f"₱{self.price:,}" if self.price else "Price on request"

    @property
    def reserved(self):
        """True while an order is waiting for, or accepted by, the artist."""
        return any(o.status in ("pending", "accepted") for o in self.orders)

    @property
    def can_buy(self):
        return bool(self.available and self.price and not self.reserved and self.is_public)

    @property
    def is_public(self):
        return not self.hidden and not self.artist.suspended and self.artist.is_approved

    @property
    def status_label(self):
        if not self.available:
            return "Sold"
        return "Reserved" if self.reserved else "Available"

    @property
    def colors(self):
        """Placeholder colors derived from the title when there is no image."""
        h = int(hashlib.md5(f"{self.id}{self.title}".encode()).hexdigest(), 16) % 360
        return [f"hsl({h} 45% 28%)", f"hsl({(h + 40) % 360} 70% 55%)",
                f"hsl({(h + 200) % 360} 40% 60%)"]


class Order(db.Model):
    __tablename__ = "purchase_order"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, nullable=False,
                      default=lambda: secrets.token_urlsafe(16))  # buyer's private status link
    artwork_id = db.Column(db.Integer, db.ForeignKey("artwork.id"))  # None if artwork was deleted
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("artist.id"), index=True)
    artwork_title = db.Column(db.String(120), nullable=False)  # snapshot at time of order
    price = db.Column(db.Integer, nullable=False)  # snapshot, whole pesos
    buyer_name = db.Column(db.String(80), nullable=False)
    buyer_email = db.Column(db.String(120), nullable=False)
    buyer_phone = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, accepted, completed, declined
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    artwork = db.relationship("Artwork", backref="orders")
    artist = db.relationship("Artist", foreign_keys=[artist_id], backref="orders")
    buyer = db.relationship("Artist", foreign_keys=[buyer_id])

    @property
    def price_label(self):
        return f"₱{self.price:,}"


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    message = db.Column(db.String(200), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    artist = db.relationship("Artist", backref=db.backref("notifications", cascade="all, delete-orphan"))


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False,
                      default=lambda: secrets.token_urlsafe(32))
    artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    participant_artist_id = db.Column(db.Integer, db.ForeignKey("artist.id"), index=True)
    artwork_id = db.Column(db.Integer, db.ForeignKey("artwork.id", ondelete="SET NULL"))
    artwork_title = db.Column(db.String(120), nullable=False)
    client_name = db.Column(db.String(80), nullable=False)
    client_email = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    artwork = db.relationship("Artwork")
    participant_artist = db.relationship("Artist", foreign_keys=[participant_artist_id])
    messages = db.relationship(
        "ChatMessage", backref="conversation", cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    sender = db.Column(db.String(10), nullable=False)
    body = db.Column(db.String(2000), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Post(db.Model):
    __tablename__ = "community_post"

    id = db.Column(db.Integer, primary_key=True)
    caption = db.Column(db.String(1000), nullable=False, default="")
    media_path = db.Column(db.String(120), nullable=False)
    media_type = db.Column(db.String(10), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    user_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    artist = db.relationship("Artist", backref=db.backref("community_posts", cascade="all, delete-orphan"))
    likes = db.relationship("Like", backref="post", cascade="all, delete-orphan")
    comments = db.relationship(
        "Comment", backref="post", cascade="all, delete-orphan",
        order_by="Comment.timestamp",
    )


class Like(db.Model):
    __tablename__ = "post_like"
    __table_args__ = (db.UniqueConstraint("post_id", "user_id", name="uq_post_like_user"),)

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("community_post.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class Comment(db.Model):
    __tablename__ = "post_comment"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("community_post.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("artist.id"), nullable=False, index=True)
    body = db.Column(db.String(1000), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    artist = db.relationship("Artist")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Artist, int(user_id))


with app.app_context():
    db.create_all()
    # Add the "avatar" column if the database is older (no data is lost).
    cols = [r[1] for r in db.session.execute(db.text("PRAGMA table_info(artist)"))]
    for col, ddl in [
        ("account_type", "VARCHAR(20) NOT NULL DEFAULT 'artist'"),
        ("is_approved", "BOOLEAN NOT NULL DEFAULT 0"),
        ("avatar", "VARCHAR(120)"),
        ("show_in_directory", "BOOLEAN NOT NULL DEFAULT 1"),
        ("show_member_since", "BOOLEAN NOT NULL DEFAULT 1"),
        ("notify_new_orders", "BOOLEAN NOT NULL DEFAULT 1"),
        ("is_admin", "BOOLEAN NOT NULL DEFAULT 0"),
        ("suspended", "BOOLEAN NOT NULL DEFAULT 0"),
    ]:
        if col not in cols:
            db.session.execute(db.text(f"ALTER TABLE artist ADD COLUMN {col} {ddl}"))
            db.session.commit()
            if col == "is_approved":
                db.session.execute(db.text(
                    "UPDATE artist SET is_approved = 1 "
                    "WHERE account_type = 'artist'"
                ))
                db.session.commit()
    # Same for the "price" column on artworks.
    art_cols = [r[1] for r in db.session.execute(db.text("PRAGMA table_info(artwork)"))]
    for col, ddl in [("price", "INTEGER"), ("hidden", "BOOLEAN NOT NULL DEFAULT 0")]:
        if col not in art_cols:
            db.session.execute(db.text(f"ALTER TABLE artwork ADD COLUMN {col} {ddl}"))
            db.session.commit()
    order_cols = [row[1] for row in db.session.execute(db.text("PRAGMA table_info(purchase_order)"))]
    if "buyer_id" not in order_cols:
        db.session.execute(db.text(
            "ALTER TABLE purchase_order ADD COLUMN buyer_id INTEGER REFERENCES artist(id)"
        ))
        db.session.commit()
    db.session.execute(db.text(
        "CREATE INDEX IF NOT EXISTS ix_purchase_order_buyer_id ON purchase_order (buyer_id)"
    ))
    db.session.commit()
    conversation_cols = [
        row[1] for row in db.session.execute(db.text("PRAGMA table_info(conversation)"))
    ]
    if "participant_artist_id" not in conversation_cols:
        db.session.execute(db.text(
            "ALTER TABLE conversation ADD COLUMN participant_artist_id "
            "INTEGER REFERENCES artist(id)"
        ))
        db.session.commit()
    db.session.execute(db.text(
        "CREATE INDEX IF NOT EXISTS ix_conversation_participant_artist_id "
        "ON conversation (participant_artist_id)"
    ))
    db.session.commit()


# ---------- Helpers ----------
def safe_next(target):
    """Only accept internal URLs to prevent open redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    if current_user.is_authenticated and current_user.account_type == "client":
        return url_for("messages")
    if (current_user.is_authenticated and current_user.account_type == "artist"
            and not current_user.is_approved):
        return url_for("artist_application_status")
    return url_for("dashboard")


def save_image(file):
    """Return the new filename, or None if no file. Raises ValueError if invalid."""
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("Only JPG, PNG, or WEBP images are accepted.")
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], name))
    return name


def save_post_media(file):
    """Save an allowed community image or video and return its filename and type."""
    if not file or not file.filename:
        raise ValueError("Choose an image or video to upload.")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    header = file.stream.read(12)
    file.stream.seek(0)
    valid_media = {
        "jpg": header.startswith(b"\xff\xd8\xff"),
        "jpeg": header.startswith(b"\xff\xd8\xff"),
        "png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        "mp4": len(header) >= 8 and header[4:8] == b"ftyp",
        "webm": header.startswith(b"\x1a\x45\xdf\xa3"),
    }
    if not valid_media.get(ext, False):
        raise ValueError("The uploaded file does not match its image or video extension.")
    if ext in POST_IMAGE_EXT:
        media_type = "image"
    elif ext in POST_VIDEO_EXT:
        media_type = "video"
    else:
        raise ValueError("Use a JPG, PNG, WEBP, MP4, or WEBM file.")
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], name))
    return name, media_type


def remove_image(name):
    if name:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], name))
        except OSError:
            pass


def parse_price(raw):
    """Return (price, error). An empty value means "price on request"."""
    raw = raw.replace(",", "").replace("₱", "").strip()
    if not raw:
        return None, None
    try:
        price = int(raw)
    except ValueError:
        price = 0
    if not (1 <= price <= 100_000_000):
        return None, "Enter a valid price in pesos (a whole number above zero), or leave it empty."
    return price, None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,20}$")


@app.context_processor
def inject_counts():
    if current_user.is_authenticated:
        return {
            "pending_orders": Order.query.filter_by(
                artist_id=current_user.id, status="pending").count(),
            "unread_notifications": Notification.query.filter_by(
                artist_id=current_user.id, is_read=False).count(),
            "unread_messages": ChatMessage.query.join(Conversation).filter(
                or_(
                    db.and_(
                        Conversation.artist_id == current_user.id,
                        ChatMessage.sender == "client",
                    ),
                    db.and_(
                        Conversation.participant_artist_id == current_user.id,
                        ChatMessage.sender == "artist",
                    ),
                ),
                ChatMessage.is_read.is_(False),
            ).count(),
        }
    return {"pending_orders": 0, "unread_notifications": 0, "unread_messages": 0}


@app.template_filter("ph_time")
def ph_time(dt):
    """Show a UTC timestamp in Philippine time (UTC+8)."""
    return (dt + timedelta(hours=8)).strftime("%B %d, %Y, %I:%M %p") if dt else ""


def visible_artworks():
    """Artworks the public may see from approved, active artists."""
    return (Artwork.query.join(Artist, Artwork.artist_id == Artist.id)
        .filter(Artwork.hidden.is_(False), Artist.suspended.is_(False),
            Artist.is_approved.is_(True)))


def can_see_private(owner_id):
    return current_user.is_authenticated and (current_user.id == owner_id or current_user.is_admin)


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def artist_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user.account_type == "client":
            return redirect(url_for("messages"))
        if current_user.account_type != "artist":
            abort(403)
        if not current_user.is_approved:
            flash("Your artist application is waiting for admin approval.", "info")
            return redirect(url_for("artist_application_status"))
        return view(*args, **kwargs)
    return wrapper


def client_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("client_login", next=request.path))
        if current_user.account_type != "client":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.before_request
def block_suspended_accounts():
    if current_user.is_authenticated and current_user.suspended:
        logout_user()
        flash("This account has been suspended. Please contact the site administrators.", "error")
        return redirect(url_for("home"))


@app.cli.command("make-admin")
@click.argument("email")
def make_admin(email):
    """Give an existing account admin rights:  flask --app app make-admin you@example.com"""
    artist = Artist.query.filter(db.func.lower(Artist.email) == email.strip().lower()).first()
    if not artist:
        click.echo("No account uses that email. Sign up on the site first, then run this again.")
        return
    artist.is_admin = True
    if artist.account_type == "artist":
        artist.is_approved = True
    db.session.commit()
    click.echo(f"{artist.name} ({artist.email}) is now an admin.")


# ---------- Routes ----------
@app.route("/")
def home():
    latest = visible_artworks().order_by(Artwork.created_at.desc()).limit(5).all()
    artists = Artist.query.filter_by(
        account_type="artist", is_approved=True, suspended=False,
    ).all()
    featured = sorted((a for a in artists if a.show_in_directory),
                      key=lambda a: len(a.public_works), reverse=True)[:3]
    return render_template(
        "index.html",
        hero=latest[0] if latest else None,
        recent=latest[1:],
        featured_artists=featured,
        total_works=visible_artworks().count(),
        total_artists=len(artists),
    )


@app.route("/community")
@login_required
def community():
    if current_user.account_type not in {"artist", "client"}:
        abort(403)
    posts = (Post.query.join(Artist)
             .filter(Artist.suspended.is_(False), Artist.is_approved.is_(True))
             .order_by(Post.timestamp.desc())
             .limit(50).all())
    post_ids = [post.id for post in posts]
    liked_ids = set()
    if current_user.is_authenticated and post_ids:
        liked_ids = {
            like.post_id for like in Like.query.filter(
                Like.user_id == current_user.id,
                Like.post_id.in_(post_ids),
            ).all()
        }
    return render_template("community.html", posts=posts, liked_ids=liked_ids)


@app.route("/community/posts", methods=["POST"])
@login_required
def create_post():
    if current_user.account_type != "artist" or not current_user.is_approved:
        abort(403)
    caption = request.form.get("caption", "").strip()
    if len(caption) > 1000:
        flash("Caption must be 1,000 characters or fewer.", "error")
        return redirect(url_for("community"))

    try:
        media_path, media_type = save_post_media(request.files.get("media"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("community"))

    db.session.add(Post(
        caption=caption,
        media_path=media_path,
        media_type=media_type,
        user_id=current_user.id,
    ))
    db.session.commit()
    flash("Your post has been shared with the community.", "success")
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    post = db.get_or_404(Post, post_id)
    if post.user_id != current_user.id:
        abort(403)

    media_path = post.media_path
    db.session.delete(post)
    db.session.commit()
    remove_image(media_path)
    flash("Your post has been deleted.", "info")
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/like", methods=["POST"])
@login_required
def toggle_post_like(post_id):
    post = db.get_or_404(Post, post_id)
    if post.artist.suspended:
        abort(404)
    existing = Like.query.filter_by(post_id=post.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
    else:
        db.session.add(Like(post_id=post.id, user_id=current_user.id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("You already liked this post.", "info")
    return redirect(url_for("community"))


@app.route("/community/posts/<int:post_id>/comments", methods=["POST"])
@login_required
def add_post_comment(post_id):
    post = db.get_or_404(Post, post_id)
    if post.artist.suspended:
        abort(404)
    body = request.form.get("body", "").strip()
    if not body or len(body) > 1000:
        flash("Comment must be between 1 and 1,000 characters.", "error")
        return redirect(url_for("community"))

    db.session.add(Comment(post_id=post.id, user_id=current_user.id, body=body))
    db.session.commit()
    flash("Your comment has been added.", "success")
    return redirect(url_for("community"))


@app.route("/community/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_post_comment(comment_id):
    comment = db.get_or_404(Comment, comment_id)
    if comment.user_id != current_user.id:
        abort(403)

    db.session.delete(comment)
    db.session.commit()
    flash("Your comment has been deleted.", "info")
    return redirect(url_for("community"))


@app.route("/artists")
def artists():
    return render_template("artists.html", artists=Artist.query.filter_by(
        account_type="artist", is_approved=True, show_in_directory=True,
        suspended=False).order_by(Artist.name).all())


@app.route("/artists/<int:artist_id>")
def artist_detail(artist_id):
    artist = db.get_or_404(Artist, artist_id)
    if artist.account_type != "artist" and not current_user.is_admin:
        abort(404)
    privileged = can_see_private(artist.id)
    if (artist.suspended or not artist.is_approved) and not privileged:
        abort(404)
    works = artist.works if privileged else artist.public_works
    return render_template("artist.html", artist=artist, works=works)


@app.route("/artists/<int:artist_id>/message", methods=["GET", "POST"])
@client_required
def artist_message(artist_id):
    recipient = db.get_or_404(Artist, artist_id)
    if recipient.account_type != "artist" or recipient.suspended or not recipient.is_approved:
        abort(404)
    if recipient.id == current_user.id:
        abort(403)

    if current_user.account_type == "client":
        owner_id, participant_id = recipient.id, current_user.id
    else:
        owner_id, participant_id = sorted((current_user.id, recipient.id))
    conversation = Conversation.query.filter(
        Conversation.participant_artist_id == participant_id,
        Conversation.artist_id == owner_id,
    ).first()
    if request.method == "POST":
        body = request.form.get("message", "").strip()
        if not body or len(body) > 2000:
            flash("Enter a message (up to 2,000 characters).", "error")
            return render_template(
                "new_artist_message.html", artist=recipient, message=body,
            ), 400

        if conversation is None:
            participant = db.session.get(Artist, participant_id)
            conversation = Conversation(
                artist_id=owner_id,
                participant_artist_id=participant_id,
                artwork_title="Artist conversation",
                client_name=participant.name,
                client_email=participant.email,
            )
            db.session.add(conversation)
            db.session.flush()

        sender = "artist" if current_user.id == owner_id else "client"
        conversation.messages.append(ChatMessage(sender=sender, body=body))
        conversation.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))

    if conversation:
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))
    return render_template("new_artist_message.html", artist=recipient, message="")


@app.route("/artworks/<int:artwork_id>")
def artwork_detail(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if not artwork.is_public and not can_see_private(artwork.artist_id):
        abort(404)
    more = [w for w in artwork.artist.public_works if w.id != artwork.id][:3]
    return render_template("artwork.html", artwork=artwork, more=more)


@app.route("/artwork/<int:artwork_id>/message", methods=["GET", "POST"])
@client_required
def new_conversation(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if not artwork.is_public:
        abort(404)
    if current_user.id == artwork.artist_id:
        abort(403)

    form, errors = request.form, {}
    if request.method == "POST":
        name = current_user.name
        email = current_user.email
        body = form.get("message", "").strip()
        if not body or len(body) > 2000:
            errors["message"] = "Enter a message (up to 2,000 characters)."
        if not errors:
            conversation = Conversation(
                artist_id=artwork.artist_id,
                participant_artist_id=current_user.id,
                artwork_id=artwork.id,
                artwork_title=artwork.title,
                client_name=name,
                client_email=email,
                messages=[ChatMessage(sender="client", body=body)],
            )
            db.session.add(conversation)
            db.session.commit()
            flash("Your message has been sent. Continue the conversation from Messages.", "success")
            return redirect(url_for("artist_conversation", conversation_id=conversation.id))
        flash("Please fix the highlighted fields.", "error")
        return render_template(
            "new_message.html", artwork=artwork, form=form, errors=errors,
        ), 400
    return render_template("new_message.html", artwork=artwork, form=form, errors=errors)


@app.route("/conversation/<token>", methods=["GET", "POST"])
def client_conversation(token):
    conversation = Conversation.query.filter_by(token=token).first_or_404()
    if conversation.participant_artist_id is not None:
        if not current_user.is_authenticated or current_user.id not in (
            conversation.artist_id, conversation.participant_artist_id,
        ):
            abort(403)
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))
    if not current_user.is_authenticated:
        return redirect(url_for("client_login", next=request.path))
    if current_user.account_type == "client":
        if current_user.email != conversation.client_email:
            abort(403)
        conversation.participant_artist_id = current_user.id
        db.session.commit()
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))
    if current_user.id == conversation.artist_id:
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))
    abort(403)

    if request.method == "POST":
        body = request.form.get("message", "").strip()
        if not body or len(body) > 2000:
            flash("Enter a message (up to 2,000 characters).", "error")
            return redirect(url_for("client_conversation", token=token))
        conversation.messages.append(ChatMessage(sender="client", body=body))
        conversation.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Your reply has been sent.", "success")
        return redirect(url_for("client_conversation", token=token))

    ChatMessage.query.filter_by(
        conversation_id=conversation.id, sender="artist", is_read=False,
    ).update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return render_template(
        "conversation.html", conversation=conversation, artist_view=False,
    )


@app.route("/messages")
@login_required
def messages():
    conversations = Conversation.query.filter(or_(
        Conversation.artist_id == current_user.id,
        Conversation.participant_artist_id == current_user.id,
    )).order_by(Conversation.updated_at.desc()).all()
    return render_template("messages.html", conversations=conversations)


@app.route("/messages/<int:conversation_id>", methods=["GET", "POST"])
@login_required
def artist_conversation(conversation_id):
    conversation = Conversation.query.filter(
        Conversation.id == conversation_id,
        or_(
            Conversation.artist_id == current_user.id,
            Conversation.participant_artist_id == current_user.id,
        ),
    ).first_or_404()
    viewer_sender = "artist" if current_user.id == conversation.artist_id else "client"
    other_sender = "client" if viewer_sender == "artist" else "artist"
    if request.method == "POST":
        body = request.form.get("message", "").strip()
        if not body or len(body) > 2000:
            flash("Enter a message (up to 2,000 characters).", "error")
            return redirect(url_for("artist_conversation", conversation_id=conversation.id))
        conversation.messages.append(ChatMessage(sender=viewer_sender, body=body))
        conversation.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Your reply has been sent.", "success")
        return redirect(url_for("artist_conversation", conversation_id=conversation.id))

    ChatMessage.query.filter_by(
        conversation_id=conversation.id, sender=other_sender, is_read=False,
    ).update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return render_template(
        "conversation.html", conversation=conversation, artist_view=True,
        viewer_sender=viewer_sender,
    )


@app.route("/messages/<int:conversation_id>/delete", methods=["POST"])
@login_required
def delete_conversation(conversation_id):
    conversation = Conversation.query.filter(
        Conversation.id == conversation_id,
        or_(
            Conversation.artist_id == current_user.id,
            Conversation.participant_artist_id == current_user.id,
        ),
    ).first_or_404()
    db.session.delete(conversation)
    db.session.commit()
    flash("Conversation deleted.", "info")
    return redirect(url_for("messages"))


@app.route("/artworks")
def artworks():
    paintings = visible_artworks().order_by(Artwork.created_at.desc()).all()
    return render_template("artworks.html", paintings=paintings)


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()[:100]
    paintings = []
    artists = []
    if query:
        pattern = f"%{query}%"
        paintings = (visible_artworks()
                     .filter(or_(Artwork.title.ilike(pattern), Artist.name.ilike(pattern)))
                     .order_by(Artwork.created_at.desc()).limit(50).all())
        artists = (Artist.query.filter_by(
            account_type="artist", is_approved=True,
            show_in_directory=True, suspended=False,
        ).filter(Artist.name.ilike(pattern)).order_by(Artist.name).limit(50).all())
    return render_template("search.html", query=query, paintings=paintings, artists=artists)


@app.route("/register", methods=["GET", "POST"])
def register():
    return register_account("artist")


@app.route("/client/register", methods=["GET", "POST"])
def client_register():
    return register_account("client")


@app.route("/artist/application")
@login_required
def artist_application_status():
    if current_user.account_type != "artist":
        return redirect(url_for("messages"))
    if current_user.is_approved:
        return redirect(url_for("dashboard"))
    return render_template("artist_application_status.html")


@app.route("/client/apply-as-artist", methods=["GET", "POST"])
@login_required
@client_required
def client_apply_as_artist():
    form = request.form if request.method == "POST" else {
        "focus": current_user.focus or "",
        "experience": current_user.experience or "",
        "bio": current_user.bio or "",
    }
    errors = {}
    if request.method == "POST":
        current_user.account_type = "artist"
        current_user.is_approved = False
        current_user.show_in_directory = False
        current_user.focus = form.get("focus", "").strip()[:80]
        current_user.experience = form.get("experience", "").strip()[:80]
        current_user.bio = form.get("bio", "").strip()[:600]
        db.session.commit()
        flash("Your artist application has been submitted and is awaiting admin approval.", "success")
        return redirect(url_for("artist_application_status"))
    return render_template("artist_application_form.html", form=form, errors=errors)


def register_account(account_type):
    if current_user.is_authenticated and (
        account_type != "client" or current_user.account_type == "client"
    ):
        if current_user.account_type == "client":
            return redirect(url_for("messages"))
        return redirect(url_for(
            "dashboard" if current_user.is_approved else "artist_application_status"
        ))
    form = request.form
    errors = {}
    if request.method == "POST":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        if len(name) < 2:
            errors["name"] = "Enter your full name."
        if "@" not in email or "." not in email.split("@")[-1]:
            errors["email"] = "Enter a valid email address."
        else:
            existing_account = Artist.query.filter(
                db.func.lower(Artist.email) == email,
            ).first()
            if existing_account and account_type == "artist" and existing_account.account_type == "client":
                errors["email"] = "This Gmail already has a client account. Log in and use Apply as artist from your account menu."
            elif existing_account:
                errors["email"] = "This email is already in use. Log in to the existing account."
        if len(password) < 8:
            errors["password"] = "Password must be at least 8 characters."
        elif password != form.get("confirm", ""):
            errors["confirm"] = "Passwords do not match."
        if not errors:
            artist = Artist(
                name=name, email=email,
                account_type=account_type,
                is_approved=account_type == "client",
                password_hash=generate_password_hash(password),
                focus=form.get("focus", "").strip()[:80] if account_type == "artist" else "",
                experience=form.get("experience", "").strip()[:80] if account_type == "artist" else "",
                bio=form.get("bio", "").strip()[:600] if account_type == "artist" else "",
            )
            db.session.add(artist)
            try:
                db.session.commit()
            except IntegrityError:  # two sign-ups with the same email at the same time
                db.session.rollback()
                errors["email"] = "This email is already in use. Use a different email or log in."
            else:
                login_user(artist)
                if account_type == "client":
                    flash("Your client account is ready. You can now message artists and join the community.", "success")
                    return redirect(safe_next(request.args.get("next")))
                flash("Your artist application has been submitted and is awaiting admin approval.", "success")
                return redirect(url_for("artist_application_status"))
        flash("Please fix the highlighted fields.", "error")
        return render_template("register.html", form=form, errors=errors, account_type=account_type), 400
    return render_template("register.html", form=form, errors=errors, account_type=account_type)


@app.route("/login")
def login():
    if current_user.is_authenticated:
        if current_user.account_type == "client":
            return redirect(url_for("messages"))
        return redirect(url_for(
            "dashboard" if current_user.is_approved else "artist_application_status"
        ))

    return render_template("login_choice.html", next_url=request.args.get("next", ""))


@app.route("/artist/login", methods=["GET", "POST"])
def artist_login():
    return login_account("artist")


@app.route("/client/login", methods=["GET", "POST"])
def client_login():
    return login_account("client")


def login_account(account_type):
    if current_user.is_authenticated:
        if current_user.account_type == "client":
            return redirect(url_for("messages"))
        return redirect(url_for(
            "dashboard" if current_user.is_approved else "artist_application_status"
        ))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        artist = Artist.query.filter_by(email=email).first()
        if artist and check_password_hash(artist.password_hash, request.form.get("password", "")):
            if artist.suspended:
                flash("This account has been suspended. Please contact the site administrators.", "error")
                return render_template("login.html", email=email, account_type=account_type), 403
            if artist.account_type != account_type:
                flash("This account uses the other sign-in option.", "error")
                return render_template(
                    "login.html", email=email, account_type=account_type,
                    next_url=request.args.get("next", ""),
                ), 403
            login_user(artist, remember=bool(request.form.get("remember")))
            if artist.account_type == "artist" and not artist.is_approved:
                flash("Your artist application is waiting for admin approval.", "info")
                return redirect(url_for("artist_application_status"))
            return redirect(safe_next(request.args.get("next")))
        flash("Incorrect email or password.", "error")
        return render_template(
            "login.html", email=email, account_type=account_type,
            next_url=request.args.get("next", ""),
        ), 401
    return render_template(
        "login.html", email="", account_type=account_type,
        next_url=request.args.get("next", ""),
    )


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
@artist_required
def dashboard():
    now_year = datetime.now().year
    form = request.form
    if request.method == "POST":
        title = form.get("title", "").strip()
        error = None
        try:
            year = int(form.get("year") or now_year)
        except ValueError:
            year = None
        price, price_error = parse_price(form.get("price", ""))
        if not title or len(title) > 120:
            error = "Enter the artwork title (up to 120 characters)."
        elif year is None or not (1900 <= year <= now_year + 1):
            error = "Enter a valid year."
        elif price_error:
            error = price_error
        image = None
        if not error:
            try:
                image = save_image(request.files.get("image"))
            except ValueError as e:
                error = str(e)
        if error:
            flash(error, "error")
            return render_template("dashboard.html", form=form, now_year=now_year), 400
        db.session.add(Artwork(
            title=title, year=year, price=price,
            size=form.get("size", "").strip()[:60],
            note=form.get("note", "").strip()[:300],
            available=bool(form.get("available")),
            image=image, artist_id=current_user.id,
        ))
        db.session.commit()
        flash(f"“{title}” has been added to the gallery.", "success")
        return redirect(url_for("dashboard"))
    return render_template("dashboard.html", form={"available": "1"}, now_year=now_year)


@app.route("/profile", methods=["GET", "POST"])
@login_required
@artist_required
def profile():
    errors = {}
    if request.method == "POST":
        form = request.form
        name = form.get("name", "").strip()
        if len(name) < 2:
            errors["name"] = "Enter your full name."
        new_avatar = None
        if not errors:
            try:
                new_avatar = save_image(request.files.get("avatar"))
            except ValueError as e:
                errors["avatar"] = str(e)
        if not errors:
            current_user.name = name[:80]
            current_user.focus = form.get("focus", "").strip()[:80]
            current_user.experience = form.get("experience", "").strip()[:80]
            current_user.bio = form.get("bio", "").strip()[:600]
            if new_avatar:
                remove_image(current_user.avatar)
                current_user.avatar = new_avatar
            db.session.commit()
            flash("Your profile has been saved.", "success")
            return redirect(url_for("profile"))
        flash("Please fix the highlighted field.", "error")
        return render_template("profile.html", form=form, errors=errors), 400
    form = {"name": current_user.name, "focus": current_user.focus or "",
            "experience": current_user.experience or "", "bio": current_user.bio or ""}
    return render_template("profile.html", form=form, errors=errors)


@app.route("/profile/avatar/delete", methods=["POST"])
@login_required
@artist_required
def delete_avatar():
    if current_user.avatar:
        remove_image(current_user.avatar)
        current_user.avatar = None
        db.session.commit()
        flash("Your profile picture has been removed.", "info")
    return redirect(url_for("profile"))


@app.route("/artwork/<int:artwork_id>/price", methods=["POST"])
@login_required
@artist_required
def update_price(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if artwork.artist_id != current_user.id:
        abort(403)
    if artwork.reserved:
        flash("Price can’t be changed while this artwork has an active order.", "error")
        return redirect(url_for("dashboard"))
    price, error = parse_price(request.form.get("price", ""))
    if error:
        flash(error, "error")
    else:
        artwork.price = price
        db.session.commit()
        flash("Price updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/artwork/<int:artwork_id>/edit", methods=["GET", "POST"])
@login_required
@artist_required
def edit_artwork(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if artwork.artist_id != current_user.id:
        abort(403)

    now_year = datetime.now().year
    reserved = artwork.reserved
    if request.method == "POST":
        form = request.form
        title = form.get("title", "").strip()
        try:
            year = int(form.get("year") or "")
        except ValueError:
            year = None

        errors = {}
        if not title or len(title) > 120:
            errors["title"] = "Enter the artwork title (up to 120 characters)."
        if year is None or not 1900 <= year <= now_year + 1:
            errors["year"] = "Enter a valid year."

        price = artwork.price
        available = artwork.available
        if not reserved:
            price, price_error = parse_price(form.get("price", ""))
            if price_error:
                errors["price"] = price_error
            available = bool(form.get("available"))

        image = None
        if not errors:
            try:
                image = save_image(request.files.get("image"))
            except ValueError as error:
                errors["image"] = str(error)

        if errors:
            flash("Please fix the highlighted fields.", "error")
            return render_template(
                "edit_artwork.html",
                artwork=artwork,
                form=form,
                errors=errors,
                now_year=now_year,
                reserved=reserved,
            ), 400

        old_image = artwork.image
        artwork.title = title
        artwork.year = year
        artwork.size = form.get("size", "").strip()[:60]
        artwork.note = form.get("note", "").strip()[:300]
        artwork.price = price
        artwork.available = available
        if image:
            artwork.image = image
        db.session.commit()
        if image:
            remove_image(old_image)
        flash(f"“{artwork.title}” has been updated.", "success")
        return redirect(url_for("dashboard"))

    form = {
        "title": artwork.title,
        "year": artwork.year,
        "size": artwork.size or "",
        "note": artwork.note or "",
        "price": artwork.price or "",
        "available": "1" if artwork.available else "",
    }
    return render_template(
        "edit_artwork.html",
        artwork=artwork,
        form=form,
        errors={},
        now_year=now_year,
        reserved=reserved,
    )


@app.route("/artwork/<int:artwork_id>/delete", methods=["POST"])
@login_required
@artist_required
def delete_artwork(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if artwork.artist_id != current_user.id:
        abort(403)
    if artwork.reserved:
        flash("This artwork has an active order. Accept, decline, or complete the order first.", "error")
        return redirect(url_for("dashboard"))
    remove_image(artwork.image)
    Conversation.query.filter_by(artwork_id=artwork.id).update(
        {"artwork_id": None}, synchronize_session=False,
    )
    db.session.delete(artwork)
    db.session.commit()
    flash("Artwork deleted.", "info")
    return redirect(url_for("dashboard"))


@app.route("/artworks/<int:artwork_id>/buy", methods=["GET", "POST"])
@client_required
def checkout(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if not artwork.is_public:
        abort(404)
    detail = redirect(url_for("artwork_detail", artwork_id=artwork.id))
    if current_user.id == artwork.artist_id:
        flash("You can’t buy your own artwork.", "info")
        return detail
    if not artwork.can_buy:
        flash("This artwork is not available for purchase right now.", "info")
        return detail

    form = request.form if request.method == "POST" else {
        "name": current_user.name,
        "email": current_user.email,
    }
    errors = {}
    if request.method == "POST":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        phone = form.get("phone", "").strip()
        if len(name) < 2:
            errors["name"] = "Enter your full name."
        if not EMAIL_RE.match(email):
            errors["email"] = "Enter a valid email address."
        if not PHONE_RE.match(phone):
            errors["phone"] = "Enter a valid phone number so the artist can reach you."
        if not errors:
            order = Order(
                artwork_id=artwork.id, artist_id=artwork.artist_id,
                buyer_id=current_user.id,
                artwork_title=artwork.title, price=artwork.price,
                buyer_name=name[:80], buyer_email=email[:120], buyer_phone=phone,
                note=form.get("note", "").strip()[:500],
            )
            db.session.add(order)
            db.session.flush()  # kunin ang order.id
            if artwork.artist.notify_new_orders:
                db.session.add(Notification(
                    artist_id=artwork.artist_id,
                    message=f"{order.buyer_name} wants to buy “{artwork.title}” for {order.price_label}.",
                ))
            db.session.commit()
            return redirect(url_for("order_status", token=order.token))
        flash("Please fix the highlighted fields.", "error")
        return render_template("checkout.html", artwork=artwork, form=form, errors=errors), 400
    return render_template("checkout.html", artwork=artwork, form=form, errors=errors)


@app.route("/track", methods=["GET", "POST"])
@login_required
def track_order():
    """Let a signed-in client find only their own order by order number."""
    if current_user.account_type != "client":
        abort(403)
    form = request.form
    if request.method == "POST":
        number = form.get("order_number", "").strip().lstrip("#")
        order = None
        if number.isdigit() and len(number) <= 9:
            order = Order.query.filter_by(id=int(number), buyer_id=current_user.id).first()
            if order is None:
                order = Order.query.filter_by(
                    id=int(number), buyer_id=None, buyer_email=current_user.email,
                ).first()
                if order:
                    order.buyer_id = current_user.id
                    db.session.commit()
        if order:
            return redirect(url_for("order_status", token=order.token))
        flash("We couldn’t find one of your orders with that number.", "error")
        return render_template("track.html", form=form), 400
    return render_template("track.html", form=form)


@app.route("/order/<token>")
@login_required
def order_status(token):
    order = Order.query.filter_by(token=token).first_or_404()
    if current_user.account_type != "client":
        abort(403)
    if order.buyer_id != current_user.id:
        if order.buyer_id is None and order.buyer_email == current_user.email:
            order.buyer_id = current_user.id
            db.session.commit()
        else:
            abort(404)
    return render_template("order_status.html", order=order)


@app.route("/orders")
@login_required
@artist_required
def orders():
    items = Order.query.filter_by(artist_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template("orders.html", orders=items)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form, errors = request.form, {}
    if request.method == "POST":
        action = form.get("action")
        if action == "email":
            new_email = form.get("new_email", "").strip().lower()
            if not check_password_hash(current_user.password_hash, form.get("email_password", "")):
                errors["email_password"] = "Incorrect password."
            if not EMAIL_RE.match(new_email):
                errors["new_email"] = "Enter a valid email address."
            elif new_email == current_user.email:
                errors["new_email"] = "That is already your email address."
            elif Artist.query.filter(db.func.lower(Artist.email) == new_email,
                                     Artist.id != current_user.id).first():
                errors["new_email"] = "This email is already in use."
            if not errors:
                current_user.email = new_email
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    errors["new_email"] = "This email is already in use."
                else:
                    flash("Your email address has been updated.", "success")
                    return redirect(url_for("settings"))
        elif action == "password":
            new_password = form.get("new_password", "")
            if not check_password_hash(current_user.password_hash, form.get("current_password", "")):
                errors["current_password"] = "Incorrect password."
            if len(new_password) < 8:
                errors["new_password"] = "Password must be at least 8 characters."
            elif new_password != form.get("confirm_password", ""):
                errors["confirm_password"] = "Passwords do not match."
            if not errors:
                current_user.password_hash = generate_password_hash(new_password)
                db.session.commit()
                flash("Your password has been changed.", "success")
                return redirect(url_for("settings"))
        else:
            abort(400)
        flash("Please fix the highlighted fields.", "error")
        return render_template("settings.html", form=form, errors=errors), 400
    return render_template("settings.html", form=form, errors=errors)


@app.route("/privacy", methods=["GET", "POST"])
@login_required
@artist_required
def privacy():
    if request.method == "POST":
        current_user.show_in_directory = bool(request.form.get("show_in_directory"))
        current_user.show_member_since = bool(request.form.get("show_member_since"))
        db.session.commit()
        flash("Your privacy settings have been saved.", "success")
        return redirect(url_for("privacy"))
    return render_template("privacy.html")


@app.route("/notifications/preferences", methods=["POST"])
@login_required
@artist_required
def notification_preferences():
    current_user.notify_new_orders = bool(request.form.get("notify_new_orders"))
    db.session.commit()
    flash("Your notification preferences have been saved.", "success")
    return redirect(url_for("notifications"))


@app.route("/notifications")
@login_required
@artist_required
def notifications():
    items = (Notification.query.filter_by(artist_id=current_user.id)
             .order_by(Notification.created_at.desc()).limit(50).all())
    unread_ids = {n.id for n in items if not n.is_read}
    if unread_ids:  # opening the page marks them as read
        Notification.query.filter(Notification.id.in_(unread_ids)).update(
            {"is_read": True}, synchronize_session=False)
        db.session.commit()
    return render_template("notifications.html", items=items, unread_ids=unread_ids)


@app.route("/notifications/clear", methods=["POST"])
@login_required
@artist_required
def clear_notifications():
    Notification.query.filter_by(artist_id=current_user.id).delete()
    db.session.commit()
    flash("Notifications cleared.", "info")
    return redirect(url_for("notifications"))


ORDER_TRANSITIONS = {
    ("pending", "accept"): ("accepted", "Order accepted. The painting stays reserved for the buyer."),
    ("pending", "decline"): ("declined", "Order declined. The painting is available again."),
    ("accepted", "complete"): ("completed", "Marked as sold. Congratulations!"),
    ("accepted", "decline"): ("declined", "Order cancelled. The painting is available again."),
}


@app.route("/orders/<int:order_id>/<action>", methods=["POST"])
@login_required
@artist_required
def order_action(order_id, action):
    order = db.get_or_404(Order, order_id)
    if order.artist_id != current_user.id:
        abort(403)
    transition = ORDER_TRANSITIONS.get((order.status, action))
    if not transition:
        flash("That action isn’t available for this order.", "error")
        return redirect(url_for("orders"))
    order.status, message = transition
    if order.status == "completed" and order.artwork:
        order.artwork.available = False
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("orders"))

@app.route("/create-admin")
def create_admin():
    # Palitan mo ng gusto mong email at password para sa admin account mo
    admin_email = "PaoloAdmin143@gmail.com"
    admin_password = "Adminpaolo143"
    
    # Hanapin kung may existing na account na gumagamit ng email na ito
    user = Artist.query.filter_by(email=admin_email).first()
    if user:
        user.account_type = "admin" # o kung ano ang field para sa admin role mo
        user.is_approved = True
        db.session.commit()
        return "Existing account promoted to Admin successfully!"
    else:
        # Kung wala pa, gagawa ng bagong admin account
        from werkzeug.security import generate_password_hash
        new_admin = Artist(
            email=admin_email,
            password=generate_password_hash(admin_password),
            account_type="admin",
            is_approved=True
        )
        db.session.add(new_admin)
        db.session.commit()
        return "New Admin account created successfully!"

# ---------- Admin ----------
@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = dict(
        artists=Artist.query.filter_by(account_type="artist", is_approved=True).count(),
        pending_artists=Artist.query.filter_by(
            account_type="artist", is_approved=False, suspended=False,
        ).count(),
        clients=Artist.query.filter_by(account_type="client").count(),
        suspended=Artist.query.filter_by(account_type="artist", suspended=True).count(),
        artworks=Artwork.query.count(),
        hidden=Artwork.query.filter_by(hidden=True).count(),
        sold=Artwork.query.filter_by(available=False).count(),
        orders=Order.query.count(),
        sales=db.session.query(db.func.coalesce(db.func.sum(Order.price), 0))
                .filter(Order.status == "completed").scalar(),
    )
    order_counts = {st: Order.query.filter_by(status=st).count()
                    for st in ("pending", "accepted", "completed", "declined")}
    return render_template(
        "admin_dashboard.html", stats=stats, order_counts=order_counts,
        recent_orders=Order.query.order_by(Order.created_at.desc()).limit(5).all(),
        new_accounts=Artist.query.order_by(Artist.created_at.desc()).limit(5).all(),
    )


@app.route("/admin/artist-applications")
@admin_required
def admin_artist_applications():
    applicants = Artist.query.filter_by(
        account_type="artist", is_approved=False, suspended=False,
    ).order_by(Artist.created_at.asc()).all()
    return render_template("admin_artist_applications.html", applicants=applicants)


@app.route("/admin/artist-applications/<int:artist_id>/<action>", methods=["POST"])
@admin_required
def admin_artist_application_action(artist_id, action):
    applicant = db.get_or_404(Artist, artist_id)
    if applicant.account_type != "artist" or applicant.is_approved:
        abort(404)
    if action == "approve":
        applicant.is_approved = True
        applicant.show_in_directory = True
        message = f"{applicant.name} has been approved as an artist."
    elif action == "reject":
        applicant.account_type = "client"
        applicant.is_approved = False
        applicant.show_in_directory = False
        message = f"{applicant.name}'s application was declined; the account is now a client account."
    else:
        abort(404)
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("admin_artist_applications"))


@app.route("/admin/artists")
@admin_required
def admin_artists():
    q = request.args.get("q", "").strip()
    query = Artist.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Artist.name.ilike(like), Artist.email.ilike(like)))
    return render_template("admin_artists.html", q=q,
                           artists=query.order_by(Artist.created_at.desc()).all())


@app.route("/admin/artists/<int:artist_id>/<action>", methods=["POST"])
@admin_required
def admin_artist_action(artist_id, action):
    artist = db.get_or_404(Artist, artist_id)
    messages = {
        "suspend": f"{artist.name} has been suspended.",
        "activate": f"{artist.name} is active again.",
        "make-admin": f"{artist.name} is now an admin.",
        "remove-admin": f"{artist.name} is no longer an admin.",
    }
    if action not in messages:
        abort(404)
    if artist.id == current_user.id and action in ("suspend", "remove-admin"):
        flash("You can’t do that to your own account.", "error")
    elif action == "suspend" and artist.is_admin:
        flash("Remove admin rights first before suspending this account.", "error")
    else:
        if action == "suspend":
            artist.suspended = True
        elif action == "activate":
            artist.suspended = False
        elif action == "make-admin":
            artist.is_admin = True
            if artist.account_type == "artist":
                artist.is_approved = True
        else:
            artist.is_admin = False
        db.session.commit()
        flash(messages[action], "success")
    return redirect(request.referrer or url_for("admin_artists"))


@app.route("/admin/artworks")
@admin_required
def admin_artworks():
    return render_template("admin_artworks.html",
                           artworks=Artwork.query.order_by(Artwork.created_at.desc()).all())


@app.route("/admin/artworks/<int:artwork_id>/<action>", methods=["POST"])
@admin_required
def admin_artwork_action(artwork_id, action):
    artwork = db.get_or_404(Artwork, artwork_id)
    if action == "hide":
        artwork.hidden = True
        flash(f"“{artwork.title}” is now hidden from the public.", "success")
    elif action == "unhide":
        artwork.hidden = False
        flash(f"“{artwork.title}” is visible again.", "success")
    elif action == "delete":
        if artwork.reserved:
            flash("This artwork has an active order. It can’t be deleted yet.", "error")
            return redirect(url_for("admin_artworks"))
        remove_image(artwork.image)
        db.session.delete(artwork)
        flash("Artwork deleted.", "success")
    else:
        abort(404)
    db.session.commit()
    return redirect(url_for("admin_artworks"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    return render_template("admin_orders.html",
                           orders=Order.query.order_by(Order.created_at.desc()).limit(200).all())


@app.errorhandler(413)
def too_large(_e):
    flash("The image is too large. Maximum size is 5 MB.", "error")
    return redirect(url_for("profile" if request.endpoint == "profile" else "dashboard"))


if __name__ == "__main__":
    app.run(debug=True)