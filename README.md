# insider_alert

SEC EDGAR'ga tushayotgan **Form 4** hujjatlarini kuzatib, yirik insayder
**xaridlarini** topadi va Telegram'ga xabar yuboradi.

---

## ⚠️ OGOHLANTIRISH

Bu vosita **faqat ochiq ma'lumot yig'ish uchun**. Bu investitsiya maslahati
emas. U hech qanday savdo qilmaydi, hech qanday brokerga ulanmaydi va
buyurtma bermaydi — va shunday bo'lib qolishi kerak. Insayder xaridi
kelajakdagi narx harakatini kafolatlamaydi. Har qanday qarorni o'zingiz
qabul qilasiz.

---

## Nima qiladi

1. Har 5 daqiqada EDGAR'ning `getcurrent` Atom feed'idan yangi Form 4'larni oladi.
2. Har bir filing'ning xom XML'ini o'qiydi va **faqat haqiqiy pul sarflangan
   xaridni** ajratadi.
3. Bozor ma'lumotini qo'shadi (narx, market cap, birja, 52 hafta diapazoni).
4. Filtrdan o'tganini Telegram'ga yuboradi.
5. Har kuni tunda kunlik indeksdan to'liq reconciliation qiladi — feed'dan
   o'tkazib yuborilgan filing bo'lmasin.

### Nima "xarid" hisoblanadi

Faqat `transactionCode = P` **va** `acquiredDisposedCode = A` **va** narx > 0.

Qat'iy chetlab o'tiladi:

| Kod | Nima | Nega chetlab o'tiladi |
|---|---|---|
| `S` | sotuv | signal emas |
| `A` | kompaniya grant/award | bepul olingan, pul sarflanmagan |
| `M`, `X` | optsion ijrosi | yangi pul kirmagan |
| `F` | soliq uchun ushlangan | insayder qarori emas |
| `G`, `C`, `D` | sovg'a, konvertatsiya, qaytarish | naqd xarid emas |
| narx = 0 | har qanday kod | pul sarflanmagan |

### Ikki muhim nuans

**1. Kod `P` har doim ochiq bozor xaridi degani emas.** U kompaniyadan
to'g'ridan-to'g'ri yangi aksiyalarga yozilishni (subscription / private
placement / PIPE) ham qamrab oladi. Bu mexanik jihatdan boshqa narsa: pul
kompaniya kassasiga kiradi va yangi aksiya chiqariladi — ya'ni dilyutsiya.
Footnote matni tekshiriladi va topilsa xabarda ⚠️ bilan belgilanadi. Filing
o'chirilmaydi, lekin siz farqni ko'rasiz.

VCIG'ning 2026-yil may oyidagi ikki xaridi aynan shunday holat edi.

**2. Derivativ xaridlar asosiy signaldan chiqarilgan.** Haqiqiy filing
(SHF Holdings, `0001493152-26-022075`) da `nonDerivativeTransaction` **nol ta**,
lekin kod `P` bilan Series B Preferred Stock 63 × $800 va varrant 4,057 × $0
bor. Bu insayderning kompaniyani "Securities Purchase Agreement" orqali
finanslashi — bozorda aksiya sotib olishi emas. Bunday oyoqlar
`derivative_purchases`da ko'rinadi, lekin summani shishirmaydi.

---

## Filtrlar va ular nimani anglatadi

`config.yaml`:

```yaml
filters:
  min_value_usd: 250000
  min_pct_of_market_cap: 1.0
```

Ikkisi **birga** (AND) ishlaydi. Buning matematik natijasi:

| Xarid summasi | Maksimal market cap |
|---|---|
| $250,000 | $25M |
| $500,000 | $50M |
| $1,000,000 | $100M |
| $10,000,000 | $1B |

Ya'ni bu **nano/micro-cap ovchisi**. Kattaroq kompaniyalardagi xaridlarni ham
ko'rishni xohlasangiz, `min_pct_of_market_cap`ni pasaytiring (masalan `0.25`).

**Market cap topilmasa** filing o'chirilmaydi — ogohlantirish bilan yuboriladi.
Yahoo'da ma'lumot yo'qligi sababli $2 mln lik CEO xaridini o'tkazib yuborish
eng yomon natija bo'lardi. `alert_when_cap_unknown: false` bilan o'zgartirasiz.

