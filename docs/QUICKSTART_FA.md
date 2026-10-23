# راهنمای سریع: تست روی داده‌ی واقعی والکس و ضبط ویدیو

این راهنما برای اجرای پروژه روی سیستم خودتان و **بدون VPN** است (`api.wallex.ir` به IP خارجی جواب نمی‌دهد).
هیچ‌کدام از دستورهای زیر به‌جز بخش ۶ معامله‌ی واقعی انجام نمی‌دهند: همه یا فقط‌خواندنی‌اند یا حالت **shadow**
(سفارش کاغذی) دارند.

> **بهترین حالت (تست‌شده در ۲۰۲۶-۱۰-۲۳):** VPN روشن، و فقط `api.wallex.ir` (یا `31.7.66.0/24`) در VPN بای‌پس شود.
> در این حالت هم داده‌ی واقعی والکس، هم مدل زبانی واقعی و هم ابزارهای MCP والکس کار می‌کنند و **نیازی به
> `--ai offline` نیست.**
>
> **نکته‌ی مهم:** بدون VPN، والکس در دسترس است اما **OpenRouter (مدل زبانی) احتمالاً در دسترس نیست.**
> در این حالت به هر دستور `wta run` یا `demo.sh` گزینه‌ی `--ai offline` را اضافه کنید. در این حالت تحلیل‌گرِ
> قاعده‌محورِ آفلاین اجرا می‌شود. این تحلیل‌گر مدل زبانی نیست و در همه‌جا با همین عنوان («offline rules provider —
> not an LLM») نمایش داده می‌شود. برنامه‌های واقعی LLM از اجرای چهارساعته در مخزن موجودند و در ویدیو نشان داده
> می‌شوند.

## ۰. آماده‌سازی (یک بار)

```bash
cd ~/Code/hackathon/wallex-trader-agent
git pull
source .venv/bin/activate        # یا: make install
make test                        # باید سبز باشد
```

## ۱. تست کامل APIهای واقعی (VPN خاموش، حدود ۵ دقیقه)

```bash
make smoke-real
```

خروجی برای هر مرحله OK، WARN یا FAIL نشان می‌دهد:

| مرحله | چه چیزی را تست می‌کند |
|---|---|
| 1 | در دسترس بودن `api.wallex.ir` |
| 2 | `wta markets`: دقت قیمت و حجم و لیست بازارهای ممنوع |
| 3 | `check_account.py` فقط‌خواندنی: موجودی، کارمزد، زیرحساب‌ها (گزارش در `logs/api_probe.md` که commit نمی‌شود) |
| 4 | فید WebSocket برای BTCUSDT |
| 5 | فید REST |
| 6 | یک برنامه‌ی آزمایشی از تحلیل‌گر LLM. اگر WARN شد یعنی OpenRouter از این شبکه در دسترس نیست و باید `--ai offline` بگذارید |
| 7 | اجرای ۳ دقیقه‌ای shadow روی **داده‌ی واقعی** (سفارش کاغذی) |
| 8 | بررسی لاگ‌ها و اسکن کلیدها |

همه‌ی خروجی‌ها در `logs/smoke_<زمان>/` ذخیره می‌شوند. اگر مرحله‌ای FAIL شد، فایل `SUMMARY.md` و فایل همان مرحله را
برایم بفرستید.

اجرای سریع بدون shadow (حدود ۱ دقیقه): `make smoke-real-quick`

## ۲. اجرای shadow روی داده‌ی واقعی + داشبورد

ترمینال ۱:

```bash
python -m wta run --mode shadow --ai offline        # اگر LLM در دسترس است، --ai offline را حذف کنید
```

ترمینال ۲ (تمام‌صفحه، دست‌کم ۲۰۰ ستون):

```bash
python -m wta tui --mode shadow
```

در داشبورد باید قیمت‌های واقعی، `rest_ok` و `ws_ok` با مقدار true، و `fresh_books=21/21` را ببینید.
بعد از چند ثانیه S2 اولین تخصیص کاغذی‌اش را انجام می‌دهد. برای توقف: Ctrl-C.

