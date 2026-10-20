import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
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
    avatar = db.Column(db.String(120))  # profile picture file in static/uploads
    # Privacy and notification preferences
    show_in_directory = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    show_member_since = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    notify_new_orders = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("1"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    works = db.relationship("Artwork", backref="artist", cascade="all, delete-orphan",
                            order_by="Artwork.created_at.desc()")

    @property
    def initials(self):
        parts = self.name.split()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def slug(self):
        return f"artist-{self.id}"


class Artwork(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    size = db.Column(db.String(60), default="")
    note = db.Column(db.String(300), default="")
    image = db.Column(db.String(120))  # filename in static/uploads
    available = db.Column(db.Boolean, default=True)
    price = db.Column(db.Integer)  # whole pesos; None = price on request
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
        return bool(self.available and self.price and not self.reserved)

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
    artwork_title = db.Column(db.String(120), nullable=False)  # snapshot at time of order
    price = db.Column(db.Integer, nullable=False)  # snapshot, whole pesos
    buyer_name = db.Column(db.String(80), nullable=False)
    buyer_email = db.Column(db.String(120), nullable=False)
    buyer_phone = db.Column(db.String(30), nullable=False)
    note = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, accepted, completed, declined
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    artwork = db.relationship("Artwork", backref="orders")
    artist = db.relationship("Artist", backref="orders")

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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Artist, int(user_id))


with app.app_context():
    db.create_all()
    # Add the "avatar" column if the database is older (no data is lost).
    cols = [r[1] for r in db.session.execute(db.text("PRAGMA table_info(artist)"))]
    for col, ddl in [
        ("avatar", "VARCHAR(120)"),
        ("show_in_directory", "BOOLEAN NOT NULL DEFAULT 1"),
        ("show_member_since", "BOOLEAN NOT NULL DEFAULT 1"),
        ("notify_new_orders", "BOOLEAN NOT NULL DEFAULT 1"),
    ]:
        if col not in cols:
            db.session.execute(db.text(f"ALTER TABLE artist ADD COLUMN {col} {ddl}"))
            db.session.commit()
    # Same for the "price" column on artworks.
    art_cols = [r[1] for r in db.session.execute(db.text("PRAGMA table_info(artwork)"))]
    if "price" not in art_cols:
        db.session.execute(db.text("ALTER TABLE artwork ADD COLUMN price INTEGER"))
        db.session.commit()


# ---------- Helpers ----------
def safe_next(target):
    """Only accept internal URLs to prevent open redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
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
        }
    return {"pending_orders": 0, "unread_notifications": 0}


@app.template_filter("ph_time")
def ph_time(dt):
    """Show a UTC timestamp in Philippine time (UTC+8)."""
    return (dt + timedelta(hours=8)).strftime("%B %d, %Y, %I:%M %p") if dt else ""


# ---------- Routes ----------
@app.route("/")
def home():
    latest = Artwork.query.order_by(Artwork.created_at.desc()).limit(5).all()
    artists = Artist.query.all()
    featured = sorted((a for a in artists if a.show_in_directory),
                      key=lambda a: len(a.works), reverse=True)[:3]
    return render_template(
        "index.html",
        hero=latest[0] if latest else None,
        recent=latest[1:],
        featured_artists=featured,
        total_works=Artwork.query.count(),
        total_artists=len(artists),
    )


@app.route("/artists")
def artists():
    return render_template("artists.html", artists=Artist.query.filter_by(show_in_directory=True).order_by(Artist.name).all())


@app.route("/artists/<int:artist_id>")
def artist_detail(artist_id):
    artist = db.get_or_404(Artist, artist_id)
    return render_template("artist.html", artist=artist)


@app.route("/artworks/<int:artwork_id>")
def artwork_detail(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    more = [w for w in artwork.artist.works if w.id != artwork.id][:3]
    return render_template("artwork.html", artwork=artwork, more=more)


@app.route("/artworks")
def artworks():
    paintings = Artwork.query.order_by(Artwork.created_at.desc()).all()
    return render_template("artworks.html", paintings=paintings)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
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
        elif Artist.query.filter(db.func.lower(Artist.email) == email).first():
            errors["email"] = "This email is already in use. Use a different email or log in."
        if len(password) < 8:
            errors["password"] = "Password must be at least 8 characters."
        elif password != form.get("confirm", ""):
            errors["confirm"] = "Passwords do not match."
        if not errors:
            artist = Artist(
                name=name, email=email,
                password_hash=generate_password_hash(password),
                focus=form.get("focus", "").strip()[:80],
                experience=form.get("experience", "").strip()[:80],
                bio=form.get("bio", "").strip()[:600],
            )
            db.session.add(artist)
            try:
                db.session.commit()
            except IntegrityError:  # two sign-ups with the same email at the same time
                db.session.rollback()
                errors["email"] = "This email is already in use. Use a different email or log in."
            else:
                login_user(artist)
                flash("Welcome! Your dashboard is ready.", "success")
                return redirect(url_for("dashboard"))
        flash("Please fix the highlighted fields.", "error")
        return render_template("register.html", form=form, errors=errors), 400
    return render_template("register.html", form=form, errors=errors)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        artist = Artist.query.filter_by(email=email).first()
        if artist and check_password_hash(artist.password_hash, request.form.get("password", "")):
            login_user(artist, remember=bool(request.form.get("remember")))
            return redirect(safe_next(request.args.get("next")))
        flash("Incorrect email or password.", "error")
        return render_template("login.html", email=email), 401
    return render_template("login.html", email="")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
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
def delete_avatar():
    if current_user.avatar:
        remove_image(current_user.avatar)
        current_user.avatar = None
        db.session.commit()
        flash("Your profile picture has been removed.", "info")
    return redirect(url_for("profile"))


@app.route("/artwork/<int:artwork_id>/price", methods=["POST"])
@login_required
def update_price(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if artwork.artist_id != current_user.id:
        abort(403)
    price, error = parse_price(request.form.get("price", ""))
    if error:
        flash(error, "error")
    else:
        artwork.price = price
        db.session.commit()
        flash("Price updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/artwork/<int:artwork_id>/delete", methods=["POST"])
@login_required
def delete_artwork(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    if artwork.artist_id != current_user.id:
        abort(403)
    if artwork.reserved:
        flash("This artwork has an active order. Accept, decline, or complete the order first.", "error")
        return redirect(url_for("dashboard"))
    remove_image(artwork.image)
    db.session.delete(artwork)
    db.session.commit()
    flash("Artwork deleted.", "info")
    return redirect(url_for("dashboard"))


@app.route("/artworks/<int:artwork_id>/buy", methods=["GET", "POST"])
def checkout(artwork_id):
    artwork = db.get_or_404(Artwork, artwork_id)
    detail = redirect(url_for("artwork_detail", artwork_id=artwork.id))
    if current_user.is_authenticated and current_user.id == artwork.artist_id:
        flash("You can’t buy your own artwork.", "info")
        return detail
    if not artwork.can_buy:
        flash("This artwork is not available for purchase right now.", "info")
        return detail

    form, errors = request.form, {}
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
def track_order():
    """Let a buyer find their order again with the order number and email."""
    form = request.form
    if request.method == "POST":
        number = form.get("order_number", "").strip().lstrip("#")
        email = form.get("email", "").strip().lower()
        order = None
        if number.isdigit() and len(number) <= 9 and email:
            order = Order.query.filter_by(id=int(number), buyer_email=email).first()
        if order:
            return redirect(url_for("order_status", token=order.token))
        flash("We couldn’t find an order with that number and email.", "error")
        return render_template("track.html", form=form), 400
    return render_template("track.html", form=form)


@app.route("/order/<token>")
def order_status(token):
    order = Order.query.filter_by(token=token).first_or_404()
    return render_template("order_status.html", order=order)


@app.route("/orders")
@login_required
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
def notification_preferences():
    current_user.notify_new_orders = bool(request.form.get("notify_new_orders"))
    db.session.commit()
    flash("Your notification preferences have been saved.", "success")
    return redirect(url_for("notifications"))


@app.route("/notifications")
@login_required
def notifications():
    items = (Notification.query.filter_by(artist_id=current_user.id)
             .order_by(Notification.created_at.desc()).limit(50).all())
    unread_ids = {n.id for n in items if not n.is_read}
    if unread_ids:  # opening the page marks them as read
        Notification.query.filter(Notification.id.in_(unread_ids)).update(
            {"is_read": True}, synchronize_session=False)
        db.session.commit()
    return render_template("notifiations.html", items=items, unread_ids=unread_ids)


@app.route("/notifications/clear", methods=["POST"])
@login_required
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


@app.errorhandler(413)
def too_large(_e):
    flash("The image is too large. Maximum size is 5 MB.", "error")
    return redirect(url_for("profile" if request.endpoint == "profile" else "dashboard"))


if __name__ == "__main__":
    app.run(debug=True)