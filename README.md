# 💬 Chatting App
Aplikasi Chat Realtime Fullstack berbasis Web, Portable dan Tanpa Instalasi.

Dibangun dengan Python Flask + SocketIO, single file aplikasi, dapat berjalan di Windows, Linux dan Mac.

---

## ✨ Fitur

### 🎯 Fitur Inti
✅ **Chat Langsung (Direct Message)** antar user secara realtime
✅ **Grup Chat** dengan admin/owner dan anggota
✅ **Realtime WebSocket** dengan SocketIO (tanpa refresh halaman)
✅ **Read Receipts** indikator pesan terkirim, diterima dan dibaca
✅ **Online Status** melihat user sedang aktif
✅ **Last Seen** melihat waktu terakhir user online
✅ **Typing Indicator** melihat lawan bicara sedang mengetik
✅ **Indikator Panggilan** melihat user sedang dalam panggilan

### 💬 Fitur Pesan
✅ Balas pesan (Reply)
✅ Teruskan pesan (Forward)
✅ Edit pesan yang sudah dikirim
✅ Hapus pesan
✅ Reaksi pesan dengan Emoji
✅ Pesan Berbintang / Bookmark
✅ Sebut / Tag user di grup (`@mention`)
✅ Pin pesan penting di grup
✅ Pin percakapan di daftar chat
✅ Pesan menghilang otomatis (Disappearing Messages)
✅ Pesan terjadwal (Scheduled Messages)
✅ Template Quick Reply / Pesan Cepat

### 📎 Media & File
✅ Upload file hingga **30 MB**
✅ Support semua format file:
  - 🖼️ Gambar: `png, jpg, jpeg, gif, webp`
  - 📄 Dokumen: `pdf, doc, docx, xls, xlsx, ppt, pptx, txt`
  - 🎵 Media: `mp3, wav, mp4, mov, avi, mkv, webm`
  - 📦 Arsip: `zip, rar, 7z, tar, gz`
  - 💻 Script: `bat, sh, py, js, html, css`
✅ Avatar Profile dengan auto crop & resize otomatis 200x200px
✅ Sticker Pack terintegrasi
✅ Cache file upload 24 jam untuk performa

### 📊 Fitur Lanjutan
✅ **Voting Polling** di grup chat
✅ **Terjemahan Pesan** dengan cache
✅ **Statistik Chat** per percakapan
✅ **Sistem Kontak** dengan Friend Request
✅ **Blokir Kontak**
✅ **History Chat Persisten** di database SQLite
✅ Pencarian user secara realtime
✅ Auto migrasi database otomatis

### 🔐 Keamanan & Autentikasi
✅ Registrasi & Login dengan password hash (`werkzeug`)
✅ Reset Password dengan token expiry
✅ Ubah profil, ganti password
✅ Hapus akun secara permanen
✅ Auto redirect HTTP ke HTTPS jika SSL tersedia
✅ CORS diijinkan untuk akses jaringan lokal

### 🚀 Deployment
✅ **Portable EXE Build** - bisa dijalankan tanpa instalasi Python
✅ System Tray Icon
✅ Halaman info jaringan untuk akses dari perangkat lain
✅ Optimasi index database untuk performa tinggi
✅ Single File Aplikasi

---

## 🚀 Cara Menjalankan

### 1. Jalankan Langsung (Python)
```bash
# Install dependensi
pip install flask flask-socketio pillow pystray python-socketio

# Jalankan aplikasi
python chatting.py
```

Aplikasi akan berjalan di `http://localhost:8080`

### 2. Jalankan Portable EXE
```bash
# Jalankan file EXE langsung
start_chat_portable.bat
```

### 3. Akses Dari Perangkat Lain
Buka halaman `http://localhost:8080/info` untuk melihat alamat IP jaringan lokal anda, device lain di jaringan yang sama dapat mengakses aplikasi.

---

## 📦 Build Portable EXE
```bash
# Install PyInstaller
pip install pyinstaller

# Jalankan build script
build_portable_chat.bat
```

File executable akan dihasilkan di folder `portable_build/`

---

## 🛠️ Teknologi yang Digunakan
| Komponen | Teknologi |
|---|---|
| Backend | Python Flask |
| Realtime | Socket.IO |
| Database | SQLite 3 |
| Image Processing | Pillow (PIL) |
| System Tray | pystray |
| Password Hash | werkzeug.security |
| Build | PyInstaller |

---

## 📁 Struktur Database
- `users` - Data user profile
- `groups` - Data grup chat
- `group_members` - Anggota grup
- `messages` - Semua pesan chat
- `message_status` - Status pesan terkirim/dibaca
- `reactions` - Reaksi emoji pesan
- `stars` - Pesan berbintang
- `polls` & `poll_votes` - Sistem voting
- `scheduled_messages` - Pesan terjadwal
- `blocked_contacts` - Kontak diblokir
- `chat_statistics` - Statistik chat
- `message_translations` - Cache terjemahan

---

## ⚙️ Konfigurasi
Semua konfigurasi ada di bagian atas file `chatting.py`:
```python
# Maksimal ukuran file upload
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 30 MB

# Secret Key aplikasi
app.secret_key = os.environ.get("APP_SECRET", "super-secret-key-change-me")
```

> ⚠️ **Penting**: Ubah `app.secret_key` untuk penggunaan produksi!

---

## 📋 Persyaratan Sistem
- Python 3.8+ (untuk menjalankan source)
- Windows 10 / 11, Linux, atau MacOS
- Minimal 512MB RAM
- Koneksi jaringan LAN untuk multi user

---

## 🤝 Kontribusi
Pull request sangat diterima. Untuk perubahan besar, silahkan buka issue terlebih dahulu untuk mendiskusikan apa yang ingin anda ubah.

---

## 📝 Lisensi
Proyek ini menggunakan lisensi MIT - lihat file LICENSE untuk detail.

---

<p align="center">
Dibuat dengan ❤️ untuk penggunaan internal perusahaan
</p>
