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
# 1) ساخت فایل env
cp .env.example .env
# مقدار LLM_API_KEY را در .env بگذارید

# 2) بالا آوردن همه سرویس‌ها
docker compose up -d --build

# 3) بررسی سلامت
curl http://localhost:8000/health        # سلامت بک‌اند
curl http://localhost:8000/health/llm    # تست اتصال به LLM Provider (معیار پذیرش M0)
# فرانت‌اند: http://localhost:3000
```

### اجرای لوکال بدون Docker (برای توسعه)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload

cd frontend
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
docker network create rag-net     # یک‌بار برای همیشه
make up-all                       # اول پایگاه دانش، بعد این پروژه
# یا: make up-all PAGEVAULT_DIR=/path/to/pagevault

make kb-check                     # آیا از داخل بک‌اند در دسترس است؟
curl localhost:8000/rag/health
```

تست بدون اجرای پایپ‌لاین:

```bash
# فقط بازیابی، بدون LLM — برای تشخیص اینکه مشکل از recall است یا از تولید
curl -X POST localhost:8000/rag/search -H 'content-type: application/json' \
  -d '{"query": "DMA registers on STM32F407"}'

# Datasheet Agent: بازیابی + پاسخ همراه با ارجاع
curl -X POST localhost:8000/rag/ask -H 'content-type: application/json' \
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
