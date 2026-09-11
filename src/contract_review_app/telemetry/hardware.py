"""硬件监控模块

提供系统硬件指标收集：
- CPU 使用率
- 内存使用情况
- 磁盘使用情况
- NVIDIA GPU 监控（温度、显存、利用率）
"""
import platform
import warnings
from typing import Optional

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, Info, REGISTRY, generate_latest

warnings.filterwarnings(
    "ignore",
    message="The pynvml package is deprecated. Please install nvidia-ml-py instead.",
    category=FutureWarning,
)

# 系统监控
try:
    import psutil
except ImportError:
    psutil = None

# GPU 监控
try:
    from nvidia import nvml
    pynvml = nvml
    PYNVML_AVAILABLE = True
except ImportError:
    try:
        import pynvml
        PYNVML_AVAILABLE = True
    except ImportError:
        pynvml = None
        PYNVML_AVAILABLE = False

# =============================================================================
# Prometheus 指标定义
# =============================================================================

# 系统信息
SYSTEM_INFO = Info(
    "contract_review_app_system",
    "System information"
)

# CPU 指标
CPU_USAGE = Gauge(
    "contract_review_app_cpu_usage_percent",
    "CPU usage percentage",
    ["core"]
)

CPU_USAGE_TOTAL = Gauge(
    "contract_review_app_cpu_usage_total_percent",
    "Total CPU usage percentage"
)

# 内存指标
MEMORY_TOTAL = Gauge(
    "contract_review_app_memory_total_bytes",
    "Total memory in bytes"
)

MEMORY_USED = Gauge(
    "contract_review_app_memory_used_bytes",
    "Used memory in bytes"
)

MEMORY_AVAILABLE = Gauge(
    "contract_review_app_memory_available_bytes",
    "Available memory in bytes"
)

MEMORY_USAGE_PERCENT = Gauge(
    "contract_review_app_memory_usage_percent",
    "Memory usage percentage"
)

# 磁盘指标
DISK_TOTAL = Gauge(
    "contract_review_app_disk_total_bytes",
    "Total disk space in bytes",
    ["mount_point"]
)

DISK_USED = Gauge(
    "contract_review_app_disk_used_bytes",
    "Used disk space in bytes",
    ["mount_point"]
)

DISK_FREE = Gauge(
    "contract_review_app_disk_free_bytes",
    "Free disk space in bytes",
    ["mount_point"]
)

DISK_USAGE_PERCENT = Gauge(
    "contract_review_app_disk_usage_percent",
    "Disk usage percentage",
    ["mount_point"]
)

# GPU 指标
GPU_INFO = Info(
    "contract_review_app_gpu",
    "GPU information"
)

GPU_COUNT = Gauge(
    "contract_review_app_gpu_count",
    "Number of available GPUs"
)

GPU_TEMPERATURE = Gauge(
    "contract_review_app_gpu_temperature_celsius",
    "GPU temperature in Celsius",
    ["gpu_id", "gpu_name"]
)

GPU_UTILIZATION = Gauge(
    "contract_review_app_gpu_utilization_percent",
    "GPU utilization percentage",
    ["gpu_id", "gpu_name"]
)

GPU_MEMORY_TOTAL = Gauge(
    "contract_review_app_gpu_memory_total_bytes",
    "GPU total memory in bytes",
    ["gpu_id", "gpu_name"]
)

GPU_MEMORY_USED = Gauge(
    "contract_review_app_gpu_memory_used_bytes",
    "GPU used memory in bytes",
    ["gpu_id", "gpu_name"]
)

GPU_MEMORY_FREE = Gauge(
    "contract_review_app_gpu_memory_free_bytes",
    "GPU free memory in bytes",
    ["gpu_id", "gpu_name"]
)

GPU_MEMORY_USAGE_PERCENT = Gauge(
    "contract_review_app_gpu_memory_usage_percent",
    "GPU memory usage percentage",
    ["gpu_id", "gpu_name"]
)

GPU_POWER_USAGE = Gauge(
    "contract_review_app_gpu_power_usage_watts",
    "GPU power usage in watts",
    ["gpu_id", "gpu_name"]
)

GPU_CLOCK_SM = Gauge(
    "contract_review_app_gpu_clock_sm_mhz",
    "GPU SM clock speed in MHz",
    ["gpu_id", "gpu_name"]
)

