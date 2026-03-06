# Cantex Swap Bot

Bot swap otomatis untuk bursa terdesentralisasi [Cantex](https://cantex.io). Kredensial dibaca dari file `.env` dan parameter swap dari `config.json`.

> **Catatan fork:** Dibangun langsung di atas [caviarnine/cantex_sdk](https://github.com/caviarnine/cantex_sdk).

## Lisensi

MIT OR Apache-2.0  
Lihat [LICENSE-MIT](LICENSE-MIT) dan [LICENSE-APACHE](LICENSE-APACHE).

---

## Instalasi

Membutuhkan Python 3.11+ dan [uv](https://github.com/astral-sh/uv).

```bash
uv sync
```

---

## Konfigurasi Awal

Buat file `.env` di direktori utama proyek:

```bash
CANTEX_OPERATOR_KEY=hex_kunci_operator_kamu
CANTEX_TRADING_KEY=hex_kunci_intent_kamu
CANTEX_BASE_URL=https://api.testnet.cantex.io   # opsional, default ke testnet
```

Kemudian edit `config.json` untuk mengatur parameter swap (lihat di bawah).

---

## Referensi config.json

```json
{
  "api_key_path": "secrets/api_key.txt",

  "swap": {
    "token_a": "CC",
    "token_b": "USDCx",

    "amount_min": "10",
    "amount_decimal_places": 6,

    "interval_min_minutes": 5,
    "interval_max_minutes": 10,

    "max_network_fee": "0.1"
  }
}
```

| Field | Wajib | Default | Keterangan |
| --- | --- | --- | --- |
| `api_key_path` | Tidak | `"secrets/api_key.txt"` | Lokasi penyimpanan cache API key di disk antar restart. Set ke `null` untuk menonaktifkan. |
| `swap` | Ya | — | Parameter bot swap (lihat di bawah). |

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

---

## Menjalankan Bot

```bash
uv run main.py
```

Hentikan kapan saja dengan `Ctrl+C`.

---

## Cara Kerja

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