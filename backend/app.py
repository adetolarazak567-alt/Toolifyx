from flask import Flask, request, jsonify, redirect
from flask_sqlalchemy import SQLAlchemy
import os
import string
import random

# ---------------- FLASK APP ----------------
app = Flask(__name__)

# ---------------- CONFIG ----------------
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URI", "sqlite:///urls.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

SHRINKME_API_KEY = os.getenv("SHRINKME_API_KEY", "your_ref_code_here")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")  # used for generating short links

db = SQLAlchemy(app)

# ---------------- DATABASE MODELS ----------------
class ShortURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    short = db.Column(db.String(10), unique=True, nullable=False)
    long = db.Column(db.Text, nullable=False)

# ---------------- HELPERS ----------------
def generate_short_code(length=6):
    characters = string.ascii_letters + string.digits
    while True:
        code = ''.join(random.choices(characters, k=length))
        if not ShortURL.query.filter_by(short=code).first():
            return code

# ---------------- ROUTES ----------------
@app.route("/")
def home():
    return "URL Shortener API is running."

@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.json
    long_url = data.get("url")
    use_shrinkme = data.get("shrinkme", False)  # optional: choose ShrinkMe

    if not long_url:
        return jsonify({"success": False, "message": "No URL provided"}), 400

    if use_shrinkme:
        # If using ShrinkMe, return the referral link
        short_url = f"https://shrinkme.io/?r={SHRINKME_API_KEY}&url={long_url}"
        return jsonify({"success": True, "short_url": short_url})

    # Generate short code for our service
    code = generate_short_code()
    new_url = ShortURL(short=code, long=long_url)
    db.session.add(new_url)
    db.session.commit()

    short_url = f"{BASE_URL}/{code}"
    return jsonify({"success": True, "short_url": short_url})

@app.route("/<string:code>")
def redirect_short(code):
    url_entry = ShortURL.query.filter_by(short=code).first()
    if url_entry:
        return redirect(url_entry.long)
    return "URL not found", 404

# ---------------- INIT DATABASE ----------------
def init_db():
    with app.app_context():
        db.create_all()

init_db()

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000) 