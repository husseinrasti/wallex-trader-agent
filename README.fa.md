<div dir="rtl">

# wallex-trader-agent (خلاصهٔ فارسی)

[![ci](https://github.com/husseinrasti/wallex-trader-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/husseinrasti/wallex-trader-agent/actions/workflows/ci.yml)

این صفحه ترجمهٔ خلاصه و «شروع سریع» [README.md](README.md) است. متن کامل و مرجع، نسخهٔ انگلیسی است.

**خلاصه**
1. یک عامل معامله‌گر خودکار و قابل‌توضیح برای بازار اسپات صرافی والکس، ساخته‌شده برای «چالش معاملات الگوریتمی والکس» استارکوچ. در یک جمله: **هوش مصنوعی برنامه می‌ریزد، قواعد معامله می‌کنند، مدیریت ریسک نگهبانی می‌دهد.**
2. یک مسیر کد برای چهار حالت: `backtest`، `shadow` (معاملهٔ کاغذی روی دفتر سفارش زنده)، `live` و `replay`. فقط شیء صرافی، منبع داده و ساعت عوض می‌شوند.
3. هر تصمیم یک خط JSON است. این خط سیگنال، برنامهٔ هوش مصنوعیِ جاری، نتیجهٔ هر ۲۳ کنترل ریسک، سفارش و نتیجهٔ آن را دارد، به‌همراه `git_commit` و `config_hash`.
4. آنچه به‌صورت آفلاین اثبات شده: ۱٬۹۵۳ تست سبز است. یک اجرای shadow چهارساعته با LLM واقعی (روی دادهٔ مصنوعی) هر ۴۰ تصمیمش را بدون هیچ تماس شبکه‌ای دوباره تولید می‌کند (`make replay-deliverable`).
5. آنچه هنوز اثبات نشده: سودآوری، و رفتار روی دادهٔ واقعی والکس. `api.wallex.ir` از ماشین توسعه فقط گاهی در دسترس است، پس فقط حدود ۲۰ دقیقه دفتر سفارش واقعی ضبط شده است. هیچ معاملهٔ واقعی (live) انجام نشده است.

## ایده

در این چالش بازدهٔ منفی امتیاز صفر می‌گیرد، کارمزد جفت‌های تومانی ۰٫۲۵ تا ۰٫۳۰ درصد است و فقط موجودی نهایی به تومان حساب می‌شود. فرض ما این است: **ضرر نکن، منظم معامله کن، قابل‌ممیزی باش.** برای همین سه لایه کاملاً از هم جدا هستند:

* **حلقهٔ سریع (قطعی، هر ۲ ثانیه):** دفتر سفارش ← ویژگی‌ها ← نیت‌های استراتژی ← ریسک ← سفارش. سه استراتژی در آن کار می‌کنند. **S1 CrossBook** یک کوین را در هر کدام از دو دفترِ تومانی و تتری‌اش که ارزان‌تر است می‌خرد و در دیگری می‌فروشد، به شرطی که اختلاف قیمت از مجموع کارمزدها به‌اضافهٔ یک حاشیه بیشتر باشد، پس به جهت بازار وابسته نیست. **S2 USDTAlloc** سهمی صریح و محدود از تتر نگه می‌دارد. **S3 RegimeMomentum** به‌طور پیش‌فرض خاموش است.
* **حلقهٔ کند (LLM):** شامل تحلیلگر رژیم (هر ۵ دقیقه، با فراخوانی ابزار روی سرور MCP والکس و ابزارهای داخلی) و بازبین پس از معامله (هر ساعت). خروجی آن یک *برنامه* با مقادیر محدودشده است: رژیم‌ها، استراتژی‌های فعال، ضریب اندازه و هدف تتر. این برنامه فقط می‌تواند کار قواعد را محدودتر کند و این حلقه هرگز سفارشی ثبت نمی‌کند.
* **نگهبان (قطعی):** شامل RiskManager با ۲۳ کنترل، کلید توقف در افت سرمایه، و تبدیل همه‌چیز به تومان پیش از پایان چالش. هر سفارش از آن عبور می‌کند و هیچ خروجی هوش مصنوعی نمی‌تواند آن را دور بزند.

پاسخ‌های LLM با هش درخواست ذخیره می‌شوند. به همین دلیل `replay` یک جلسهٔ ضبط‌شده را بدون دسترسی به شبکه دوباره اجرا می‌کند.

## وضعیت (۲۰۲۶-۱۰-۲۳، تگ `v0.6-ops`)

* **اثبات‌شده:**
  * ۱٬۹۵۳ تست سبز است و CI (ruff، pytest روی پایتون 3.11، ساخت Docker) هم سبز است.
  * همان `Engine` در حالت‌های backtest، shadow و replay اجرا می‌شود و قواعد ریسک طبق مشخصات عمل می‌کنند.
  * در یک اجرای shadow ‏۴٫۰۸ ساعته روی **دادهٔ مصنوعی**، ‏۳۸ برنامه از `openai/gpt-5.4-mini` (از طریق OpenRouter) گرفته شد. وقتی ارائه‌دهنده از دسترس خارج شد، موتور طبق طراحی فقط با قواعد ادامه داد.
  * replay همان اجرا: ۴۰ از ۴۰ تصمیم یکسان بود، با ۰ تلاش اتصال و ۰ فراخوانی LLM.
* **در انتظار:**
  * **معاملهٔ واقعی (live):** انجام نشده است. آن را فقط اپراتور و بعد از «go live» شروع می‌کند ([GO_LIVE_CHECKLIST](docs/GO_LIVE_CHECKLIST.md)).
  * **دادهٔ واقعی:** فقط حدود ۲۰ دقیقه دفتر سفارش REST در ۲۰۲۶-۱۰-۲۲ (به‌اضافهٔ یک انفجار یک‌ثانیه‌ای) و ۲٫۲ ساعت کندل یک‌دقیقه‌ای وجود دارد. حکم بررسی بازار (survey) «دادهٔ ناکافی» است. همین دادهٔ ضبط‌شده در git است (`deliverables/market/`، دستور `make market-db`)، پس گزارش‌های دادهٔ واقعی به‌صورت آفلاین دوباره اجرا می‌شوند.
  * **اتصال به API:** `api.wallex.ir` فقط گاه‌به‌گاه در دسترس است. WebSocket، ابزارهای MCP، پذیرش سفارش STOP_LIMIT و مسیریابی زیرحساب هنوز روی API زنده آزموده نشده‌اند.
  * **تنظیمات اپراتور:** `CHALLENGE_END_UTC`، `WALLEX_SUB_ACCOUNT_CLIENT_ID` و `TELEGRAM_CHAT_ID` در `.env` اپراتور هنوز خالی‌اند و باید پیش از live پر شوند. کلید Gemini (جایگزین LLM) محدودیت سهمیه دارد (HTTP 429).

همهٔ اجراهای طولانی‌تر روی دادهٔ **مصنوعی (SYNTHETIC)** بوده‌اند: اجرای shadow چهارساعته، بک‌تست‌های ۶ ساعته، ۲۴ ساعته و ۳۰ روزه، و walk-forward. گزارش‌هایشان این را در عنوان و بنر می‌گویند. این اجراها سازوکار را اثبات می‌کنند، نه سود را.

## شروع سریع برای داوران

**دسترسی:** این مخزن تا زمان ارسال نهایی **خصوصی** است و اپراتور آن را هنگام ارسال باز می‌کند ([SUBMISSION_CHECKLIST §3](docs/SUBMISSION_CHECKLIST.md)). تا آن زمان دستور clone زیر و نشان CI فقط برای حساب‌هایی کار می‌کند که دسترسی گرفته‌اند؛ درخواست بدون احراز هویت پاسخ HTTP 404 می‌گیرد.

پیش‌نیازها: پایتون ۳٫۱۱ یا بالاتر (با 3.11، 3.12 و 3.14 آزموده شده)، `make` و git. هیچ‌کدام از این دستورها نمی‌تواند سفارش واقعی ثبت کند. حالت live به `WTA_LIVE=1` در محیط، پرچم `--i-understand-live`، درخت git تمیز و `CHALLENGE_END_UTC` نیاز دارد.

</div>

```bash
git clone https://github.com/husseinrasti/wallex-trader-agent.git
cd wallex-trader-agent
cp .env.example .env          # all keys are optional
make install                  # .venv (python3; or: make install PYTHON=python3.12)
source .venv/bin/activate
make test                     # ruff + pytest, ~2 min, exit 0
```

<div dir="rtl">

**کلیدها:** بدون هیچ کلیدی همهٔ دستورهای زیر کار می‌کنند. در این حالت لایهٔ هوش مصنوعی از ارائه‌دهندهٔ قواعد آفلاین (`offline-rules-v1`) استفاده می‌کند. این ارائه‌دهنده در لاگ‌ها، در `wta status` و در گزارش‌ها با برچسب «NOT an LLM» مشخص است. اگر `OPENROUTER_API_KEY` و `OPENROUTER_MODEL_FAST` تنظیم شوند، LLM واقعی فراخوانی می‌شود. داده‌های بازار عمومی‌اند.

| # | دستور | پیش‌نیاز | زمان | خروجی مورد انتظار |
|---|---|---|---|---|
| ۱ | `make shadow-synth` | هیچ (دادهٔ مصنوعی آفلاین) | تا Ctrl-C | هر ۶۰ ثانیه یک خط `heartbeat: feed=synthetic ... fresh_books=21/21`؛ با Ctrl-C پیام `engine: stopped (signal SIGINT)` |
| ۲ | `make shadow` | دسترسی به `api.wallex.ir` | تا Ctrl-C | معاملهٔ کاغذی روی دفتر سفارش زنده؛ بدون دسترسی: `fresh_books=0/21` و هیچ معامله‌ای |
| ۳ | `make backtest-synth` | هیچ | حدود ۳۰ ثانیه | `return -0.236%  max drawdown +0.770%  trades 2  orders 19  vetoes 18  fees 16,063 TMN`؛ گزارش در `logs/synthetic_bt_books/` |
| ۴ | `make backtest` | ۳۰ روز کندل واقعی در `data/wta.sqlite` (نیاز به API) | ۱۵ تا ۲۰ دقیقه | روی کلون تازه: `error: market database data/wta.sqlite does not exist` |
| ۵ | `make replay-deliverable` | هیچ (بسته در git است) | ۲ تا ۳ دقیقه | `result:   40/40 decisions reproduced -> PASS` و `0 socket attempts, 0 LLM provider calls` |
| ۶ | `make backtest-cache` | هیچ (کش LLM در git است) | حدود ۳ دقیقه | اجرای LLM واقعیِ آزمون ablation دوباره و بدون کلید: `ai=cache`، `return +0.121%`، `model calls 132 (132 cached)` |
| ۷ | `make market-db` | هیچ (دادهٔ ضبط‌شده در git است) | حدود ۱ ثانیه | `SHA256SUMS verified; wrote data/wta.sqlite`؛ پس از آن گزارش‌های دادهٔ واقعی آفلاین دوباره اجرا می‌شوند ([REPRODUCE §4.2](docs/REPRODUCE.md)) |
| ۸ | `python -m wta status` / `python -m wta tui` | لاگ یک اجرا | فوری | خلاصهٔ یک‌صفحه‌ای / داشبورد زنده (Ctrl-C برای خروج) |

برای اجرای مصنوعیِ مرحلهٔ ۱:

</div>

```bash
python -m wta status --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite
python -m wta tui    --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite
```

<div dir="rtl">

### Docker

</div>

```bash
cp .env.example .env
docker compose build
docker compose run --rm --no-deps agent python -m pytest
docker compose run --rm --no-deps agent python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h
docker compose up -d recorder agent      # recorder + shadow agent on the real feed
docker compose down
```

<div dir="rtl">

پرچم `--no-deps` لازم است. بدون آن compose سرویس recorder را هم بالا می‌آورد و روشن نگه می‌دارد. فقط پوشه‌های `./data`، `./logs` و `./configs/runtime` به کانتینر وصل‌اند، پس خروجی‌ها را زیر `logs/` بنویسید.

## مستندات (به این ترتیب بخوانید)

1. [`docs/STRATEGY.md`](docs/STRATEGY.md): استراتژی‌ها
2. [`docs/RISK.md`](docs/RISK.md): مدیریت ریسک
3. [`docs/REPORTS/`](docs/REPORTS/README.md) و [`deliverables/REPORT.md`](deliverables/REPORT.md): نتایج
4. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): معماری و نحوهٔ استفاده از هوش مصنوعی
5. [`docs/REPRODUCE.md`](docs/REPRODUCE.md): بازتولید همهٔ نتایج
6. [`docs/RUNBOOK.md`](docs/RUNBOOK.md): عملیات
7. [`docs/DECISION_LOG_FORMAT.md`](docs/DECISION_LOG_FORMAT.md): قالب لاگ تصمیم
8. [`docs/GO_LIVE_CHECKLIST.md`](docs/GO_LIVE_CHECKLIST.md): چک‌لیست شروع معاملهٔ واقعی

## ویدئو و تماس

* **ویدئوی نمایشی** (حدود ۵ دقیقه، ضبط توسط اپراتور): هنوز ضبط نشده است. پیوند آن در [`deliverables/VIDEO.md`](deliverables/VIDEO.md) قرار می‌گیرد. فهرست صحنه‌ها و متن روایت در [`docs/VIDEO_SHOTLIST.md`](docs/VIDEO_SHOTLIST.md) است. متن فارسی روایت: [`docs/VIDEO_SCRIPT_FA.md`](docs/VIDEO_SCRIPT_FA.md).
* **سازنده:** حسین راستی (توسعه‌دهندهٔ تنها)، گیت‌هاب [@husseinrasti](https://github.com/husseinrasti).

</div>
