# Product

## Register

product

## Users
Personal use — user sendiri yang memantau status pesanan Grab (share-location link) untuk orang dekat / order sendiri, sambil menerima update real-time via Telegram. Konteks: buka dashboard sekali-sekali atau pantau via notifikasi Telegram; butuh status cepat terbaca tanpa harus mikir.

## Product Purpose
Monitoring tracker untuk link `sharelocation.grab.com`: poll status pesanan/driver/ETA, tampilkan di dashboard lokal, dan push perubahan ke Telegram. Success = user tahu status order dalam <2 detik lihat layar / <1 detik baca notifikasi, tanpa false alarm atau noise.

## Brand Personality
Pragmatic, calm, sharp. Tidak panik, tidak norak; data bicara sendiri. Persona: tool pribadi yang diandalkan, bukan produk komersial.

## Anti-references
- AI slop dark theme: near-black + satu neon acid-green glow, border-top 3px berwarna per kartu, uppercase tracked label di mana-mana.
- Decorative motion / glow / shimmer yang tidak mengonfirmasi state.
- Modal sebagai first thought, custom weird controls, display font di label/data.
- Landing-page energy (hero besar, gradient blob) — ini app, bukan marketing.

## Design Principles
1. **Status first** — badge state adalah hierarki #1 di setiap kartu; sisanya menyokong.
2. **Earned familiarity** — komponen terasa seperti Linear/Stripe: kontrol standar, konsisten, hilang ke dalam tugas.
3. **Signal over decoration** — warna hanya untuk semantic state (amber prepare, green selesai, red batal); aksen Grab dipakai hemat untuk aksi utama.
4. **Quiet density** — info padat tapi rapi; monospace hanya untuk data teknis (token, koordinat, ts), bukan untuk seluruh UI.
5. **Telegram = alert keren** — pesan push harus eye-catching (emoji + struktur jelas) tapi tetap tenang, bukan scream-all-caps.

## Accessibility & Inclusion
- Contrast text utama ≥ WCAG AA di atas surface; badge state tidak bergantung warna saja (label teks ikut).
- Focus ring terlihat; `prefers-reduced-motion` dihormati (matikan transisi non-esensial).
- Touch target tombol kecil tetap ≥ ~36px; layout responsif sampai 360px.
