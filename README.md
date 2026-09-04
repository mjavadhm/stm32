# دستیار هوشمند مهندسی STM32

پلتفرم چند-ایجنتی (Multi-Agent) برای تولید، دیباگ، بهینه‌سازی و تست فرم‌ور STM32.

وضعیت فعلی: **M0 تا M3** — زیرساخت، ارکستریتور، اتصال پایگاه دانش، و ایجنت‌های تحلیل و طراحی.

## پشته فناوری

| لایه | ابزار |
|---|---|
| Backend / Orchestration | Python + FastAPI + LangGraph |
| صف پردازش | Celery + Redis |
| دیتابیس | PostgreSQL (SQLModel) |
| پایگاه دانش برداری | Qdrant |
| LLM | هر Provider سازگار با OpenAI API (فعلاً آنلاین — بعداً Ollama فقط با تغییر `LLM_BASE_URL`) |
| Frontend | Next.js (App Router) |

## راه‌اندازی سریع

```bash
# 1) ساخت فایل env و گذاشتن LLM_API_KEY در آن
cp .env.example .env

# 2) یک دستور، همین
./run.sh
```

`run.sh` تکرارپذیر (idempotent) است — هم اجرای اول و هم هر بار بعدی. چهار کاری که
`docker compose up` تنهایی انجام نمی‌دهد و تا امروز باید با خطا خوردن کشفشان می‌کردید:

1. شبکه‌ی external به نام `rag-net` را می‌سازد؛ بدون آن compose از همان ثانیه‌ی اول fail می‌کند.
2. ایمیج toolchain را **قبل از** بقیه می‌سازد، چون `backend` به healthy شدنش وابسته است.
   حدود ۱ گیگابایت، تنها مرحله‌ای که به اینترنت نیاز دارد، و در اجرای اول ۱۰ تا ۲۵ دقیقه.
3. جدول‌های پین وندور را import می‌کند (`build_devices`) — بی آن، اعتبارسنجی پین و AF
   هیچ داده‌ای برای مقایسه ندارد.
4. دو تنظیمی را که با جابه‌جا شدن پورت‌ها بی‌صدا UI را می‌شکنند چک می‌کند:
   `NEXT_PUBLIC_API_URL` و `CORS_ORIGINS`.

```bash
./run.sh --no-kb     # بدون پایگاه دانش PageVault
./run.sh --status    # چه چیزی بالاست و روی چه آدرسی
./run.sh --down      # خواباندن این استک
```

### آدرس‌ها

| سرویس | آدرس |
|---|---|
| داشبورد و چت | <http://localhost:19300> |
| API بک‌اند | <http://localhost:19800> |
| PageVault API | <http://localhost:19100> |

```bash
curl http://localhost:19800/health        # سلامت بک‌اند
curl http://localhost:19800/health/llm    # تست اتصال به LLM Provider (معیار پذیرش M0)
```

پورت‌های میزبان عمداً در بازه‌ی ۱۹xxx هستند: IANA این بازه را به چیزی اختصاص نداده و
لینوکس پورت‌های ephemeral را از ۳۲۷۶۸ به بالا برمی‌دارد، پس با Postgres یا Redis‌ای که
از قبل روی سرور بالاست تصادم نمی‌کنند. پورت‌های **داخلی** کانتینرها دست‌نخورده‌اند، یعنی
`DATABASE_URL` و `REDIS_URL` و `PAGEVAULT_URL` هیچ‌وقت عوض نمی‌شوند. برای جابه‌جا کردن
پورت‌ها بلوک «Host ports» در `.env.example` را ببینید.

Postgres و Redis و Qdrant فقط روی loopback باز می‌شوند، چون `POSTGRES_PASSWORD` به‌طور
پیش‌فرض `stm32ai` است. اگر واقعاً به دسترسی از بیرون نیاز داشتید، **اول رمز را عوض کنید**
و بعد `INFRA_BIND=0.0.0.0` بگذارید.

> اگر UI را از مرورگری غیر از همین ماشین باز می‌کنید، `NEXT_PUBLIC_API_URL` باید آدرسی
> باشد که **مرورگر کاربر** می‌بیند (نه `localhost` و نه نام کانتینر)، و `CORS_ORIGINS`
> هم باید همان مبدأ را داشته باشد. `run.sh` هر دو را چک می‌کند و هشدار می‌دهد.

### اجرای لوکال بدون Docker (برای توسعه)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload

