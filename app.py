from flask import Flask
from flask import render_template
from flask import request
from models.bin_status import BinStatus

import os
from flask_socketio import SocketIO
import uuid

from flask import jsonify
from werkzeug.utils import secure_filename
from datetime import datetime
from datetime import timedelta
import calendar

import pytz

from config import Config

from database.db import db
from flask import redirect
from flask import url_for


# ==================================================
# APP
# ==================================================

app = Flask(__name__)

app.config.from_object(Config)

UPLOAD_FOLDER = os.path.join(
    app.root_path,
    "static",
    "uploads",
    "history"
)

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)

# ==================================================
# DATABASE
# ==================================================

db.init_app(app)

# ==================================================
# SOCKET
# ==================================================

socketio = SocketIO(app, async_mode="gevent")

# ==================================================
# TIMEZONE
# ==================================================

timezone = pytz.timezone('Asia/Jakarta')

devices = [
    {
        "id": 1,
        "name": "Sensor Raspberry Pi",
        "status": "ON"
    },
    {
        "id": 2,
        "name": "Sensor Kamera",
        "status": "ON"
    },
    {
        "id": 3,
        "name": "Sensor Ultrasonik Non-Medis",
        "status": "ON"
    },
    {
        "id": 4,
        "name": "Sensor Load Cell Medis",
        "status": "ON"
    },
    {
        "id": 5,
        "name": "Sensor Load Cell Non-Medis",
        "status": "OFF"
    },
    {
        "id": 6,
        "name": "Sensor Ultrasonik Medis",
        "status": "ERROR"
    }
]

# ==================================================
# CREATE TABLE
# ==================================================

from models.sampah import Sampah
from models.trash_log import TrashLog
from models.device_log import DeviceLog
from models.history_log import HistoryLog
from models.pengambilan_sampah import PengambilanSampah

with app.app_context():
    db.create_all()

    # Periksa dan inisialisasi tabel BinStatus jika kosong
    if BinStatus.query.count() == 0:
        db.session.add(BinStatus(kategori='Medical', current_volume=0, is_full=False))
        db.session.add(BinStatus(kategori='Non Medical', current_volume=0, is_full=False))
        db.session.commit()

# ==================================================
# INDEX
# ==================================================

@app.route('/')
def index():
    return redirect(url_for("dashboard"))

# ==================================================
# DASHBOARD
# ==================================================

@app.route('/dashboard')
def dashboard():
    
    total_sampah = TrashLog.query.count()

    total_berat_medis = db.session.query(
        db.func.sum(TrashLog.berat)
    ).filter_by(
        kategori='Medical'
    ).scalar() or 0

    total_berat_non_medis = db.session.query(
        db.func.sum(TrashLog.berat)
    ).filter_by(
        kategori='Non Medical'
    ).scalar() or 0

    logs = TrashLog.query.order_by(
        TrashLog.timestamp.desc()
    ).limit(10).all()
    
    # Ambil status tempat sampah
    medical_bin = BinStatus.query.filter_by(kategori='Medical').first()
    non_medical_bin = BinStatus.query.filter_by(kategori='Non Medical').first()

    return render_template(
        "dashboard/index.html",
        total_sampah=total_sampah,
        total_berat_medis=round(total_berat_medis, 2),
        total_berat_non_medis=round(total_berat_non_medis, 2),
        logs=logs,
        medical_bin=medical_bin,
        non_medical_bin=non_medical_bin
    )

@app.route('/history/pengambilan', methods=['POST'])
def get_pengambilan_history():
    kategori = request.json.get('kategori', 'Medical')
    
    # Dapatkan waktu 24 jam yang lalu
    yesterday = datetime.utcnow() - timedelta(days=1)
    
    pengambilan_logs = PengambilanSampah.query.filter(
        PengambilanSampah.kategori == kategori,
        PengambilanSampah.timestamp >= yesterday
    ).order_by(PengambilanSampah.timestamp.desc()).all()

    return jsonify({
        "success": True,
        "logs": [{
            "timestamp": log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            "berat_sebelumnya": log.berat_sebelumnya,
            "petugas": log.petugas
        } for log in pengambilan_logs]
    })


# ==================================================
# TRASH
# ==================================================

def is_allowed_file(filename):
    """
    Fungsi untuk memeriksa apakah file yang diupload
    memiliki ekstensi yang diizinkan (.jpg, .jpeg, .png).
    """
    
    ALLOWED_EXTENSIONS = {
        'png',
        'jpg',
        'jpeg'
    }

    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/trash', methods=['POST'])
