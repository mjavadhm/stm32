# معماری سیستم (خلاصه)

> سند مرجع کامل: «نقشه راه و مستندات پیاده‌سازی دستیار هوشمند مهندسی STM32»

## جریان کلی

```
کاربر → Router (تشخیص نوع درخواست: End-to-End / Copilot)
      → LangGraph Workflow (گره‌ها = ایجنت‌ها)
      → Celery (پردازش‌های طولانی: کامپایل، شبیه‌سازی)
      → خروجی: پروژه کامل / گزارش دیباگ / گزارش بهینه‌سازی / تست‌ها
```

## نگاشت ایجنت‌ها به مایلستون‌ها

| ایجنت | مایلستون | ماژول هدف |
|---|---|---|
| Requirements | M3 | `app/agents/requirements_agent.py` |
| Architecture | M3 | `app/agents/architecture_agent.py` |
| Datasheet | M3 | `app/agents/datasheet_agent.py` |
| CubeMX | M4 | `app/agents/cubemx_agent.py` |
| Firmware | M4 | `app/agents/firmware_agent.py` |
| Review | M5 | `app/agents/review_agent.py` |
| Debug | M5 | `app/agents/debug_agent.py` |
| Optimization | M5 | `app/agents/optimization_agent.py` |
| Test | M5 | `app/agents/test_agent.py` |
| Docs | M6 | `app/agents/docs_agent.py` |

## تصمیمات کلیدی ثبت‌شده

1. **LLM Provider:** فعلاً آنلاین (OpenAI-compatible)، مهاجرت به Ollama فقط با تغییر `.env`.
2. **Embedding:** جدا از LLM کانفیگ می‌شود؛ باید قبل از شروع M2 قطعی شود (تغییر بعدی = re-index کامل Qdrant).
3. **خروجی ساخت‌یافته بین ایجنت‌ها:** JSON Schema + اعتبارسنجی Pydantic + retry (از M1 رعایت شود تا مهاجرت به مدل لوکال بی‌دردسر باشد).
4. **افزودن تدریجی ایجنت‌ها:** هر ایجنت تک‌به‌تک اضافه و ارزیابی می‌شود (طبق توصیه سند مرجع).