cd ../frontend
npm install && npm run dev
```

## سوییچ به Ollama (بعداً)

فقط `.env` را عوض کنید — هیچ کدی تغییر نمی‌کند:

```env
LLM_BASE_URL=http://ollama:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5-coder:14b
```

و سرویس Ollama را (که زیر پروفایل `local` تعریف شده) بالا بیاورید:

```bash
docker compose --profile local up -d ollama
```

> ⚠️ **نکته مهم:** مدل Embedding را قبل از ingest انبوه قطعی کنید؛ تغییر آن بعد از ساخت کالکشن‌ها یعنی re-index کامل پایگاه دانش.

## پایگاه دانش (M2)

بازیابی در این ریپو پیاده‌سازی نشده؛ توسط **PageVault** انجام می‌شود — یک سرویس اپن‌سورس مستقل که در استک داکر خودش بالا می‌آید و از طریق HTTP روی شبکهٔ مشترک `rag-net` صدا زده می‌شود.

```bash
./run.sh                          # خودش rag-net را می‌سازد و PageVault را هم بالا می‌آورد
# یا دستی: make up-all PAGEVAULT_DIR=/path/to/pagevault

make kb-check                     # آیا از داخل بک‌اند در دسترس است؟
curl localhost:19800/rag/health
```

اگر `../pagevault` وجود نداشته باشد، `run.sh` هشدار می‌دهد و ادامه می‌دهد: چت و RAG کار
می‌کنند ولی پاسخ‌ها بدون ارجاع و «unverified» هستند.

تست بدون اجرای پایپ‌لاین:

```bash
# فقط بازیابی، بدون LLM — برای تشخیص اینکه مشکل از recall است یا از تولید
curl -X POST localhost:19800/rag/search -H 'content-type: application/json' \
  -d '{"query": "DMA registers on STM32F407"}'

# Datasheet Agent: بازیابی + پاسخ همراه با ارجاع
curl -X POST localhost:19800/rag/ask -H 'content-type: application/json' \
  -d '{"question": "How do I use HAL_SPI_Transmit with DMA on STM32F407?"}'
```

چرایی دو استک جدا به‌جای یکی، و قرارداد نوشتن ایجنت‌های مبتنی بر ارجاع: [`docs/knowledge-base.md`](docs/knowledge-base.md)

## کیفیت و ارزیابی

```bash
make test        # تست‌های آفلاین (بدون LLM و بدون PageVault)
make lint        # ruff
make kb-probe    # آیا بازیابی *با فیلتر family* هم چیزی برمی‌گرداند؟
make eval        # اعداد کیفیت پایپ‌لاین طراحی (نیازمند LLM و PageVault زنده)
```

`make eval` روی سناریوهای [`backend/evals/cases.json`](backend/evals/cases.json) اجرا می‌شود و نرخ ارجاع‌دهی، ارجاع‌های ساختگی و تأخیر هر مرحله را گزارش می‌کند؛ خروجی هر اجرا در `backend/evals/results/` ذخیره می‌شود تا اثر تغییر پرامپت قابل مقایسه باشد.

بازبینی کیفیت کد تا انتهای M3 و کارهای باقی‌مانده: [`docs/quality-review.md`](docs/quality-review.md)

## ساختار پروژه

```
├── backend/
│   ├── app/
│   │   ├── main.py          # اپ FastAPI
│   │   ├── core/            # config + کارخانه کلاینت LLM
│   │   ├── api/routes/      # health / projects / agents / rag
│   │   ├── agents/          # router, requirements, datasheet, architecture
│   │   ├── orchestrator/    # گراف LangGraph + قراردادهای بین ایجنت‌ها
│   │   ├── workers/         # Celery
│   │   ├── db/              # SQLModel / انجین دیتابیس
│   │   └── rag/             # کلاینت HTTP پایگاه دانش (PageVault)
│   ├── tests/
│   ├── scripts/             # ابزارهای تشخیصی (kb_probe)
│   └── evals/               # سنجش کیفیت پایپ‌لاین طراحی
├── frontend/                # Next.js (داشبورد — M7 تکمیل می‌شود)
├── deploy/                  # override اتصال PageVault به شبکهٔ rag-net
├── docs/                    # معماری + پایگاه دانش + پلن M3/M4 + بازبینی کیفیت
├── docker-compose.yml       # postgres + redis + qdrant + backend + worker + frontend (+ ollama)
└── .github/workflows/ci.yml # Lint + Test
```

## مایلستون‌ها

- [x] **M0** — زیرساخت و اسکلت پروژه (این ریپو)
- [x] **M1** — ارکستریتور و مدیریت تسک‌ها (LangGraph + Celery + روتر)
- [ ] **M2** — پایگاه دانش و RAG — از طریق [PageVault](https://github.com/mjavadhm/pagevault) (اتصال برقرار است؛ باقی‌مانده: ingest مستندات و ارزیابی recall)
- [x] **M3** — ایجنت‌های تحلیل و طراحی ([پلن](docs/m3-plan.md))
- [ ] **M4** — ایجنت‌های تولید کد + کامپایل ایزوله ([پلن](docs/m4-plan.md))
- [ ] **M5** — ایجنت‌های کیفیت (Review / Debug / Optimize / Test)
- [ ] **M6** — مستندسازی و تحویل
- [ ] **M7** — داشبورد تعاملی
- [ ] **M8** — یکپارچه‌سازی نهایی و MVP
