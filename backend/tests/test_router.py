from app.agents.router import classify_by_rules
from app.db.models import RequestType


def test_default_is_full_project() -> None:
    assert (
        classify_by_rules("Read MPU6050 over SPI with DMA on STM32F407")
        == RequestType.full_project
    )


def test_debug_detection() -> None:
    assert classify_by_rules("Why do I get a HardFault after boot?") == RequestType.debug


def test_optimize_detection_fa() -> None:
    assert (
        classify_by_rules("این تابع را از نظر مصرف حافظه بهینه کن") == RequestType.optimize
    )


def test_test_detection_fa() -> None:
    assert classify_by_rules("برای این درایور تست بنویس") == RequestType.test