## ۲.۵. داشبورد وب (پیشنهادی برای ضبط ویدیو)

همان داده‌های داشبورد ترمینال، در مرورگر: http://localhost:8080. داشبورد وب فقط‌خواندنی است. هیچ فایلی را
تغییر نمی‌دهد، با والکس حرف نمی‌زند و نمی‌تواند سفارش بگذارد. فقط روی `127.0.0.1` باز است.

**با Docker (همه‌چیز در کانتینر):**

```bash
docker compose up -d --build recorder agent web    # recorder + agent در حالت shadow روی داده‌ی واقعی + داشبورد وب
docker compose ps                                  # هر سه باید Up باشند؛ agent بعد از ۲ دقیقه healthy می‌شود
docker compose logs -f agent                       # خط heartbeat با fresh_books=21/21 یعنی داده‌ی واقعی می‌رسد
open http://localhost:8080
docker compose down                                # پایان
```

**بدون Docker:** ترمینال ۱ `python -m wta run --mode shadow`، ترمینال ۲ `python -m wta web --mode shadow`
(یا `make web`)، بعد http://localhost:8080.

دو زبانه دارد:

| زبانه | چه چیزی نشان می‌دهد |
|---|---|
| **Live** | حالت و فید (SHADOW، feed live، RUNNING، AI llm)، equity و منحنی‌اش، drawdown تا حد kill (۵٪)، بودجه‌ی کارمزد، health (REST، WebSocket، LLM، MCP)، دفتر سفارش ۲۱ بازار، edgeهای S1 (سبز یعنی edge ≥ هزینه)، برنامه‌ی AI با یادداشت‌ها و رژیم‌ها، پوزیشن‌ها و سفارش‌های باز، و آخرین تصمیم‌ها. **روی هر تصمیم کلیک کنید** تا هر ۲۳ چک ریسک و رکورد کامل `decisions.jsonl` را ببینید |
| **Evidence** | همه‌ی گزارش‌های `docs/REPORTS` با نمودارها: backtestها، اجرای ۴ ساعته، replay، ablation. هر گزارش برچسب SYNTHETIC یا real data دارد |

اجرای ضبط‌شده‌ی ۴ ساعته را هم می‌شود در وب نشان داد (روی پورت دیگر، کنار داشبورد زنده):

```bash
python -m wta web --port 8081 --log-dir logs/shadow_synth_4h --set 'data.state_sqlite_path=data/state_shadow4h_{mode}.sqlite'
```

## ۳. ضبط ویدیو

**پیشنهاد:** صحنه‌ی ۳ (داشبورد) را در مرورگر روی داشبورد وب ضبط کنید و بقیه‌ی صحنه‌ها را در ترمینال:

1. پیش از ضبط: `docker compose up -d --build recorder agent web` و چند دقیقه صبر کنید تا agent چند heartbeat
   و دست‌کم یک برنامه‌ی AI بنویسد. مرورگر را روی http://localhost:8080 تمام‌صفحه کنید.
2. صحنه‌های ۰ تا ۲ را با `demo.sh` در ترمینال اجرا کنید (پایین).
3. صحنه‌ی ۳: به مرورگر بروید. زبانه‌ی Live را نشان دهید، روی یک تصمیم کلیک کنید و چک‌های ریسک را بخوانید.
4. صحنه‌های ۴ تا ۷ را در ترمینال ادامه دهید. در صحنه‌ی ۵ (Evidence) می‌توانید به‌جای ترمینال زبانه‌ی Evidence
   مرورگر را نشان دهید: جدول‌ها و نمودارهای همان گزارش‌ها.

اجرای صحنه‌ها با `demo.sh` دو راه دارد. هر دو دقیقاً همان صحنه‌ها را اجرا می‌کنند.

**راه الف: داشبورد روی داده‌ی واقعی (پیشنهادی وقتی VPN خاموش است)**

