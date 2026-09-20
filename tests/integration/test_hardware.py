"""硬件信息接口的安全回归测试。"""

from fastapi.testclient import TestClient

from contract_review_app.main import app
from contract_review_app.telemetry import hardware


client = TestClient(app)


def test_hardware_endpoint_does_not_expose_gpu_exception(monkeypatch):
    class BrokenNvml:
        def nvmlDeviceGetCount(self):
            raise RuntimeError("GPU driver secret")

    monkeypatch.setattr(hardware, "PYNVML_AVAILABLE", True)
    monkeypatch.setattr(hardware.GPUCollector, "_initialized", True)
    monkeypatch.setattr(hardware, "pynvml", BrokenNvml())
    monkeypatch.setattr(hardware.hardware_monitor, "collect_all", lambda: None)

    response = client.get("/api/v1/hardware")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["hardware"]["gpu_error"] == "GPU 信息暂不可用"
    assert "GPU driver secret" not in response.text
