import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "subsentry.db"

CATEGORY_KEYWORDS = {
    "Streaming": ["netflix", "hulu", "spotify", "disney", "prime video", "hbo", "max"],
    "SaaS": ["github", "notion", "slack", "figma", "dropbox", "atlassian"],
    "Utilities": ["comcast", "verizon", "att", "internet", "xfinity"],
    "Fitness": ["peloton", "strava", "gym", "fitness", "classpass"],
    "News": ["nytimes", "economist", "substack", "news"],
    "Cloud": ["aws", "google cloud", "azure", "digitalocean"],
    "Security": ["1password", "lastpass", "nordvpn", "dashlane"],
}

PLATFORM_KEYWORDS = {
    "Apple": ["apple", "icloud", "itunes", "app store"],
    "Google": ["google", "gplay", "play store", "youtube"],
    "PayPal": ["paypal"],
}

CANCELLATION_GUIDES = {
    "netflix": (
        "Netflix",
        "Go to netflix.com/youraccount and select Cancel Membership. Your plan ends at the next billing date.",
        "https://www.netflix.com/youraccount",
    ),
    "spotify": (
        "Spotify",
        "Visit spotify.com/account, choose Your Plan, and select Cancel Premium.",
        "https://www.spotify.com/account",
    ),
    "hulu": (
        "Hulu",
        "Open your Hulu Account page and toggle off subscriptions in Manage Plan.",
        "https://secure.hulu.com/account",
    ),
    "dropbox": (
        "Dropbox",
        "Go to dropbox.com/account/plan and select Cancel Plan.",
        "https://www.dropbox.com/account/plan",
    ),
    "notion": (
        "Notion",
        "Visit notion.so/teams/your-workspace/settings/billing and downgrade to Free.",
        "https://www.notion.so/",
    ),
}

