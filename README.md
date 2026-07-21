# دستیار هوشمند مهندسی STM32

پلتفرم چند-ایجنتی (Multi-Agent) برای تولید، دیباگ، بهینه‌سازی و تست فرم‌ور STM32.

این ریپو خروجی **مایلستون M0** است: زیرساخت، اسکلت سرویس‌ها و CI.

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

> ⚠️ **نکته مهم:** مدل Embedding (`EMBEDDING_MODEL`) را قبل از شروع M2 قطعی کنید؛ تغییر آن بعد از ساخت کالکشن‌های Qdrant یعنی re-index کامل پایگاه دانش.

## ساختار پروژه

```
├── backend/
│   ├── app/
│   │   ├── main.py          # اپ FastAPI
│   │   ├── core/            # config + کارخانه کلاینت LLM
│   │   ├── api/routes/      # اندپوینت‌ها (فعلاً health)
│   │   ├── agents/          # ایجنت‌ها (M1+ — فعلاً خالی)
│   │   ├── workers/         # Celery
│   │   ├── db/              # SQLModel / انجین دیتابیس
│   │   └── rag/             # زیرساخت RAG (M2 — فعلاً خالی)
│   └── tests/
├── frontend/                # Next.js (داشبورد — M7 تکمیل می‌شود)
├── docs/                    # مستندات معماری
├── docker-compose.yml       # postgres + redis + qdrant + backend + worker + frontend (+ ollama)
└── .github/workflows/ci.yml # Lint + Test
```

## مایلستون‌ها

- [x] **M0** — زیرساخت و اسکلت پروژه (این ریپو)
- [ ] **M1** — ارکستریتور و مدیریت تسک‌ها (LangGraph + Celery + روتر)
- [ ] **M2** — پایگاه دانش و RAG (Qdrant + Ingestion)
- [ ] **M3** — ایجنت‌های تحلیل و طراحی
- [ ] **M4** — ایجنت‌های تولید کد + کامپایل ایزوله
- [ ] **M5** — ایجنت‌های کیفیت (Review / Debug / Optimize / Test)
- [ ] **M6** — مستندسازی و تحویل
- [ ] **M7** — داشبورد تعاملی
- [ ] **M8** — یکپارچه‌سازی نهایی و MVP
