"""按需启停的本机 Qwen Tesla T4 推理 Provider。"""
from __future__ import annotations

import csv
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..core.errors import BackendNotInstalledError, BackendOfflineError
from ..core.models import ModelInfo, OperationResult, RunningModel
from ..core.processes import ManagedProcess, ProcessSupervisor
from .base import (
    CancellationToken,
    Capability,
    CapabilityState,
    LoopbackHTTPClient,
    Provider,
    ProviderAPIError,
    ProviderStatus,
    check_cancelled,
    find_executable,
)

DEFAULT_T4_URL = "http://127.0.0.1:8712"
# 启停脚本路径不内置具体机器路径，由用户通过环境变量指定；未配置时为空，不会执行任何脚本。
DEFAULT_T4_START_SCRIPT = os.environ.get("LLM_MANAGER_T4_START_SCRIPT", "")
DEFAULT_T4_STOP_SCRIPT = os.environ.get("LLM_MANAGER_T4_STOP_SCRIPT", "")
DEFAULT_T4_MODEL = "Qwen2.5-7B-Instruct"
ServiceProgress = Callable[[dict[str, Any]], None]


class T4Provider(Provider):
    """只把健康且由用户显式启动的固定服务作为 T4 模型。"""

    name = "t4"
    display_name = "Qwen 本机双 T4"

    def __init__(
        self,
        base_url: str = DEFAULT_T4_URL,
        *,
        timeout: float = 5.0,
        generate_timeout: float = 300.0,
        start_script: str | os.PathLike[str] = DEFAULT_T4_START_SCRIPT,
        stop_script: str | os.PathLike[str] = DEFAULT_T4_STOP_SCRIPT,
        model_name: str = DEFAULT_T4_MODEL,
        supervisor: ProcessSupervisor | None = None,
        powershell_path: str | os.PathLike[str] | None = None,
        nvidia_smi_path: str | os.PathLike[str] | None = None,
        gpu_timeout: float = 5.0,
        stop_timeout: float = 60.0,
    ):
        self._client = LoopbackHTTPClient(self.name, base_url, timeout)
        self.base_url = self._client.base_url
        self.generate_timeout = float(generate_timeout)
        self.gpu_timeout = float(gpu_timeout)
        self.stop_timeout = float(stop_timeout)
        if min(self.generate_timeout, self.gpu_timeout, self.stop_timeout) <= 0:
            raise ValueError("生成、显存查询和停止超时必须大于 0 秒")
        self.start_script = str(Path(start_script).expanduser()) if str(start_script or "").strip() else ""
        self.stop_script = str(Path(stop_script).expanduser()) if str(stop_script or "").strip() else ""
        self.model_name = str(model_name).strip()
        if not self.model_name:
            raise ValueError("T4 固定模型名称不能为空")
        self._supervisor = supervisor or ProcessSupervisor()
        self._owns_supervisor = supervisor is None
        self._powershell_path = os.fspath(powershell_path) if powershell_path else ""
        self._nvidia_smi_path = os.fspath(nvidia_smi_path) if nvidia_smi_path else ""
        self._last_status: ProviderStatus | None = None
        self._records: dict[str, ManagedProcess] = {}
        self._records_lock = threading.RLock()
        self._service_lock = threading.RLock()

    @staticmethod
    def _progress(callback: ServiceProgress | None, phase: str, message: str, **data: Any) -> None:
        if callback is not None:
            callback({"phase": phase, "message": message, **data})

    @staticmethod
    def _ready(payload: dict[str, Any]) -> bool:
        for key in ("ok", "ready", "healthy"):
            if key in payload:
                value = payload[key]
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "ok", "ready", "healthy", "yes"}
                return value is True
        status = str(payload.get("status") or payload.get("state") or "").strip().lower()
        if status:
            return status in {"ok", "ready", "healthy", "running", "success", "up"}
        # 没有否定字段时，成功返回的专用 /health 即表示就绪。
        return True

    @staticmethod
    def _reported_gpu(payload: dict[str, Any]) -> str:
        value = payload.get("gpu") or payload.get("gpu_name") or payload.get("device_name") or ""
        return str(value).strip()

    @classmethod
    def _unsupported_gpu(cls, payload: dict[str, Any]) -> str:
        reported = cls._reported_gpu(payload)
        if reported and "t4" not in reported.casefold():
            return reported
        return ""

    def health(self) -> dict[str, Any]:
        """只读取回环 /health；从不调用启动器。"""
        payload = self._client.request_json("GET", "/health")
        if not isinstance(payload, dict):
            raise ProviderAPIError("T4 /health 响应不是 JSON 对象", provider=self.name, payload=payload)
        return payload

    def _fallback_details(self, running: bool) -> dict[str, Any]:
        return {
            "default_provider": "ollama",
            "only_when_ollama_unavailable": True,
            "automatic_start": False,
            "manual_start_required": not running,
            "action": "" if running else "NEEDS_START",
            "start_script": self.start_script,
            "stop_script": self.stop_script,
            "start_script_exists": Path(self.start_script).is_file(),
            "stop_script_exists": Path(self.stop_script).is_file(),
        }

    def _manual_start_message(self) -> str:
        return (
            f"T4 服务未就绪。请由用户手动调用 start_service 或执行 {self.start_script}；"
            "检测和回退不会自动启动 T4。"
        )

    def detect(self) -> ProviderStatus:
        start_exists = Path(self.start_script).is_file()
        gpu = self.gpu_status()
        details: dict[str, Any] = {"gpu": gpu, "fallback": self._fallback_details(False)}
        try:
            payload = self.health()
            details["health_response"] = payload
            if not self._ready(payload):
                raise ProviderAPIError(
                    f"T4 /health 明确报告未就绪：{payload}", provider=self.name, payload=payload
                )
            unsupported_gpu = self._unsupported_gpu(payload)
            if unsupported_gpu:
                status = ProviderStatus(
                    provider=self.name,
                    installed=True,
                    running=False,
                    state=CapabilityState.UNSUPPORTED,
                    executable=self.start_script if start_exists else "",
                    base_url=self.base_url,
                    message=f"健康接口报告的显卡为 {unsupported_gpu}，不是 Tesla T4；拒绝用于推理。",
                    error="T4 Provider 只允许 Tesla T4，GPU0/GTX1080 被排除。",
                    details=details,
                )
            else:
                status = ProviderStatus(
                    provider=self.name,
                    installed=True,
                    running=True,
                    state=CapabilityState.SUPPORTED,
                    executable=self.start_script if start_exists else "",
                    base_url=self.base_url,
                    message="T4 本机服务已就绪；仅作为 Ollama 不可用时的显式回退。",
                    details=details,
                )
        except (BackendOfflineError, ProviderAPIError) as exc:
            status = ProviderStatus(
                provider=self.name,
                installed=start_exists,
                running=False,
                state=CapabilityState.NOT_RUNNING if start_exists else CapabilityState.NOT_INSTALLED,
                executable=self.start_script if start_exists else "",
                base_url=self.base_url,
                message=self._manual_start_message(),
                error=str(exc),
                details=details,
            )
        status.details["fallback"] = self._fallback_details(status.running)
        self._last_status = status
        return status

    def _powershell(self) -> str:
        executable = (
            find_executable(self._powershell_path)
            if self._powershell_path else find_executable(None, "powershell.exe", "powershell")
        )
        if not executable:
            raise BackendNotInstalledError("找不到 powershell.exe，无法执行用户指定的 T4 启停脚本。")
        return executable

    def capabilities(self) -> dict[str, Capability]:
        status = self._last_status or self.detect()
        running_state = CapabilityState.SUPPORTED if status.running else status.state
        start_exists = Path(self.start_script).is_file()
        stop_exists = Path(self.stop_script).is_file()
        try:
            self._powershell()
            powershell_exists = True
        except BackendNotInstalledError:
            powershell_exists = False
        start_state = (
            CapabilityState.SUPPORTED if start_exists and powershell_exists
            else CapabilityState.NOT_INSTALLED
        )
        stop_state = (
            CapabilityState.SUPPORTED if stop_exists and powershell_exists
            else CapabilityState.NOT_INSTALLED
        )
        unsupported_lifecycle = Capability(
            CapabilityState.UNSUPPORTED,
            "T4 是固定服务，不按 Ollama 模型装入/卸载；请显式调用 start_service/stop_service。",
        )
        return {
            "detect": Capability(CapabilityState.SUPPORTED, "只读取本机 /health，不启动服务。"),
            "health": Capability(CapabilityState.SUPPORTED, "只读取本机 /health。"),
            "list": Capability(running_state, status.message),
            "ps": Capability(running_state, status.message),
            "chat": Capability(running_state, status.message),
            "generate": Capability(running_state, status.message),
            "start_service": Capability(
                start_state,
                "仅由用户显式调用，使用受管 PowerShell 参数数组。" if start_state == CapabilityState.SUPPORTED
                else "启动脚本或 powershell.exe 不存在。",
                self.start_script,
            ),
            "stop_service": Capability(
                stop_state,
                "执行指定停止脚本并独立查询 T4 显存。" if stop_state == CapabilityState.SUPPORTED
                else "停止脚本或 powershell.exe 不存在。",
                self.stop_script,
            ),
            "gpu_status": Capability(
                CapabilityState.SUPPORTED,
                "仅报告 nvidia-smi 中索引非 0 且名称为 Tesla T4 的显卡。",
            ),
            "load": unsupported_lifecycle,
            "unload": unsupported_lifecycle,
            "pull": Capability(CapabilityState.UNSUPPORTED, "T4 固定本地模型不提供下载。"),
            "delete": Capability(CapabilityState.UNSUPPORTED, "T4 Provider 不删除模型文件。"),
            "install": Capability(CapabilityState.UNSUPPORTED, "T4 Provider 不安装服务或注册自启。"),
        }

    def _model_info(self, payload: dict[str, Any]) -> ModelInfo:
        model_ref = str(payload.get("model") or payload.get("model_name") or self.model_name).strip()
        display_name = Path(model_ref).name or self.model_name
        return ModelInfo(
            file_path=f"t4://{display_name}",
            file_name=display_name,
            format="unknown",
            size_bytes=0,
            architecture="qwen2",
            quant="4bit",
            provider=self.name,
            model_ref=model_ref,
            model_id=f"t4:{model_ref}",
            display_name=display_name,
            source_type="backend",
            integrity="unknown",
        )

    def _ready_model(self) -> tuple[ModelInfo, dict[str, Any]]:
        payload = self.health()
        if not self._ready(payload):
            raise BackendOfflineError(self._manual_start_message())
        unsupported_gpu = self._unsupported_gpu(payload)
        if unsupported_gpu:
            raise ProviderAPIError(
                f"T4 健康接口报告非 Tesla T4 显卡 {unsupported_gpu}，拒绝推理。",
                provider=self.name,
                payload=payload,
            )
        return self._model_info(payload), payload

    def list_models(self) -> list[ModelInfo]:
        model, _ = self._ready_model()
        return [model]

    def running_models(self) -> list[RunningModel]:
        model, payload = self._ready_model()
        return [RunningModel(
            name=model.model_ref,
            provider=self.name,
            details={
                "source": "/health",
                "confidence": "exact",
                "health_response": payload,
                "gpu": self.gpu_status(),
            },
        )]

    ps = running_models

    def show_model(self, model: str) -> dict[str, Any]:
        info, payload = self._ready_model()
        accepted = {info.model_ref, info.file_name, self.model_name}
        if model not in accepted:
            raise ProviderAPIError(f"T4 固定服务未报告模型 {model}", provider=self.name)
        return {**payload, "model": info.model_ref, "provider": self.name}

    def load_model(self, model: str, **options: Any) -> OperationResult:
        self._unsupported("模型装入", "T4 只能由用户显式调用 start_service，不能由通用装入流程自动启动")

    def unload_model(self, model: str, **options: Any) -> OperationResult:
        self._unsupported("模型卸载", "请由用户显式调用 stop_service 停止固定 T4 服务")

    def _powershell_command(self, script: str) -> list[str]:
        path = Path(script).expanduser()
        if not path.is_file():
            raise BackendNotInstalledError(f"T4 启停脚本不存在：{path}")
        return [self._powershell(), "-NoProfile", "-File", str(path.resolve())]

    def _start_script(self, script: str, purpose: str) -> ManagedProcess:
        record = self._supervisor.start(
            self._powershell_command(script),
            provider=self.name,
            model_id=self.model_name,
            purpose=purpose,
        )
        with self._records_lock:
            self._records[record.id] = record
        return record

    def _process_data(self, record: ManagedProcess, exit_code: int | None = None) -> dict[str, Any]:
        logs = self._supervisor.read_logs(record, max_lines=1000)
        return {
            "process": record.to_dict(),
            "exit_code": record.exit_code if exit_code is None else exit_code,
            "logs": logs,
            "script_output": "\n".join(logs),
        }

    def start_service(
        self,
        token: CancellationToken | None = None,
        progress: ServiceProgress | None = None,
        wait_ready: bool = True,
        timeout_seconds: float = 120,
    ) -> OperationResult:
        """TaskManager 入口；只有此显式调用会执行启动脚本。"""
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("启动等待超时必须大于 0 秒")
        with self._service_lock:
            check_cancelled(token)
            try:
                payload = self.health()
                if self._ready(payload):
                    unsupported_gpu = self._unsupported_gpu(payload)
                    if unsupported_gpu:
                        message = f"健康接口报告非 Tesla T4 显卡 {unsupported_gpu}，拒绝启动或使用。"
                        return OperationResult(False, message, data={"ready": False}, error=message)
                    self._progress(progress, "completed", "T4 已经就绪，无需重复启动。")
                    return OperationResult(
                        True,
                        "T4 服务已经就绪，未重复执行启动脚本。",
                        data={"already_running": True, "ready": True, "gpu": self.gpu_status()},
                    )
            except (BackendOfflineError, ProviderAPIError):
                pass

            self._progress(progress, "launching", "正在执行用户指定的 T4 启动脚本。")
            record = self._start_script(self.start_script, "t4-service")
            if not wait_ready:
                return OperationResult(
                    True,
                    "已发起 T4 启动脚本；按调用要求未等待健康就绪，不能视为模型已可用。",
                    data={"ready": False, "wait_ready": False, **self._process_data(record)},
                )

            deadline = time.monotonic() + timeout
            last_error = ""
            while True:
                check_cancelled(token)
                if not record.running:
                    exit_code = record.wait(timeout=0)
                    try:
                        ready = self._ready(self.health())
                    except (BackendOfflineError, ProviderAPIError) as exc:
                        ready = False
                        last_error = str(exc)
                    data = {
                        "ready": bool(ready and exit_code == 0),
                        "health_error": last_error,
                        **self._process_data(record, exit_code),
                    }
                    if exit_code == 0 and ready:
                        self._progress(progress, "completed", "T4 启动脚本正常退出且健康检查通过。")
                        return OperationResult(True, "T4 服务已就绪，启动脚本返回码为 0。", data=data)
                    message = (
                        f"T4 启动脚本已退出，返回码 {exit_code}；健康检查未确认就绪，不能视为启动成功。"
                    )
                    self._progress(progress, "failed", message, exit_code=exit_code)
                    return OperationResult(False, message, data=data, error=last_error or message)

                try:
                    payload = self.health()
                    if self._ready(payload):
                        # 健康与退出状态都必须是同一时刻的真实结果。
                        if not record.running:
                            continue
                        unsupported_gpu = self._unsupported_gpu(payload)
                        if unsupported_gpu:
                            message = f"T4 健康接口报告非 Tesla T4 显卡 {unsupported_gpu}，拒绝视为启动成功。"
                            return OperationResult(
                                False, message, data=self._process_data(record), error=message
                            )
                        self._progress(progress, "completed", "T4 健康检查通过，受管启动脚本仍在运行。")
                        return OperationResult(
                            True,
                            "T4 服务已就绪；启动脚本仍由 ProcessSupervisor 管理。",
                            data={
                                "ready": True,
                                "health_response": payload,
                                "gpu": self.gpu_status(),
                                **self._process_data(record),
                            },
                        )
                except (BackendOfflineError, ProviderAPIError) as exc:
                    last_error = str(exc)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    message = (
                        f"等待 T4 就绪超过 {timeout:g} 秒；启动脚本仍在运行，未伪装为成功。"
                        f"最后健康状态：{last_error or '未就绪'}"
                    )
                    self._progress(progress, "failed", message)
                    return OperationResult(
                        False,
                        message,
                        data={
                            "ready": False,
                            "timed_out": True,
                            "health_error": last_error,
                            **self._process_data(record),
                        },
                        error=last_error or message,
                    )
                self._progress(progress, "waiting", "T4 模型仍在加载，等待 /health 就绪。")
                time.sleep(min(0.5, remaining))

    def stop_service(
        self,
        token: CancellationToken | None = None,
        progress: ServiceProgress | None = None,
    ) -> OperationResult:
        """执行用户指定停止脚本，显存只采信独立 nvidia-smi 查询。"""
        with self._service_lock:
            check_cancelled(token)
            self._progress(progress, "stopping", "正在执行用户指定的 T4 停止脚本。")
            record = self._start_script(self.stop_script, "t4-stop")
            deadline = time.monotonic() + self.stop_timeout
            while True:
                check_cancelled(token)
                try:
                    exit_code = record.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        self._supervisor.stop(record)
                        message = "T4 停止脚本超时，已仅回收本工具创建的脚本进程。"
                        return OperationResult(
                            False,
                            message,
                            data={"timed_out": True, "gpu": self.gpu_status(), **self._process_data(record)},
                            error=message,
                        )
                    self._progress(progress, "stopping", "等待 T4 停止脚本真实退出。")

            logs = self._supervisor.read_logs(record, max_lines=1000)
            gpu = self.gpu_status()
            health_error = ""
            try:
                service_running: bool | None = self._ready(self.health())
            except BackendOfflineError:
                service_running = False
            except ProviderAPIError as exc:
                service_running = None
                health_error = str(exc)
            check_cancelled(token)
            success = exit_code == 0 and service_running is False
            if success:
                message = f"T4 停止脚本返回码为 0，/health 已不可访问；{self._gpu_summary(gpu)}"
            elif service_running:
                message = f"停止脚本返回码为 {exit_code}，但 /health 仍就绪，不能视为已停止。"
            elif service_running is None:
                message = f"停止脚本返回码为 {exit_code}，停止后的健康状态无法确认：{health_error}"
            else:
                message = f"T4 停止脚本返回非零返回码 {exit_code}；{self._gpu_summary(gpu)}"
            data = {
                "service_running": service_running,
                "stop_confirmed": success,
                "health_error": health_error,
                "gpu": gpu,
                "memory": gpu,
                **self._process_data(record, exit_code),
            }
            self._progress(progress, "completed" if success else "failed", message)
            return OperationResult(success, message, data=data, error="" if success else message)

    @staticmethod
    def _gpu_summary(gpu: dict[str, Any]) -> str:
        rows = gpu.get("gpus", [])
        if rows:
            return "；".join(
                f"GPU{row['index']} {row['name']} 已用 {row['memory_used_mib']} MiB / "
                f"总计 {row['memory_total_mib']} MiB，剩余 {row['memory_free_mib']} MiB"
                for row in rows
            )
        return str(gpu.get("error") or gpu.get("message") or "未获得 Tesla T4 显存数据")

    def _nvidia_smi(self) -> str:
        if self._nvidia_smi_path:
            return find_executable(self._nvidia_smi_path)
        return find_executable(None, "nvidia-smi", "nvidia-smi.exe")

    def gpu_status(self) -> dict[str, Any]:
        """返回真实 nvidia-smi 数据；GPU0 与非 Tesla T4 只列入排除项。"""
        result: dict[str, Any] = {
            "success": False,
            "source": "nvidia-smi",
            "gpus": [],
            "excluded": [],
            "warnings": [],
            "error": "",
            "policy": "仅允许索引非 0 且名称精确为 Tesla T4 的显卡；明确排除 GPU0/GTX1080。",
        }
        executable = self._nvidia_smi()
        if not executable:
            result["error"] = "找不到 nvidia-smi，无法确认 Tesla T4 显存。"
            return result
        command = [
            executable,
            "--query-gpu=index,name,memory.total,memory.used,memory.free",
            "--format=csv,noheader,nounits",
        ]
        kwargs: dict[str, Any] = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.gpu_timeout,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"nvidia-smi 调用失败：{exc}"
            return result
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"返回码 {completed.returncode}"
            result["error"] = f"nvidia-smi 未返回显卡数据：{detail}"
            return result

        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                index_text, name, total, used, free = next(csv.reader([line], skipinitialspace=True))
                index = int(index_text.strip())
                name = name.strip()
                row = {
                    "index": index,
                    "name": name,
                    "memory_total_mib": int(float(total.strip())),
                    "memory_used_mib": int(float(used.strip())),
                    "memory_free_mib": int(float(free.strip())),
                }
                for key in ("total", "used", "free"):
                    row[f"memory_{key}_bytes"] = row[f"memory_{key}_mib"] * 1024 * 1024
            except (ValueError, csv.Error) as exc:
                result["warnings"].append(f"无法解析 nvidia-smi 行 {line!r}：{exc}")
                continue
            excluded_reason = ""
            if index == 0:
                excluded_reason = "GPU0 禁止使用（本机为 GTX1080 时也必须排除）"
            elif "gtx1080" in name.casefold().replace(" ", ""):
                excluded_reason = "GTX1080 禁止使用"
            elif name.casefold() != "tesla t4":
                excluded_reason = "仅允许 Tesla T4"
            if excluded_reason:
                result["excluded"].append({**row, "reason": excluded_reason})
            else:
                result["gpus"].append(row)
        result["success"] = True
        if not result["gpus"]:
            result["message"] = "nvidia-smi 未报告允许使用的 Tesla T4；GPU0/其他显卡均已排除。"
        return result

    memory_status = gpu_status

    @staticmethod
    def _generation_options(options: dict[str, Any] | None) -> tuple[int, float]:
        if options is not None and not isinstance(options, dict):
            raise ValueError("options 必须是字典")
        values = options or {}
        raw_tokens = values.get("max_new_tokens", values.get("num_predict", 256))
        if isinstance(raw_tokens, bool):
            raise ValueError("max_new_tokens 必须是整数")
        try:
            max_new_tokens = int(raw_tokens)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_new_tokens 必须是整数") from exc
        if isinstance(raw_tokens, float) and not raw_tokens.is_integer():
            raise ValueError("max_new_tokens 必须是整数")
        if not 1 <= max_new_tokens <= 32768:
            raise ValueError("max_new_tokens 必须在 1 到 32768 之间")
        raw_temperature = values.get("temperature", 0.0)
        if isinstance(raw_temperature, bool):
            raise ValueError("temperature 必须是 0 到 2 之间的有限数值")
        try:
            temperature = float(raw_temperature)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("temperature 必须是 0 到 2 之间的有限数值") from exc
        if not math.isfinite(temperature) or not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature 必须是 0 到 2 之间的有限数值")
        return max_new_tokens, temperature

    @classmethod
    def _extract_text(cls, payload: Any) -> str:
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, dict):
            text = ""
            for key in ("reply", "text", "response", "generated_text", "content", "output", "output_text"):
                value = payload.get(key)
                if isinstance(value, str):
                    text = value
                    break
            if not text:
                choices = payload.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    choice = choices[0]
                    message = choice.get("message")
                    text = (
                        choice.get("text")
                        or choice.get("content")
                        or (message.get("content") if isinstance(message, dict) else "")
                        or ""
                    )
            if not text and isinstance(payload.get("data"), (dict, str)):
                return cls._extract_text(payload["data"])
        elif isinstance(payload, list) and payload:
            return cls._extract_text(payload[0])
        else:
            text = ""
        if not isinstance(text, str) or not text.strip():
            raise ProviderAPIError(
                "T4 /generate 响应没有非空文本字段（支持 reply/text/response/generated_text/content 等）。",
                provider="t4",
                payload=payload,
            )
        return text

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        options: dict[str, Any] | None = None,
        keep_alive: Any = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("模型名称不能为空")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 不能为空")
        max_new_tokens, temperature = self._generation_options(options)
        check_cancelled(cancel_token)
        info, _ = self._ready_model()
        if model not in {info.model_ref, info.file_name, self.model_name}:
            raise ValueError(f"T4 是固定模型服务，{model!r} 不是当前健康接口报告的模型")
        # keep_alive 是 Ollama 语义；T4 生命周期只能由显式 stop_service 管理。
        payload = self._client.request_json(
            "POST",
            "/generate",
            {"prompt": prompt, "max_new_tokens": max_new_tokens, "temperature": temperature},
            timeout=self.generate_timeout,
        )
        check_cancelled(cancel_token)
        text = self._extract_text(payload)
        yield {
            "response": text,
            "done": True,
            "done_reason": "stop",
            "provider": self.name,
            "model": info.model_ref,
            "t4_response": payload,
        }

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        keep_alive: Any = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not isinstance(messages, list):
            raise ValueError("messages 必须是消息对象数组")
        lines = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("每条消息必须是对象")
            role = str(message.get("role") or "user")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("消息 content 必须是字符串")
            if content:
                lines.append(f"{role}: {content}")
        if not lines:
            raise ValueError("messages 没有可用于生成的文本")
        prompt = "\n\n".join(lines)
        for event in self.generate(
            model,
            prompt,
            options=options,
            keep_alive=keep_alive,
            cancel_token=cancel_token,
        ):
            yield {
                "message": {"role": "assistant", "content": event["response"]},
                "done": event["done"],
                "done_reason": event.get("done_reason", "stop"),
                "provider": self.name,
                "model": event.get("model", model),
                "t4_response": event.get("t4_response", {}),
            }

    def close(self) -> None:
        """只回收本 Provider 创建的脚本进程，不终止任何外部 PID。"""
        with self._records_lock:
            records = list(self._records.values())
        for record in records:
            if record.running:
                self._supervisor.stop(record)
        if self._owns_supervisor:
            self._supervisor.close()