---

## O'rnatish (lokal sinov uchun)

```bash
git clone <repo-url> && cd insider_alert
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # va .env ni to'ldiring
```

Tarmoqsiz tekshirish:

```bash
python run.py --self-test --dry-run     # fixture'larda, internet kerak emas
python -m pytest tests/ -q              # 12 test
```

Jonli sinov (Telegram'ga yubormaydi):

```bash
python run.py --dry-run
```

Rejimlar:

| Buyruq | Nima qiladi |
|---|---|
| `python run.py` | feed'dan bir marta tekshiradi (CI shuni ishlatadi) |
| `--dry-run` | hammasi ishlaydi, Telegram'ga yubormaydi |
| `--self-test` | fixture'larda, tarmoqsiz |
| `--reconcile 2026-05-22` | bir kunni kunlik indeksdan to'liq qayta o'qiydi |
| `--backfill 2026-05-15 2026-06-01` | davrni qayta ishlaydi |

---

## 1-qadam: Telegram (buni O'ZINGIZ qilasiz)

Token — bu parol. Men uni ko'rmasligim kerak va hech qaerga yozmayman.

### Bot yaratish

1. Telegram'da **@BotFather** ni oching → `/newbot`
2. Nom va username bering (username `bot` bilan tugashi kerak)
3. Tokenni oling — `123456789:AAF...` ko'rinishida

> Webull skaneringizdagi mavjud botni qayta ishlatsangiz, bu qadam kerak emas.

### Guruh mavzusiga (topic) yuborish

1. Guruhda **Settings → Topics** yoqilgan bo'lsin
2. Botni guruhga qo'shing va **xabar yuborish huquqini** bering
   (eng ishonchlisi — admin qiling)
3. Kerakli mavzuni oching va u yerga bitta xabar yozing
4. Brauzerda oching: `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Javobdan ikkita raqamni oling:
   - `"chat":{"id":-1001234567890` → bu **TELEGRAM_CHAT_ID** (minus bilan)
   - `"message_thread_id":42` → bu **TELEGRAM_THREAD_ID**

`message_thread_id` ko'rinmasa, demak xabarni General mavzusiga yozgansiz —
kerakli mavzu ichiga yozib, `getUpdates`ni qayta oching.

Oddiy shaxsiy chatga yuborish uchun `TELEGRAM_THREAD_ID` ni bo'sh qoldiring.

### Tekshirish

```bash
python run.py --test-telegram
```

Bitta sinov xabari yuboradi va qayerga ketganini aytadi. Xato bo'lsa sababini
aniq yozadi (mavzu topilmadi / bot guruhda emas).

## 2-qadam: GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Uchta secret qo'shing:

| Nomi | Qiymati |
|---|---|
| `SEC_USER_AGENT` | `Ism Familiya siz@example.com` — SEC talab qiladi, bo'lmasa 403 |
| `TELEGRAM_BOT_TOKEN` | BotFather bergan token |
| `TELEGRAM_CHAT_ID` | guruh id'si (`-100...`) yoki shaxsiy chat id |

`TELEGRAM_THREAD_ID` esa **secret emas, Variable** bo'lib qo'shiladi
(Settings → Secrets and variables → Actions → **Variables** → New variable),
chunki mavzu raqami maxfiy emas. Secret qilib qo'ysangiz GitHub o'sha raqamga
mos har bir belgini log'da yulduzcha bilan yashiradi va log o'qib bo'lmay
qoladi.

| Variable nomi | Qiymati |
|---|---|
| `TELEGRAM_THREAD_ID` | mavzu id'si — faqat forum guruhi uchun, aks holda qo'shmang |

`SEC_USER_AGENT` ichida haqiqiy email bo'lishi shart. SEC bu orqali kim
so'rov yuborayotganini biladi; yolg'on qo'yish ToS buzilishi va IP bloklanishi
bilan tugaydi.

---

## 3-qadam: sinash

1. Repo → **Actions** → `tests` workflow o'tishini kuting (yashil bo'lishi kerak)
2. **Actions → monitor → Run workflow** → `dry_run` ✅ → ishga tushiring
3. Log'da nima topilganini ko'ring. Telegram'ga hech narsa ketmaydi.
4. Hammasi yaxshi bo'lsa, `dry_run`siz bir marta ishga tushirib, Telegram'ga
   xabar kelishini tekshiring.

Cron o'zi ishlay boshlaydi. Birinchi run'dan keyin `state/state.json` repo'ga
commit bo'ladi — bu holat fayli, uni qo'lda tahrirlash kerak emas.

---

## GitHub Actions cheklovlari — bilib turishingiz kerak

| Cheklov | Ta'siri |
|---|---|
| Eng qisqa cron `*/5` | bundan tez bo'lmaydi |
| Cron aniq vaqtda ishlamaydi | yuklama paytida 10–30 daqiqa kechikish odatiy |
| **Real kechikish ~5–25 daqiqa** | "darhol" emas. Aggregatorlardan tezroq, lekin bir necha daqiqa emas |
| **Public repo: daqiqa cheklovi yo'q** | shuning uchun `*/5` ishlatilgan. Billing haqida o'ylash shart emas |
| 60 kun commit bo'lmasa | GitHub scheduled workflow'larni o'chiradi. Holat fayli har signalda commit bo'lgani uchun bu o'zi hal bo'ladi |
| Workflow yiqilsa | GitHub xabar bermaydi. Shuning uchun xatolikni bot o'zi Telegram'ga yuboradi |
| Cron faqat default branch'da | `main`da bo'lishi kerak |

**Repo public — buni bilib turing:** kod, `state/state.json` va **workflow
log'lari** hammaga ko'rinadi. Secret'larni GitHub avtomatik yashiradi va ular
hech qachon log'ga chiqmaydi. Shuning uchun kodga `echo $TELEGRAM_BOT_TOKEN`
kabi narsa qo'shmang. Repo'dagi hamma ma'lumot SEC'ning ochiq ma'lumoti,
maxfiy narsa yo'q.

Haqiqiy 1–2 daqiqali kechikish kerak bo'lsa, GitHub Actions bunga yaramaydi —
u holda ~$5/oy VPS'da doimiy process kerak bo'ladi. Kod host'ga bog'liq emas,
ko'chirish faqat cron o'rniga `while True` qo'yish.

---

## Loyiha strukturasi

```
insider_alert/
├── config.yaml              # filtrlar va sozlamalar (secret YO'Q)
├── .env.example             # secret'lar shabloni
├── run.py                   # CLI va pipeline
├── insider/
│   ├── edgar.py             # rate limiter, retry, Atom feed, filing olish
│   ├── form4.py             # XML parser, xarid mantig'i, subscription detektor
│   ├── market.py            # MarketDataProvider + yfinance (+ finviz shabloni)
│   ├── screen.py            # filtrlar
│   ├── notify.py            # Telegram
│   ├── state.py             # holat (GitHub'ga commit bo'ladigan JSON)
│   └── config.py
├── tests/fixtures/          # 8 ta Form 4 namunasi
└── .github/workflows/       # monitor, reconcile, tests
```

Kod izohlari inglizcha, README va Telegram xabarlari o'zbekcha.
Xabarlardagi barcha vaqtlar **Nyu-York vaqti** (EDT/EST) — AQSh bozor kuniga mos.

## Texnik qarorlar

- **Filing bir so'rovda olinadi.** XML fayl nomi turlicha bo'ladi
  (`ownership.xml`, `wk-form4_1778291166.xml`, ...), shuning uchun to'liq
  submission `.txt` faylidan (nomi oldindan ma'lum) XML ajratiladi.
  `index.json` faqat zaxira yo'l. Bu SEC'ga so'rovni ikki barobar kamaytiradi.
- **Bir filing bir necha marta feed'da chiqadi** — har bir issuer va har bir
  reporting owner uchun bir marta, lekin accession bir xil. Accession bo'yicha
  dedupe qilinadi.
- **Bir filingda bir necha xarid oyog'i** bo'lsa, ular bitta xaridga
  jamlanadi: umumiy summa + vaznli o'rtacha narx.
- **Hech qachon crash bo'lmaydi.** Buzuq XML, yo'q maydon, `&` kabi noto'g'ri
  belgi — log yoziladi va davom etadi. Bitta filing butun run'ni to'xtatmaydi.
- **SEC rate limit** 10 req/s, biz 8 da ishlaymiz. 403/429/5xx uchun
  eksponensial backoff.