GPU_CLOCK_MEMORY = Gauge(
    "contract_review_app_gpu_clock_memory_mhz",
    "GPU memory clock speed in MHz",
    ["gpu_id", "gpu_name"]
)


# =============================================================================
# 系统信息收集
# =============================================================================

def collect_system_info():
    """收集系统基本信息"""
    SYSTEM_INFO.info({
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "hostname": platform.node(),
    })


def collect_cpu_metrics():
    """收集 CPU 指标"""
    if psutil is None:
        return

    # 每核心使用率
    per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)
    for i, usage in enumerate(per_cpu):
        CPU_USAGE.labels(core=str(i)).set(usage)

    # 总体使用率
    total_usage = psutil.cpu_percent(interval=0.1)
    CPU_USAGE_TOTAL.set(total_usage)


def collect_memory_metrics():
    """收集内存指标"""
    if psutil is None:
        return

    mem = psutil.virtual_memory()

    MEMORY_TOTAL.set(mem.total)
    MEMORY_USED.set(mem.used)
    MEMORY_AVAILABLE.set(mem.available)
    MEMORY_USAGE_PERCENT.set(mem.percent)


def collect_disk_metrics(mount_points: Optional[list] = None):
    """收集磁盘指标"""
    if psutil is None:
        return

    if mount_points is None:
        mount_points = ["/"]

    for mount_point in mount_points:
        try:
            disk = psutil.disk_usage(mount_point)
            DISK_TOTAL.labels(mount_point=mount_point).set(disk.total)
            DISK_USED.labels(mount_point=mount_point).set(disk.used)
            DISK_FREE.labels(mount_point=mount_point).set(disk.free)
            DISK_USAGE_PERCENT.labels(mount_point=mount_point).set(disk.percent)
        except Exception:
            pass


# =============================================================================
# GPU 信息收集
# =============================================================================

class GPUCollector:
    """GPU 指标收集器"""

    _initialized = False
    _handle = None

    @classmethod
    def initialize(cls):
        """初始化 NVML"""
        if not PYNVML_AVAILABLE:
            return False

        try:
            pynvml.nvmlInit()
            cls._initialized = True
            return True
        except Exception as e:
            print(f"GPU monitoring not available: {e}")
            return False

    @classmethod
    def collect_gpu_metrics(cls):
        """收集 GPU 指标"""
        if not cls._initialized:
            if not cls.initialize():
                return

        try:
            device_count = pynvml.nvmlDeviceGetCount()
            GPU_COUNT.set(device_count)

            driver_version = pynvml.nvmlSystemGetDriverVersion()
            cuda_version = pynvml.nvmlSystemGetCudaDriverVersion_v2()
            # CUDA 版本转换：12080 -> "12.0"
            cuda_version_str = f"{cuda_version // 1000}.{(cuda_version % 1000) // 10}"
            GPU_INFO.info({
                "driver_version": driver_version,
                "cuda_version": cuda_version_str,
            })

            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)

                # GPU 名称
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode('utf-8')
                gpu_id = str(i)

                # 温度
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    GPU_TEMPERATURE.labels(gpu_id=gpu_id, gpu_name=name).set(temp)
                except Exception:
                    GPU_TEMPERATURE.labels(gpu_id=gpu_id, gpu_name=name).set(0)

                # GPU 利用率
                try:
                    utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    GPU_UTILIZATION.labels(gpu_id=gpu_id, gpu_name=name).set(utilization.gpu)
                except Exception:
                    GPU_UTILIZATION.labels(gpu_id=gpu_id, gpu_name=name).set(0)

                # 显存信息
                try:
                    mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    GPU_MEMORY_TOTAL.labels(gpu_id=gpu_id, gpu_name=name).set(mem_info.total)
                    GPU_MEMORY_USED.labels(gpu_id=gpu_id, gpu_name=name).set(mem_info.used)
                    GPU_MEMORY_FREE.labels(gpu_id=gpu_id, gpu_name=name).set(mem_info.free)
                    mem_percent = (mem_info.used / mem_info.total * 100) if mem_info.total > 0 else 0
                    GPU_MEMORY_USAGE_PERCENT.labels(gpu_id=gpu_id, gpu_name=name).set(mem_percent)
                except Exception:
                    pass

                # 功耗
                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW to W
                    GPU_POWER_USAGE.labels(gpu_id=gpu_id, gpu_name=name).set(power)
                except Exception:
                    GPU_POWER_USAGE.labels(gpu_id=gpu_id, gpu_name=name).set(0)

                # 时钟频率
                try:
                    clock_sm = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                    clock_mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                    GPU_CLOCK_SM.labels(gpu_id=gpu_id, gpu_name=name).set(clock_sm)
                    GPU_CLOCK_MEMORY.labels(gpu_id=gpu_id, gpu_name=name).set(clock_mem)
                except Exception:
                    pass

        except Exception as e:
            print(f"Error collecting GPU metrics: {e}")


