from flask import Flask, request, jsonify, send_file, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from datetime import datetime, date
import os, string, random, subprocess, uuid

# ---------------- LOAD ENV ----------------
load_dotenv()

app = Flask(__name__, static_folder="static")

# ---------------- CONFIG ----------------
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URI", "sqlite:///toolifyx.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------------- MODELS ----------------

class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(500), nullable=False)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    clicks = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Compression(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------------- HELPERS ----------------

def generate_short_code(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        code = "".join(random.choice(chars) for _ in range(length))
        if not URL.query.filter_by(short_code=code).first():
            return code


# ---------------- HOME ----------------

@app.route("/")
def home():
    return "ToolifyX backend running"


# ---------------- URL SHORTENER ----------------

@app.route("/api/shorten", methods=["POST"])
def shorten():
    data = request.get_json()
    long_url = data.get("url", "").strip()

    if not long_url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400

    existing = URL.query.filter_by(original_url=long_url).first()
    if existing:
        short = request.host_url + existing.short_code
    else:
        code = generate_short_code()
        new = URL(original_url=long_url, short_code=code)
        db.session.add(new)
        db.session.commit()
        short = request.host_url + code

    return jsonify({"short_url": short})


@app.route("/<string:code>")
def redirect_short(code):
    url = URL.query.filter_by(short_code=code).first_or_404()
    url.clicks += 1
    db.session.commit()
    return redirect(url.original_url)


# ---------------- VIDEO COMPRESSOR ----------------

@app.route("/api/compress", methods=["POST"])
def compress_video():
    if "video" not in request.files:
        return jsonify({"error": "No video uploaded"}), 400

    file = request.files["video"]
    level = request.form.get("level", "medium")

    crf_map = {
        "low": "18",
        "medium": "23",
        "high": "28"
    }

    crf = crf_map.get(level, "23")

    input_path = f"/tmp/{uuid.uuid4().hex}_{file.filename}"
    output_name = f"toolifyx_{uuid.uuid4().hex}.mp4"
    output_path = f"/tmp/{output_name}"

    file.save(input_path)

    # ⚡ FAST FFmpeg SETTINGS
    command = [
        "ffmpeg",
        "-i", input_path,
        "-map_metadata", "-1",
        "-movflags", "faststart",
        "-preset", "ultrafast",     # SPEED BOOST
        "-threads", "2",            # Render-safe
        "-crf", crf,
        "-vcodec", "libx264",
        "-acodec", "aac",
        "-y",
        output_path
    ]

    try:
        subprocess.run(command, check=True)

        # save stats
        db.session.add(Compression(filename=output_name))
        db.session.commit()

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=output_name
        )

        @response.call_on_close
        def cleanup():
            if os.path.exists(input_path):
                os.remove(input_path)
            if os.path.exists(output_path):
                os.remove(output_path)

        return response

    except subprocess.CalledProcessError:
        return jsonify({"error": "Compression failed"}), 500


# ---------------- ADMIN STATS API ----------------

@app.route("/admin/stats")
def admin_stats():
    total_links = URL.query.count()
    total_clicks = db.session.query(db.func.sum(URL.clicks)).scalar() or 0
    total_compressions = Compression.query.count()

    today = date.today()
    today_compressions = Compression.query.filter(
        db.func.date(Compression.created_at) == today
    ).count()

    return jsonify({
        "total_links": total_links,
        "total_clicks": total_clicks,
        "total_compressions": total_compressions,
        "today_compressions": today_compressions
    })


@app.route("/admin/daily-compressions")
def daily_compressions():
    data = (
        db.session.query(
            db.func.date(Compression.created_at),
            db.func.count(Compression.id)
        )
        .group_by(db.func.date(Compression.created_at))
        .all()
    )

    return jsonify([
        {"date": str(d[0]), "count": d[1]} for d in data
    ])


# ---------------- MAIN ----------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))