```bash
bash scripts/demo.sh --feed live --ai offline --scene 0     # آماده‌سازی، ضبط نمی‌شود (~۴ دقیقه)
bash scripts/demo.sh --status                                # صبر تا drill و replay هر دو «finished, exit 0»
# ضبط را شروع کنید (QuickTime: Cmd-Shift-5)
bash scripts/demo.sh --feed live --ai offline --scene 1 --scene 2 --scene 3 --scene 4 --scene 5 --scene 6 --scene 7
```

**راه ب: همه‌چیز آفلاین (داده‌ی synthetic)؛ با VPN روشن هم کار می‌کند**

```bash
bash scripts/demo.sh --scene 0
bash scripts/demo.sh --scene 1 --scene 2 --scene 3 --scene 4 --scene 5 --scene 6 --scene 7
```

متن فارسی گویندگی: [`VIDEO_SCRIPT_FA.md`](VIDEO_SCRIPT_FA.md). در راه الف، به‌جای جمله‌ی اول صحنه‌ی ۳ این را
بخوانید:

> این داشبورد است و در یک فرایند جدا و فقط‌خواندنی اجرا می‌شود. داده‌ها **واقعی** و زنده از والکس است، اما موتور
> در حالت shadow است: سفارش‌ها کاغذی‌اند و هیچ معامله‌ی واقعی‌ای انجام نمی‌شود. تحلیل‌گر در این شبکه به مدل زبانی
> دسترسی ندارد، پس تحلیل‌گر قاعده‌محور آفلاین جایش اجرا می‌شود و صفحه هم همین را نشان می‌دهد. برنامه‌ی واقعی مدل
> زبانی را در اجرای چهارساعته خواهیم دید.

(کشتن سیستم با kill-switch در صحنه‌ی ۴ همیشه روی داده‌ی synthetic است، چون به یک افت مصنوعی ۸ درصدی نیاز دارد.
همین را هم بگویید.)

بعد از ضبط: `bash scripts/demo.sh --stop`

## ۴. ثبت لینک ویدیو و تحویل

1. ویدیو را در YouTube به‌صورت **unlisted** یا در Google Drive با دسترسی «anyone with the link» آپلود کنید.
2. لینک را برایم بفرستید تا ثبت کنم. یا خودتان فایل `deliverables/VIDEO.md` را ویرایش کنید و بعد:
   ```bash
   git add deliverables/VIDEO.md && git commit -m "docs: demo video link" && git push
   ```
3. مخزن عمومی است: https://github.com/husseinrasti/wallex-trader-agent

## ۵. (اختیاری) جمع‌آوری داده‌ی واقعی و survey

```bash
nohup python -m wta record --symbols-from configs/markets.yaml > logs/recorder.out 2>&1 &
# دست‌کم ۱ ساعت بعد:
make survey        # docs/REPORTS/survey_<date>.md روی داده‌ی واقعی
```

## ۶. (اختیاری) معامله‌ی live: فقط با تصمیم خودتان

پیش‌نیاز در `.env`: `CHALLENGE_END_UTC` (زمان پایان رسمی، مثل `2026-10-25T12:00:00Z`) و
`WALLEX_SUB_ACCOUNT_CLIENT_ID` (شناسه‌ی زیرحساب چالش؛ `check_account.py` آن را نشان می‌دهد).
مراحل کامل در [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md) است. خلاصه:

```bash
python scripts/check_account.py --probe-order     # دو سفارش دور از بازار ثبت و لغو می‌کند؛ PROBE را تایپ کنید
make test && git status --porcelain               # باید خالی باشد
TAG=live-$(date -u +%Y%m%d-%H%M); git tag "$TAG" && git push origin "$TAG"
WTA_LIVE=1 python -m wta run --mode live --i-understand-live --overlay configs/live_halfsize.yaml --ai offline
```

kill-switch اضطراری: `touch KILL` در ریشه‌ی مخزن. این کار سفارش‌های ورود را لغو می‌کند و همه‌چیز را به تومان
برمی‌گرداند. فروش اضطراری همه‌چیز به تومان: `make flatten-live`.