# =============================================================================
# 硬件监控器
# =============================================================================

class HardwareMonitor:
    """硬件监控器，定期收集硬件指标"""

    def __init__(self, interval: int = 15):
        """
        Args:
            interval: 采集间隔（秒）
        """
        self.interval = interval
        self._running = False

    def collect_all(self):
        """收集所有硬件指标"""
        collect_system_info()
        collect_cpu_metrics()
        collect_memory_metrics()
        collect_disk_metrics()
        GPUCollector.collect_gpu_metrics()

    def get_metrics_text(self) -> bytes:
        """获取 Prometheus 格式的指标"""
        return generate_latest(REGISTRY)

    @property
    def content_type(self) -> str:
        """获取 Content-Type"""
        return CONTENT_TYPE_LATEST


# 全局硬件监控器
hardware_monitor = HardwareMonitor()


# =============================================================================
# 便捷函数
# =============================================================================

def get_hardware_summary() -> dict:
    """获取硬件摘要信息"""
    summary = {
        "system": {
            "platform": platform.system(),
            "hostname": platform.node(),
        },
        "cpu": {},
        "memory": {},
        "gpu": [],
        "disk": {},
    }

    # CPU 信息
    if psutil:
        summary["cpu"]["count"] = psutil.cpu_count(logical=True)
        summary["cpu"]["physical_count"] = psutil.cpu_count(logical=False)
        summary["cpu"]["usage_percent"] = psutil.cpu_percent(interval=0.1)

        # 每核心使用率
        per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)
        summary["cpu"]["per_core_usage"] = per_cpu

        # CPU 频率
        try:
            cpu_freq = psutil.cpu_freq()
            if cpu_freq:
                summary["cpu"]["frequency_mhz"] = {
                    "current": cpu_freq.current,
                    "min": cpu_freq.min,
                    "max": cpu_freq.max,
                }
        except Exception:
            pass

    # 内存信息
    if psutil:
        mem = psutil.virtual_memory()
        summary["memory"] = {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "usage_percent": mem.percent,
        }

        # Swap 信息
        swap = psutil.swap_memory()
        summary["memory"]["swap"] = {
            "total_gb": round(swap.total / (1024**3), 2),
            "used_gb": round(swap.used / (1024**3), 2),
            "usage_percent": swap.percent,
        }

    # GPU 信息
    if PYNVML_AVAILABLE and GPUCollector._initialized:
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode('utf-8')

                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = 0

                try:
                    utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    gpu_util = utilization.gpu
                    mem_util = utilization.memory
                except Exception:
                    gpu_util = 0
                    mem_util = 0

                try:
                    mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    mem_total_gb = round(mem_info.total / (1024**3), 2)
                    mem_used_gb = round(mem_info.used / (1024**3), 2)
                    mem_free_gb = round(mem_info.free / (1024**3), 2)
                except Exception:
                    mem_total_gb = 0
                    mem_used_gb = 0
                    mem_free_gb = 0

                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                except Exception:
                    power = 0

                try:
                    clock_sm = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                    clock_mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                except Exception:
                    clock_sm = 0
                    clock_mem = 0

                gpu_info = {
                    "id": i,
                    "name": name,
                    "temperature_celsius": temp,
                    "utilization_percent": gpu_util,
                    "memory": {
                        "total_gb": mem_total_gb,
                        "used_gb": mem_used_gb,
                        "free_gb": mem_free_gb,
                        "usage_percent": round(mem_util, 1),
                    },
                    "power_watts": power,
                    "clock_sm_mhz": clock_sm,
                    "clock_memory_mhz": clock_mem,
                }

                summary["gpu"].append(gpu_info)
        except Exception as e:
            summary["gpu_error"] = str(e)

    # 磁盘信息
    if psutil:
        disk = psutil.disk_usage("/")
        summary["disk"] = {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "usage_percent": disk.percent,
        }

    return summary