def add_trash():
    """
    Menerima data dari Raspberry Pi saat sampah dimasukkan.
    Data yang diterima:
    - gambar (file)
    - kategori (Medical / Non Medical)
    - jenis_sampah (Syringe, Cardboard, dll)
    - confidence (0.0 - 1.0)
    - berat (float)
    """

    try:

        if 'image' not in request.files:
            
            return jsonify({
                "success": False,
                "message": "Tidak ada file gambar"
            }), 400

        file = request.files['image']
        
        if file.filename == '':
            
            return jsonify({
                "success": False,
                "message": "Nama file kosong"
            }), 400

        kategori = request.form.get('kategori')
        jenis_sampah = request.form.get('jenis_sampah')
        confidence = request.form.get('confidence')
        berat = request.form.get('berat', type=float, default=0.0)

        if not all([kategori, jenis_sampah, confidence]):
            return jsonify({
                "success": False,
                "message": "Data tidak lengkap"
            }), 400

        if file and is_allowed_file(file.filename):

            unique_filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
            filepath = os.path.join(UPLOAD_FOLDER, unique_filename)

            file.save(filepath)

            # Tambahkan logika untuk memperbarui BinStatus
            bin_status = BinStatus.query.filter_by(kategori=kategori).first()
            if bin_status:
                bin_status.current_volume += berat
                if bin_status.current_volume >= bin_status.max_capacity:
                    bin_status.is_full = True
                    # Opsional: Kirim notifikasi via socketio
                    socketio.emit("bin_full", {
                        "kategori": kategori,
                        "message": f"Tempat sampah {kategori} penuh!"
                    })
                db.session.commit()

            new_trash = TrashLog(
                kategori=kategori,
                jenis_sampah=jenis_sampah,
                confidence=float(confidence),
                image_path=f"uploads/history/{unique_filename}",
                berat=berat
            )

            db.session.add(new_trash)
            db.session.commit()

            socketio.emit("new_trash", {
                "id": new_trash.id,
                "kategori": new_trash.kategori,
                "jenis_sampah": new_trash.jenis_sampah,
                "confidence": new_trash.confidence,
                "image_path": new_trash.image_path,
                "berat": new_trash.berat,
                "timestamp": new_trash.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            })

            return jsonify({
                "success": True,
                "message": "Data berhasil disimpan"
            }), 201

        return jsonify({
            "success": False,
            "message": "Tipe file tidak diizinkan"
        }), 400

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

@app.route('/api/empty_bin', methods=['POST'])
def empty_bin():
    kategori = request.json.get('kategori')
    if not kategori:
        return jsonify({"success": False, "message": "Kategori tidak ditentukan"}), 400

    bin_status = BinStatus.query.filter_by(kategori=kategori).first()
    if bin_status:
        berat_sebelumnya = bin_status.current_volume
        bin_status.current_volume = 0
        bin_status.is_full = False
        
        # Tambahkan log pengambilan sampah
        pengambilan = PengambilanSampah(
            kategori=kategori,
            berat_sebelumnya=berat_sebelumnya,
            petugas="Admin" # Bisa disesuaikan dengan sistem login nanti
        )
        db.session.add(pengambilan)
        db.session.commit()
        
        socketio.emit("bin_emptied", {"kategori": kategori})
        return jsonify({"success": True})
        
    return jsonify({"success": False, "message": "Kategori tidak ditemukan"}), 404

# ==================================================
# ANALYTICS
# ==================================================

@app.route('/analytics')
def analytics():
    
    # Ambil data statistik dari database (contoh: 7 hari terakhir)
    today = datetime.utcnow()
    seven_days_ago = today - timedelta(days=7)

    # 1. Total sampah per kategori dalam 7 hari
    daily_stats = db.session.query(
        db.func.date(TrashLog.timestamp).label('date'),
        TrashLog.kategori,
        db.func.count(TrashLog.id).label('count')
    ).filter(
        TrashLog.timestamp >= seven_days_ago
    ).group_by(
        db.func.date(TrashLog.timestamp),
        TrashLog.kategori
    ).all()

    # 2. Distribusi jenis sampah (Medical vs Non-Medical)
    category_distribution = db.session.query(
        TrashLog.kategori,
        db.func.count(TrashLog.id).label('count')
    ).group_by(
        TrashLog.kategori
    ).all()

    # 3. Akurasi rata-rata per jenis sampah
    accuracy_stats = db.session.query(
        TrashLog.jenis_sampah,
        db.func.avg(TrashLog.confidence).label('avg_confidence')
    ).group_by(
        TrashLog.jenis_sampah
    ).all()

    # 4. Ringkasan hari ini
    today_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    today_stats = db.session.query(
        db.func.count(TrashLog.id).label('total'),
        db.func.sum(TrashLog.berat).label('total_berat')
    ).filter(
        TrashLog.timestamp >= today_start
    ).first()

    # Ambil 5 log terakhir untuk aktivitas terbaru
    latest_logs = TrashLog.query.order_by(
        TrashLog.timestamp.desc()
    ).limit(5).all()

    return render_template(
        "analytics/index.html",
        daily_stats=daily_stats,
        category_distribution=category_distribution,
        accuracy_stats=accuracy_stats,
        today_stats=today_stats,
        latest_logs=latest_logs
    )

# ==================================================
# DEVICES
# ==================================================

@app.route('/seed')
def seed_devices():
    db.session.query(DeviceLog).delete()
    
    devices_data = [
        {"nama_perangkat": "Raspberry Pi", "status": "OFF"},
        {"nama_perangkat": "Camera", "status": "OFF"},
        {"nama_perangkat": "Ultrasonic Medis", "status": "OFF"},
        {"nama_perangkat": "Ultrasonic Non Medis", "status
