from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os, string, random, validators
from datetime import datetime

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URI', 'sqlite:///urls.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------- MODELS ----------------
class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(500), nullable=False)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks = db.Column(db.Integer, default=0)

# ---------------- HELPERS ----------------
def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if not URL.query.filter_by(short_code=code).first():
            return code

# ---------------- ROUTES ----------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        original_url = request.form.get("url").strip()
        domain_choice = request.form.get("domain")

        if not validators.url(original_url):
            return render_template("index.html", error="Enter a valid URL.")

        if domain_choice == "shrinkme":
            # User chooses ShrinkMe
            shrinkme_ref = os.getenv("SHRINKME_REF", "")
            short_url = f"https://shrinkme.io/?r={shrinkme_ref}&u={original_url}"
            return render_template("index.html", short_url=short_url, original_url=original_url)

        # Use our own shortener
        existing = URL.query.filter_by(original_url=original_url).first()
        if existing:
            short_url = request.host_url + existing.short_code
        else:
            code = generate_short_code()
            new_url = URL(original_url=original_url, short_code=code)
            db.session.add(new_url)
            db.session.commit()
            short_url = request.host_url + code

        return render_template("index.html", short_url=short_url, original_url=original_url)

    return render_template("index.html")

@app.route("/<string:short_code>")
def redirect_short(short_code):
    url = URL.query.filter_by(short_code=short_code).first_or_404()
    url.clicks += 1
    db.session.commit()
    return redirect(url.original_url)

@app.route("/api/stats/<string:short_code>")
def stats(short_code):
    url = URL.query.filter_by(short_code=short_code).first_or_404()
    return jsonify({
        "original_url": url.original_url,
        "short_code": url.short_code,
        "clicks": url.clicks,
        "created_at": url.created_at.isoformat()
    })

# ---------------- MAIN ----------------
if __name__ == "__main__":
    app.run (debug=True)