KNOWN_FREQUENCY = {
    "netflix": "Monthly",
    "spotify": "Monthly",
    "hulu": "Monthly",
    "dropbox": "Monthly",
    "notion": "Monthly",
}


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "subsentry-dev-key"
    app.config["DATABASE"] = DB_PATH

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    @app.before_request
    def load_user():
        g.user = None
        if "user_id" in session:
            g.user = query_db("SELECT * FROM users WHERE id = ?", (session["user_id"],), one=True)

    @app.route("/")
    def index():
        if g.user:
            return redirect(url_for("dashboard"))
        return render_template("index.html")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            plan = request.form.get("plan", "free")

            if not email or not password:
                flash("Email and password are required.")
                return redirect(url_for("signup"))

            existing = query_db("SELECT id FROM users WHERE email = ?", (email,), one=True)
            if existing:
                flash("An account with that email already exists.")
                return redirect(url_for("signup"))

            password_hash = generate_password_hash(password)
            query_db(
                "INSERT INTO users (email, password_hash, plan, created_at) VALUES (?, ?, ?, ?)",
                (email, password_hash, plan, datetime.utcnow().isoformat()),
                commit=True,
            )
            flash("Account created. Please log in.")
            return redirect(url_for("login"))

        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = query_db("SELECT * FROM users WHERE email = ?", (email,), one=True)
            if not user or not check_password_hash(user["password_hash"], password):
                flash("Invalid credentials.")
                return redirect(url_for("login"))
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/dashboard")
    def dashboard():
        response = require_login()
        if response:
            return response
        subs = query_db("SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC", (g.user["id"],))
        totals = calculate_totals(subs)
        duplicate_groups = find_duplicates(subs)
        return render_template(
            "dashboard.html",
            subscriptions=subs,
            totals=totals,
            duplicate_groups=duplicate_groups,
            plan=g.user["plan"],
        )

    @app.route("/add")
    def add_subscription():
        response = require_login()
        if response:
            return response
        return render_template("add.html", plan=g.user["plan"])

    @app.route("/add/manual", methods=["POST"])
    def add_manual():
        response = require_login()
        if response:
            return response
        if not can_add_subscription():
            flash("Free plan limit reached. Upgrade to add more subscriptions.")
            return redirect(url_for("dashboard"))

        merchant = request.form.get("merchant_name", "").strip()
        amount = request.form.get("amount", "0").strip()
        frequency = request.form.get("frequency", "Unknown")
        platform = request.form.get("platform", "Unknown")
        category = request.form.get("category", "Uncategorized")
        status = request.form.get("status", "review")
        notes = request.form.get("notes", "").strip()

        if not merchant or not amount:
            flash("Merchant name and amount are required.")
            return redirect(url_for("add_subscription"))

        query_db(
            """
            INSERT INTO subscriptions
            (user_id, merchant_name, amount, frequency, platform, category, status, notes, created_at, raw_line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.user["id"],
                merchant,
                float(amount),
                frequency,
                platform,
                category,
                status,
                notes,
                datetime.utcnow().isoformat(),
                "",
            ),
            commit=True,
        )
        flash("Subscription added.")
        return redirect(url_for("dashboard"))

    @app.route("/add/paste", methods=["POST"])
    def add_paste():
        response = require_login()
        if response:
            return response
        if g.user["plan"] == "free":
            flash("Paste import is a Pro feature. Upgrade to use AI parsing.")
            return redirect(url_for("add_subscription"))

        paste_text = request.form.get("paste_text", "").strip()
        if not paste_text:
            flash("Paste at least one transaction line.")
            return redirect(url_for("add_subscription"))

        entries = parse_paste(paste_text)
        added = 0
        for entry in entries:
            if not can_add_subscription():
                flash("Free plan limit reached. Upgrade to add more subscriptions.")
                break
            query_db(
                """
                INSERT INTO subscriptions
                (user_id, merchant_name, amount, frequency, platform, category, status, notes, created_at, raw_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.user["id"],
                    entry["merchant"],
                    entry["amount"],
                    entry["frequency"],
                    entry["platform"],
                    entry["category"],
                    entry["status"],
                    "",
                    datetime.utcnow().isoformat(),
                    entry["raw_line"],
                ),
                commit=True,
            )
            added += 1

        flash(f"Imported {added} line items.")
        return redirect(url_for("dashboard"))

    @app.route("/subscription/<int:sub_id>")
    def view_subscription(sub_id):
        response = require_login()
        if response:
            return response
        sub = query_db(
            "SELECT * FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, g.user["id"]),
            one=True,
        )
        if not sub:
            flash("Subscription not found.")
            return redirect(url_for("dashboard"))
        guide = lookup_cancellation(sub["merchant_name"])
        return render_template("subscription.html", subscription=sub, guide=guide)

    @app.route("/subscription/<int:sub_id>/edit", methods=["GET", "POST"])
    def edit_subscription(sub_id):
        response = require_login()
        if response:
            return response
        sub = query_db(
            "SELECT * FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, g.user["id"]),
            one=True,
        )
        if not sub:
            flash("Subscription not found.")
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            merchant = request.form.get("merchant_name", "").strip()
            amount = request.form.get("amount", "0").strip()
            frequency = request.form.get("frequency", "Unknown")
            platform = request.form.get("platform", "Unknown")
            category = request.form.get("category", "Uncategorized")
            status = request.form.get("status", "review")
            notes = request.form.get("notes", "").strip()

            query_db(
                """
                UPDATE subscriptions
                SET merchant_name = ?, amount = ?, frequency = ?, platform = ?, category = ?, status = ?, notes = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    merchant,
                    float(amount),
                    frequency,
                    platform,
                    category,
                    status,
                    notes,
                    sub_id,
                    g.user["id"],
                ),
                commit=True,
            )
            flash("Subscription updated.")
            return redirect(url_for("view_subscription", sub_id=sub_id))

        return render_template("edit.html", subscription=sub)

    @app.route("/subscription/<int:sub_id>/delete", methods=["POST"])
    def delete_subscription(sub_id):
        response = require_login()
        if response:
            return response
        query_db(
            "DELETE FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, g.user["id"]),
            commit=True,
        )
        flash("Subscription removed.")
        return redirect(url_for("dashboard"))

    @app.route("/upgrade", methods=["GET", "POST"])
    def upgrade():
        response = require_login()
        if response:
            return response
        if request.method == "POST":
            plan = request.form.get("plan", "free")
            query_db(
                "UPDATE users SET plan = ? WHERE id = ?",
                (plan, g.user["id"]),
                commit=True,
            )
            flash("Plan updated.")
            return redirect(url_for("dashboard"))
        return render_template("upgrade.html", plan=g.user["plan"])

    @app.route("/export")
    def export_csv():
        response = require_login()
        if response:
            return response
        subs = query_db("SELECT * FROM subscriptions WHERE user_id = ?", (g.user["id"],))
        export_path = INSTANCE_DIR / f"subsentry_export_{g.user['id']}.csv"
        with export_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "Merchant",
                    "Amount",
                    "Frequency",
                    "Platform",
                    "Category",
                    "Status",
                    "Notes",
                    "Created At",
                ]
            )
            for sub in subs:
                writer.writerow(
                    [
                        sub["merchant_name"],
                        sub["amount"],
                        sub["frequency"],
                        sub["platform"],
                        sub["category"],
                        sub["status"],
                        sub["notes"],
                        sub["created_at"],
                    ]
                )
        return send_file(export_path, as_attachment=True)

    return app


def init_db():
    if DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                merchant_name TEXT NOT NULL,
                amount REAL NOT NULL,
                frequency TEXT NOT NULL,
                platform TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                raw_line TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def query_db(query, args=(), one=False, commit=False):
    db = get_db()
    cursor = db.execute(query, args)
    if commit:
        db.commit()
    results = cursor.fetchall()
    cursor.close()
    if one:
        return results[0] if results else None
    return results


def require_login():
    if not g.user:
        return redirect(url_for("login"))


def can_add_subscription():
    if not g.user:
        return False
    if g.user["plan"] != "free":
        return True
    count = query_db("SELECT COUNT(*) AS total FROM subscriptions WHERE user_id = ?", (g.user["id"],), one=True)
    return count["total"] < 10


def parse_paste(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries = []
    for line in lines:
        amount_match = re.search(r"(-?\d+[\.,]?\d*)", line)
        if not amount_match:
            continue
        amount = float(amount_match.group(1).replace(",", ""))
        merchant = re.sub(r"\s*-?\s*\$?\d+[\.,]?\d*", "", line, count=1).strip()
        if not merchant:
            merchant = "Unknown Merchant"
        classification = classify_entry(merchant, amount, line)
        entries.append(
            {
                "merchant": merchant,
                "amount": amount,
                "frequency": classification["frequency"],
                "platform": classification["platform"],
                "category": classification["category"],
                "status": classification["status"],
                "raw_line": line,
            }
        )
    return entries


def classify_entry(merchant, amount, raw_line):
    merchant_lower = merchant.lower()
    category = "Uncategorized"
    platform = "Unknown"
    frequency = "Unknown"
    status = "review"

    for category_name, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in merchant_lower for keyword in keywords):
            category = category_name
            status = "keep"
            break

    for platform_name, keywords in PLATFORM_KEYWORDS.items():
        if any(keyword in merchant_lower for keyword in keywords):
            platform = platform_name
            break

    if merchant_lower in KNOWN_FREQUENCY:
        frequency = KNOWN_FREQUENCY[merchant_lower]
    elif re.search(r"monthly|month", raw_line, re.IGNORECASE):
        frequency = "Monthly"
    elif re.search(r"annual|year", raw_line, re.IGNORECASE):
        frequency = "Yearly"
    elif amount >= 100:
        frequency = "Yearly"
    else:
        frequency = "Monthly"

    if category == "Uncategorized" and amount > 200:
        status = "review"

    return {
        "category": category,
        "platform": platform,
        "frequency": frequency,
        "status": status,
    }


def calculate_totals(subs):
    monthly = 0
    yearly = 0
    for sub in subs:
        if sub["frequency"].lower().startswith("year"):
            yearly += sub["amount"]
            monthly += sub["amount"] / 12
        else:
            monthly += sub["amount"]
            yearly += sub["amount"] * 12
    return {
        "monthly": monthly,
        "yearly": yearly,
        "savings": sum(sub["amount"] for sub in subs if sub["status"] == "cancel"),
    }


def find_duplicates(subs):
    seen = {}
    duplicates = {}
    for sub in subs:
        key = (sub["merchant_name"].lower(), sub["amount"])
        if key in seen:
            duplicates.setdefault(key, []).append(sub)
            duplicates[key].append(seen[key])
        else:
            seen[key] = sub
    return duplicates


def lookup_cancellation(merchant_name):
    merchant_lower = merchant_name.lower()
    for key, value in CANCELLATION_GUIDES.items():
        if key in merchant_lower:
            return value
    return (
        merchant_name,
        "Check the service's billing page or app settings to locate the cancellation option. If billed via Apple, Google, or PayPal, cancel inside that platform.",
        "",
    )


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
