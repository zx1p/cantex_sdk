# Cantex Trading Bot

Bot trading multi-strategi otomatis untuk bursa terdesentralisasi [Cantex](https://cantex.io). Kredensial dibaca dari file `.env` dan parameter strategi dari `config.json`.

> **Catatan fork:** Dibangun langsung di atas [caviarnine/cantex_sdk](https://github.com/caviarnine/cantex_sdk).

## Lisensi

MIT OR Apache-2.0  
Lihat [LICENSE-MIT](LICENSE-MIT) dan [LICENSE-APACHE](LICENSE-APACHE).

---

## Platform yang Didukung

Bot ini telah diuji dan berjalan di platform berikut:

**Windows (PowerShell)**
![Windows PowerShell](assets/screenshot-windows.png)

**Linux / Ubuntu**
![Linux Ubuntu](assets/screenshot-linux.png)

**Android (Termux + proot-distro)**
![Termux dengan proot-distro](assets/screenshot-termux.jpeg)

---

## Instalasi

Membutuhkan Python 3.11+ dan [uv](https://github.com/astral-sh/uv).

```bash
uv sync
```

---

## Strategi

Tiga strategi trading tersedia. Pilih salah satu dengan mengatur `"strategy"` di `config.json`:

| Strategi | Keterangan |
| --- | --- |
| `"swap"` | Loop swap dua arah dengan interval acak (strategi awal) |
| `"scalp"` | Scalping berbasis ambang harga dengan profit target dan/atau stop-loss, dilengkapi pencatatan P&L |
| `"drip"` | Sesi harian satu arah yang membagi saldo menjadi swap-swap kecil yang merata |

Hanya blok yang sesuai dengan strategi aktif yang perlu ada di `config.json`, meskipun ketiga blok bisa disimpan sekaligus untuk kemudahan penggantian strategi.

---

## Mode Multi-Akun

Bot dapat mengelola beberapa akun secara bersamaan dalam satu proses. Buat satu sub-folder per akun di dalam direktori `accounts/`:

```
accounts/
    account1/
        config.json        ← konfigurasi strategi untuk akun ini
        .env               ← CANTEX_OPERATOR_KEY, CANTEX_TRADING_KEY
        secrets/
            api_key.txt
    account2/
        config.json
        .env
        secrets/
            api_key.txt
```

Semua akun berjalan secara bersamaan. Masing-masing menggunakan kredensial dan konfigurasi strategi sendiri secara independen. Setiap baris log diberi **prefiks tag `[nama_akun]` berwarna magenta** agar mudah dibedakan.

Jika direktori `accounts/` tidak ada, bot akan menggunakan `config.json` dan `.env` dari direktori utama proyek (mode akun tunggal, kompatibel dengan versi sebelumnya).

---

## Konfigurasi Awal

Buat file `.env` di direktori utama proyek (atau di dalam sub-folder tiap akun untuk mode multi-akun):

```bash
CANTEX_OPERATOR_KEY=hex_kunci_operator_kamu
CANTEX_TRADING_KEY=hex_kunci_intent_kamu
CANTEX_BASE_URL=https://api.testnet.cantex.io   # opsional, default ke testnet
```

Kemudian edit `config.json` untuk mengatur strategi dan parameternya (lihat di bawah).

---

## Referensi config.json

```json
{
  "strategy": "drip",

  "api_key_path": "secrets/api_key.txt",

  "swap": {
    "token_a": "CC",
    "token_b": "USDCx",

    "amount_min": "10",
    "amount_decimal_places": 6,

    "interval_min_minutes": 5,
    "interval_max_minutes": 10,

    "max_network_fee": "0.1"
  },

  "scalp": {
    "token_a": "CC",
    "token_b": "USDCx",

    "amount_decimal_places": 6,

    "profit_target_pct": 2.0,
    "stop_loss_pct": 1.0,

    "min_position_amount": "0.001",

    "interval_min_seconds": 15,
    "interval_max_seconds": 30,

    "watch_interval_min_seconds": 60,
    "watch_interval_max_seconds": 120,

    "max_network_fee": "0.1"
  },

  "drip": {
    "token_a": "CC",
    "token_b": "USDCx",

    "num_swaps": 10,

    "amount_decimal_places": 6,

    "interval_min_seconds": 300,
    "interval_max_seconds": 600,

    "reset_hour_utc": 0,
    "reset_minute_utc": 5,

    "max_network_fee": "0.1"
  }
}
```

### Field tingkat atas

| Field | Wajib | Default | Keterangan |
| --- | --- | --- | --- |
| `strategy` | Tidak | `"swap"` | Strategi aktif: `"swap"`, `"scalp"`, atau `"drip"`. |
| `api_key_path` | Tidak | `"secrets/api_key.txt"` | Lokasi penyimpanan cache API key di disk antar restart. Set ke `null` untuk menonaktifkan. |

### Blok `swap`

| Field | Wajib | Default | Keterangan |
| --- | --- | --- | --- |
| `token_a` | Ya | — | Simbol atau ID instrumen token utama (contoh: `"CC"`). |
| `token_b` | Ya | — | Simbol atau ID instrumen token kedua (contoh: `"USDCx"`). |
| `amount_min` | Ya | — | Jumlah minimum yang dijual per swap (contoh: `"10"`). |
| `amount_decimal_places` | Tidak | `6` | Jumlah desimal saat mengacak nominal jual. |
| `interval_min_minutes` | Ya | — | Waktu tunggu minimum antar siklus (menit). |
| `interval_max_minutes` | Ya | — | Waktu tunggu maksimum antar siklus (menit). |
| `max_network_fee` | Ya | — | Swap dilewati jika network fee yang dikutip >= nilai ini. |

### Blok `scalp`

| Field | Wajib | Default | Keterangan |
| --- | --- | --- | --- |
| `token_a` | Ya | — | Simbol atau ID instrumen token posisi (token yang dipegang). |
| `token_b` | Ya | — | Simbol atau ID instrumen token quote. |
| `profit_target_pct` | Tidak* | `0` | Jual ketika harga naik sebesar % ini dari harga masuk. |
| `stop_loss_pct` | Tidak* | `0` | Jual ketika harga turun sebesar % ini dari harga masuk. |
| `min_position_amount` | Tidak | `"0.001"` | Saldo `token_a` minimum agar dianggap sedang memegang posisi. |
| `amount_decimal_places` | Tidak | `6` | Jumlah desimal untuk nominal jual. |
| `interval_min_seconds` | Ya | — | Interval polling minimum saat memegang posisi (detik). |
| `interval_max_seconds` | Ya | — | Interval polling maksimum saat memegang posisi (detik). |
| `watch_interval_min_seconds` | Tidak | `4× interval_min` | Interval polling minimum saat menunggu kesempatan beli ulang (detik). |
| `watch_interval_max_seconds` | Tidak | `4× interval_max` | Interval polling maksimum saat menunggu kesempatan beli ulang (detik). |
| `max_network_fee` | Ya | — | Swap dilewati jika network fee yang dikutip >= nilai ini. |

\* Setidaknya satu dari `profit_target_pct` atau `stop_loss_pct` harus diisi dengan nilai positif.

### Blok `drip`

| Field | Wajib | Default | Keterangan |
| --- | --- | --- | --- |
| `token_a` | Ya | — | Simbol atau ID instrumen token utama. |
| `token_b` | Ya | — | Simbol atau ID instrumen token kedua. |
| `num_swaps` | Tidak | `10` | Jumlah bagian yang sama untuk membagi saldo per sesi harian. |
| `amount_decimal_places` | Tidak | `6` | Presisi pembulatan nominal swap. |
| `interval_min_seconds` | Ya | — | Waktu tunggu minimum antar swap dalam satu sesi (detik). |
| `interval_max_seconds` | Ya | — | Waktu tunggu maksimum antar swap dalam satu sesi (detik). |
| `reset_hour_utc` | Tidak | `0` | Jam UTC untuk reset sesi harian. |
| `reset_minute_utc` | Tidak | `5` | Menit UTC untuk reset sesi harian. |
| `max_network_fee` | Ya | — | Jika fee >= nilai ini, bot menunggu interval normal lalu mencoba ulang swap yang sama (tidak pernah dilewati). |

---

## Menjalankan Bot

```bash
uv run main.py
```

Hentikan kapan saja dengan `Ctrl+C`.

---

## Cara Kerja

### Strategi `swap`

Setiap siklus, bot melakukan:

1. **Resolusi instrumen** — saat pertama berjalan, bot mencari ID instrumen live untuk `token_a` dan `token_b` dengan mencocokkan simbol ke daftar token akun (tidak peka huruf besar/kecil). Keluar dengan pesan error yang jelas jika salah satu token tidak ditemukan.

2. **Cek saldo live** — menentukan arah swap:
   - Jika saldo `token_a` ≥ `amount_min`: jual `token_a` untuk mendapatkan `token_b`.
   - Jika tidak, jika saldo `token_b` ≥ `amount_min`: jual `token_b` untuk mendapatkan `token_a` (swap terbalik).
   - Jika kedua saldo tidak memenuhi minimum: log error dan lewati siklus.

3. **Pilih jumlah acak** — terdistribusi merata antara `amount_min` dan saldo token yang dijual, dibulatkan ke `amount_decimal_places` desimal.

4. **Ambil kutipan harga** — jika network fee yang dikutip ≥ `max_network_fee`, swap dilewati.

5. **Eksekusi swap** — mengirim transaksi swap dan mencatat hasilnya.

6. **Tunggu** — tidur selama durasi acak antara `interval_min_minutes` dan `interval_max_minutes` sebelum siklus berikutnya.

Bot mencatat total jumlah swap berhasil, swap dilewati karena fee, dan swap dilewati karena saldo kurang di setiap siklus.

---

### Strategi `scalp`

Mesin dua kondisi (**WATCHING** ↔ **HOLDING**) yang mengelola posisi berulang di `token_a`:

1. **Kondisi WATCHING** — menghabiskan seluruh saldo `token_b` untuk membeli `token_a` secepatnya, lalu beralih ke HOLDING. Selama masih di WATCHING (misalnya fee terlalu tinggi), polling dilakukan dengan interval `watch_interval` yang lebih lambat.

2. **Kondisi HOLDING** — polling harga pool setiap `interval_min_seconds` – `interval_max_seconds`. Metrik harga yang digunakan adalah `pool_price_before_trade` dari probe `token_a → token_b`, yaitu "berapa `token_b` per satu `token_a`". Kondisi keluar diperiksa secara berurutan:
   - **Stop-loss:** harga ≤ entry_price × (1 − `stop_loss_pct` / 100)
   - **Profit target:** harga ≥ entry_price × (1 + `profit_target_pct` / 100)

   Ketika kondisi keluar terpenuhi, seluruh saldo `token_a` dijual dan bot kembali ke kondisi WATCHING.

3. **Keamanan restart** — saldo `token_a` yang tidak nol (≥ `min_position_amount`) saat startup diperlakukan sebagai posisi yang sudah ada. Harga masuk di-rebaseline ke harga pasar saat ini agar kedua kondisi keluar langsung berfungsi dari siklus pertama.

4. **Pencatatan P&L** — P&L per trade (token_b diterima − token_b dikeluarkan) dan P&L kumulatif dicatat di setiap siklus.

---

### Strategi `drip`

Loop sesi harian satu arah:

1. **Deteksi arah** — di awal setiap sesi, saldo live menentukan arah swap:
   - `token_a` > 0 → jual `token_a`, beli `token_b` (A → B)
   - `token_b` > 0 → jual `token_b`, beli `token_a` (B → A)
   - Keduanya nol → tidur hingga reset harian berikutnya.

   Karena seluruh token jual habis di setiap sesi, arah secara alami bergantian dari hari ke hari.

2. **Pembagian saldo** — saldo jual yang tersedia dibagi tepat menjadi `num_swaps` bagian yang sama.

3. **Eksekusi swap** — satu bagian di-swap per siklus, dengan jeda acak `interval_min_seconds` – `interval_max_seconds` di antara setiap swap. Swap terakhir di setiap sesi menguras seluruh saldo yang tersisa agar tidak ada yang tertinggal di token jual.

4. **Coba ulang fee** — jika fee yang dikutip ≥ `max_network_fee`, bot menunggu interval normal lalu mencoba ulang swap yang *sama* (tidak pernah dilewati) untuk memastikan seluruh saldo habis sebelum sesi berakhir.

5. **Reset harian** — setelah semua swap selesai (atau saldo turun di bawah `min_swap_amount`), bot tidur hingga `reset_hour_utc:reset_minute_utc` UTC dan memulai sesi baru.

6. **Keamanan restart** — setelah setiap sesi selesai, tanggal UTC ditulis ke `drip_state.json`. Saat restart, jika sesi hari ini sudah selesai, bot tidur hingga reset berikutnya alih-alih menjalankan sesi duplikat. Crash di tengah sesi membiarkan file state tidak berubah, sehingga bot melanjutkan dengan saldo yang tersisa — yang merupakan perilaku yang benar.