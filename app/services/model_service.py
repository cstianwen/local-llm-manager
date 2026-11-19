"""模型应用服务：协调扫描、Provider、预检、SQLite 与安全操作。"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from ..core.config import app_data_dir
from ..core.errors import ModelManagerError, UnsupportedOperationError
from ..core.models import DictSerializable, ModelInfo, OperationResult, RunningModel
from ..core.preflight import PreflightChecker, PreflightResult
from ..core.processes import ManagedProcess
from ..inspector.base import InspectionResult
from ..inspector.files import FileInspector
from ..inspector.ollama_logs import OllamaInspector
from ..inspector.ports import PortInspector
from ..providers.base import Provider, check_cancelled
from ..providers.registry import ProviderRegistry, create_default_registry
from ..scanner.service import ScannerService
from ..store.db import ModelStore
from .safety import DiskMaintenanceService, QuarantineService, _confirmation_required
from .transfer import FormatConversionService, TransferService


@dataclass
class ReleaseEvidence(DictSerializable):
    model_id: str
    inspections: list[dict[str, Any]] = field(default_factory=list)
    running_models: list[dict[str, Any]] = field(default_factory=list)
    managed_processes: list[dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = True
    message: str = "请先查看证据；服务层不会终止非受管进程。"


class ModelService:
    """同步方法可直接调用；长任务入口的前两个参数与 TaskManager 对齐。"""

    def __init__(
        self,
        store: ModelStore,
        registry: ProviderRegistry | None = None,
        *,
        scanner: ScannerService | None = None,
        preflight: PreflightChecker | None = None,
        file_inspector: Any = None,
        port_inspector: Any = None,
        ollama_inspector: Any = None,
        quarantine: QuarantineService | None = None,
        data_dir: str | os.PathLike[str] | None = None,
    ):
        self.store = store
        self.registry = registry or create_default_registry()
        self._owns_registry = registry is None
        self.scanner = scanner or ScannerService(store)
        self.preflight_checker = preflight or PreflightChecker()
        self.file_inspector = file_inspector or FileInspector()
        self.port_inspector = port_inspector or PortInspector()
        self._ollama_inspector = ollama_inspector
        base = Path(data_dir).expanduser().absolute() if data_dir else app_data_dir()
        self.quarantine = quarantine or QuarantineService(store, base / "quarantine")
        self.transfer = TransferService(store)
        self.conversion = FormatConversionService(store, self.registry)
        self.disk_maintenance = DiskMaintenanceService(store, self.quarantine, data_dir=base)
        self._processes: dict[str, ManagedProcess] = {}

    def list_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if refresh:
            self.refresh_models()
        return self.store.all()

    models = list_models

    def get_model(self, reference: str, *, provider: str = "") -> ModelInfo:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("模型 ID 或路径不能为空")
        rows = self.store.all()
        exact = [model for model in rows if model.model_id == reference or model.file_path == reference]
        if provider:
            exact = [model for model in exact if model.provider == provider]
        if not exact:
            exact = [
                model for model in rows
                if model.model_ref == reference and (not provider or model.provider == provider)
            ]
        if not exact:
            raise KeyError(f"模型库中找不到记录：{reference}")
        if len(exact) > 1:
            raise KeyError(f"模型标识不唯一，请使用稳定 model_id 或完整路径：{reference}")
        return exact[0]

    def scan(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        **options: Any,
    ) -> Any:
        """示例：tasks.submit("model-scan", service.scan, roots=[...])。"""
        result = self.scanner.scan(token=token, progress=progress, **options)
        self.store.operations.add(
            "model.scan", status="success",
            message=f"扫描完成，登记 {len(result.models)} 个模型。",
            details={"models_found": len(result.models), "issues": len(result.issues), "pruned": result.pruned},
        )
        return result

    def refresh_models(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        *,
        provider_names: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """从真实 Provider API 刷新数据库；失败按后端分别返回，不伪造空成功。"""
        names = list(provider_names) if provider_names is not None else list(self.registry.names)
        models: list[ModelInfo] = []
        errors: dict[str, str] = {}
        for index, name in enumerate(names, 1):
            check_cancelled(token)
            if progress:
                progress({"phase": "provider", "provider": name, "index": index, "count": len(names)})
            try:
                found = self.registry.get(name).list_models()
                self.store.upsert_many(found)
                models.extend(found)
                self._prune_virtual_provider_records(name, found)
            except Exception as exc:
                errors[name] = str(exc)
        if progress:
            progress({"phase": "completed", "models_found": len(models), "errors": errors})
        self.store.operations.add(
            "provider.refresh", status="success" if not errors else "partial",
            message=f"Provider 刷新得到 {len(models)} 条记录。",
            details={"errors": errors, "providers": names},
        )
        return {"models": models, "errors": errors, "providers": names}

    def _prune_virtual_provider_records(self, provider: str, alive: list[ModelInfo]) -> None:
        """只清理 API 虚拟 URI 旧登记，绝不删除扫描 manifest 或本地文件。"""
        prefix = f"{provider}://"
        paths = {model.file_path for model in alive}
        for old in self.store.all():
            if old.provider == provider and old.file_path.startswith(prefix) and old.file_path not in paths:
                self.store.delete(old.file_path)

    def refresh_status(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        statuses = self.registry.detect_all()
        running: list[RunningModel] = []
        errors: dict[str, str] = {}
        for index, (name, provider) in enumerate(((name, self.registry.get(name)) for name in self.registry.names), 1):
            check_cancelled(token)
            if progress:
                progress({"phase": "running", "provider": name, "index": index, "count": len(self.registry)})
            if not statuses[name].running:
                continue
            try:
                running.extend(provider.running_models())
            except Exception as exc:
                errors[name] = str(exc)
        if progress:
            progress({"phase": "completed", "running_count": len(running), "errors": errors})
        return {"providers": statuses, "running": running, "errors": errors, "captured_at": time.time()}

    status = refresh_status

    def preflight(
        self,
        model_id: str,
        *,
        device: str = "auto",
        context_length: int | None = None,
        hardware: Any = None,
        backend: str = "",
    ) -> PreflightResult:
        return self._check_preflight(
            self.get_model(model_id), device=device,
            context_length=context_length, hardware=hardware, backend=backend,
        )

    def _check_preflight(
        self,
        model: ModelInfo,
        *,
        device: str,
        context_length: int | None,
        hardware: Any,
        backend: str,
    ) -> PreflightResult:
        provider = self._load_provider_name(model, backend)
        context = model.context_length if context_length is None else context_length
        return self.preflight_checker.check(
            model, provider=provider, device=device,
            context_length=context or 0, hardware=hardware,
        )

    @staticmethod
    def _load_provider_name(model: ModelInfo, backend: str = "") -> str:
        if model.provider == "local-file":
            return backend or "llamacpp"
        if backend and backend != model.provider:
            raise ValueError("后端登记模型只能由其所属 Provider 装入，不能把模型引用转交其他后端")
        return model.provider

    def load_model(
        self,
        model_id: str,
        *,
        device: str = "auto",
        context_length: int | None = None,
        backend: str = "",
        keep_alive: Any = "5m",
        options: dict[str, Any] | None = None,
        hardware: Any = None,
        allow_insufficient: bool = False,
    ) -> OperationResult:
        model = self.get_model(model_id)
        provider_name = self._load_provider_name(model, backend)
        provider = self.registry.get(provider_name)
        check = self._check_preflight(
            model, device=device, context_length=context_length,
            hardware=hardware, backend=backend,
        )
        if not check.can_attempt and not allow_insufficient:
            self.store.operations.add(
                "model.load", model.model_id, status="blocked", message="装入预检未通过，未调用 Provider。",
                details={"preflight": check.to_dict()},
            )
            raise ModelManagerError("装入预检未通过：" + "；".join(check.errors or [check.message]))
        call_options = dict(options or {})
        target: str
        if model.provider == "local-file":
            if provider_name == "ollama":
                raise UnsupportedOperationError("Ollama 不能通过 generate 把任意裸 GGUF 路径直接装入；请使用 llama.cpp 或先在 Ollama 中真实创建模型")
            target = model.file_path
            if provider_name == "llamacpp" and (context_length or model.context_length):
                call_options.setdefault("context_length", context_length or model.context_length)
        else:
            target = model.model_ref
            if not target:
                raise ValueError("数据库记录缺少 Provider 模型引用 model_ref")
            if provider_name == "ollama":
                call_options.setdefault("keep_alive", keep_alive)
        try:
            raw = provider.load_model(target, **call_options)
            if isinstance(raw, ManagedProcess):
                self._processes[raw.id] = raw
                result = OperationResult(
                    True, "已由本工具启动受管模型进程；请依据进程状态确认后续可用性。",
                    data={"process": raw.to_dict()},
                )
            elif isinstance(raw, OperationResult):
                result = raw
            else:
                raise TypeError(f"Provider 返回了未知装入结果：{type(raw).__name__}")
            result.data["preflight"] = check.to_dict()
            result.data["provider"] = provider_name
            result.data["model_id"] = model.model_id
            self.store.operations.add(
                "model.load", model.model_id, status="success" if result.success else "error",
                message=result.message, details={"provider": provider_name, "preflight": check.to_dict(), "forced": allow_insufficient},
            )
            return result
        except Exception as exc:
            self._log_failure("model.load", model.model_id, exc, {"provider": provider_name})
            raise

    def load_model_task(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        model_id: str = "",
        **options: Any,
    ) -> OperationResult:
        check_cancelled(token)
        if progress:
            progress({"phase": "preflight", "model_id": model_id})
        result = self.load_model(model_id, **options)
        check_cancelled(token)
        if progress:
            progress({"phase": "completed", "model_id": model_id, "success": result.success})
        return result

    def ensure_loaded(self, model_id: str, **options: Any) -> OperationResult:
        model = self.get_model(model_id)
        for process in self._processes.values():
            if process.running and process.model_id in {model.model_id, model.file_path}:
                return OperationResult(True, "本工具创建的模型进程仍在运行，无需重复自动装入。", data={"already_running": True, "process": process.to_dict()})
        provider = self.registry.get(self._load_provider_name(model, options.get("backend", "")))
        if model.provider != "local-file":
            for running in provider.running_models():
                if running.name == model.model_ref:
                    return OperationResult(True, f"Provider 已报告模型 {model.model_ref} 正在运行，无需重复装入。", data={"already_running": True})
        return self.load_model(model_id, **options)

    auto_load = ensure_loaded

    def auto_load_task(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        model_id: str = "",
        **options: Any,
    ) -> OperationResult:
        check_cancelled(token)
        if progress:
            progress({"phase": "check_running", "model_id": model_id})
        result = self.ensure_loaded(model_id, **options)
        check_cancelled(token)
        if progress:
            progress({
                "phase": "completed", "model_id": model_id,
                "success": result.success,
                "already_running": bool(result.data.get("already_running")),
            })
        return result

    def unload_model(
        self,
        model_id: str,
        *,
        backend: str = "",
        process: str | int | ManagedProcess | None = None,
        options: dict[str, Any] | None = None,
    ) -> OperationResult:
        model = self.get_model(model_id)
        provider_name = self._load_provider_name(model, backend)
        provider = self.registry.get(provider_name)
        try:
            if process is not None:
                stop = getattr(provider, "stop_model", None)
                if not callable(stop):
                    raise UnsupportedOperationError(f"{provider_name} 不能停止受管模型进程")
                handle = self._processes.get(process, process) if isinstance(process, str) else process
                stopped = stop(handle)
                result = OperationResult(True, "已请求停止本工具创建的受管进程。" if stopped else "该受管进程已经退出。", data={"stopped": bool(stopped)})
            else:
                raw = provider.unload_model(model.model_ref, **dict(options or {}))
                if not isinstance(raw, OperationResult):
                    raise TypeError("Provider 卸载返回值不是 OperationResult")
                result = raw
            self.store.operations.add("model.unload", model.model_id, message=result.message, details={"provider": provider_name})
            return result
        except Exception as exc:
            self._log_failure("model.unload", model.model_id, exc, {"provider": provider_name})
            raise

    def inspect_release(self, model_id: str, *, port: int | None = None, backend: str = "") -> ReleaseEvidence:
        """只收集 inspector 证据和受管进程快照，绝不发送终止信号。"""
        model = self.get_model(model_id)
        evidence = ReleaseEvidence(model_id=model.model_id)
        paths = [model.file_path, *model.shard_files]
        for value in dict.fromkeys(paths):
            if not value or "://" in value:
                continue
            path = Path(value)
            if not path.is_file():
                continue
            try:
                inspected = self.file_inspector.inspect(path)
                evidence.inspections.append({"kind": "file", "path": str(path), "result": self._inspection_dict(inspected)})
            except Exception as exc:
                evidence.inspections.append({"kind": "file", "path": str(path), "error": str(exc)})
        provider_name = self._load_provider_name(model, backend)
        provider = self.registry.get(provider_name)
        selected_port = port or urlsplit(getattr(provider, "base_url", "")).port
        try:
            if provider_name == "ollama":
                inspector = self._ollama_inspector or OllamaInspector()
                inspected = inspector.inspect(model=model.model_ref, port=selected_port or 11434)
                evidence.inspections.append({"kind": "ollama_logs", "result": self._inspection_dict(inspected)})
            elif selected_port:
                inspected = self.port_inspector.inspect(
                    selected_port, direction="remote", provider=provider_name, model=model.model_ref,
                )
                evidence.inspections.append({"kind": "port_users", "port": selected_port, "result": self._inspection_dict(inspected)})
            if provider.detect().running:
                evidence.running_models = [item.to_dict() for item in provider.running_models() if item.name == model.model_ref]
        except Exception as exc:
            evidence.inspections.append({"kind": "provider", "provider": provider_name, "error": str(exc)})
        for process in self._processes.values():
            if process.model_id in {model.model_id, model.file_path}:
                evidence.managed_processes.append(process.to_dict())
        return evidence

    prepare_release = inspect_release

    @staticmethod
    def _inspection_dict(value: InspectionResult | dict[str, Any]) -> dict[str, Any]:
        return value.to_dict() if hasattr(value, "to_dict") else dict(value)

    def release_model(
        self,
        model_id: str,
        *,
        confirmation: bool = False,
        process: str | int | ManagedProcess | None = None,
        port: int | None = None,
        backend: str = "",
        options: dict[str, Any] | None = None,
    ) -> OperationResult:
        evidence = self.inspect_release(model_id, port=port, backend=backend)
        if confirmation is not True:
            return OperationResult(
                False, "释放占用前必须由 UI 展示 inspector 证据并取得确认；未终止任何进程。",
                data={"evidence": evidence.to_dict()}, error="confirmation_required",
            )
        if process is None and self.get_model(model_id).provider == "local-file":
            raise UnsupportedOperationError("本地文件模型需要 UI 从证据中选择一个由本工具创建的 process；不会按 Inspector PID 擅自杀进程")
        result = self.unload_model(model_id, backend=backend, process=process, options=options)
        result.data["evidence"] = evidence.to_dict()
        result.data["non_managed_processes_terminated"] = False
        return result

    def delete_model(
        self,
        model_id: str,
        *,
        confirmation: bool = False,
        path: str | os.PathLike[str] | None = None,
    ) -> OperationResult:
        model = self.get_model(model_id)
        if model.provider == "local-file":
            return self.quarantine.quarantine_model(model.model_id, path, confirmation=confirmation)
        if confirmation is not True:
            return _confirmation_required("通过 Provider API 删除模型", {"model_id": model.model_id, "model_ref": model.model_ref, "provider": model.provider})
        provider = self.registry.get(model.provider)
        try:
            result = provider.delete_model(model.model_ref)
            if not isinstance(result, OperationResult):
                raise TypeError("Provider 删除返回值不是 OperationResult")
            # 仅移除 SQLite 登记，不触碰 manifest/blob 或 Provider 报告的本地路径。
            for old in self.store.all():
                if old.provider == model.provider and old.model_ref == model.model_ref:
                    self.store.delete(old.file_path)
            result.data.update({"provider_api": True, "local_quarantine": False, "model_ref": model.model_ref})
            self.store.operations.add("provider.delete", model.model_id, message=result.message, details=result.data)
            return result
        except Exception as exc:
            self._log_failure("provider.delete", model.model_id, exc, {"provider": model.provider})
            raise

    def list_quarantine(self) -> list[dict[str, Any]]:
        return self.quarantine.list()

    def restore_quarantined(self, entry_id: str, *, confirmation: bool = False) -> OperationResult:
        return self.quarantine.restore(entry_id, confirmation=confirmation)

    def permanently_delete(self, entry_id: str, *, confirmation: bool = False, expected_path: str | os.PathLike[str] | None = None) -> OperationResult:
        return self.quarantine.permanent_delete(entry_id, confirmation=confirmation, expected_path=expected_path)

    def export_models(self, destination: str | os.PathLike[str], **options: Any) -> OperationResult:
        return self.transfer.export_models(destination, **options)

    def import_models(self, source: str | os.PathLike[str], *, apply: bool = False) -> OperationResult:
        return self.transfer.import_models(source, apply=apply)

    def export_config(self, destination: str | os.PathLike[str]) -> OperationResult:
        return self.transfer.export_config(destination)

    def import_config(self, source: str | os.PathLike[str], *, apply: bool = False) -> OperationResult:
        return self.transfer.import_config(source, apply=apply)

    def convert_model(
        self,
        model_id: str,
        output_path: str | os.PathLike[str],
        quantization: str,
        *,
        threads: int | None = None,
    ) -> ManagedProcess:
        model = self.get_model(model_id)
        if model.provider != "local-file" or model.format.lower() != "gguf":
            raise UnsupportedOperationError("服务层量化只接受数据库中登记的本地 GGUF 文件")
        process = self.conversion.convert(model.file_path, output_path, quantization, threads=threads, model_id=model.model_id)
        self._processes[process.id] = process
        return process

    quantize_model = convert_model

    def record_usage(
        self,
        model_id: str,
        *,
        duration_ms: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        status: str = "success",
        started_at: float | None = None,
    ) -> int:
        model = self.get_model(model_id)
        return self.store.usage.record(
            model.model_id, model.provider, duration_ms=duration_ms,
            input_tokens=input_tokens, output_tokens=output_tokens,
            status=status, started_at=started_at,
        )

    def usage_statistics(self, *, days: int = 30, model_id: str | None = None) -> dict[str, Any]:
        if days <= 0:
            raise ValueError("统计天数必须大于 0")
        selected = self.get_model(model_id).model_id if model_id else None
        return {"summary": self.store.usage.summary(days), "daily": self.store.usage.daily(days, selected)}

    usage = usage_statistics

    def operation_logs(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        return self.store.operations.recent(limit, status)

    def compare_models(self, model_ids: Iterable[str] | None = None) -> dict[str, Any]:
        selected = set(model_ids) if model_ids is not None else None
        models = [model for model in self.store.all() if selected is None or model.model_id in selected]
        if selected is not None and len({model.model_id for model in models}) != len(selected):
            raise KeyError("部分对比模型 ID 不存在")
        rows = [{
            "model_id": model.model_id, "display_name": model.display_name or model.file_name,
            "provider": model.provider, "format": model.format, "quant": model.quant,
            "size_bytes": model.size_bytes, "params_b": model.params_b,
            "architecture": model.architecture, "context_length": model.context_length,
            "integrity": model.integrity, "file_path": model.file_path,
        } for model in models]
        fields = ("provider", "format", "quant", "size_bytes", "params_b", "architecture", "context_length", "integrity")
        differences = [name for name in fields if len({row[name] for row in rows}) > 1]
        return {"models": rows, "differences": differences, "read_only": True}

    def deduplication_suggestions(self) -> list[dict[str, Any]]:
        """只给量化/副本审查建议，不选择删除目标，也不执行文件操作。"""
        groups: dict[tuple[str, str, float], list[ModelInfo]] = {}
        for model in self.store.all():
            base = re.sub(r"(?i)(?:^|[-_.\s])(?:q\d+(?:_[a-z0-9]+)?|f16|f32|bf16|int4|int8)(?:$|[-_.\s])", "-", model.display_name or model.file_name)
            base = re.sub(r"\.(?:gguf|safetensors|bin)$", "", base, flags=re.IGNORECASE)
            base = re.sub(r"[-_.\s]+", "-", base).strip("-").casefold()
            groups.setdefault((base, model.architecture.casefold(), round(model.params_b, 3)), []).append(model)
        suggestions = []
        for (base, architecture, params), items in groups.items():
            if len(items) < 2:
                continue
            ordered = sorted(items, key=lambda item: item.size_bytes, reverse=True)
            suggestions.append({
                "group": base, "architecture": architecture, "params_b": params,
                "models": [{"model_id": item.model_id, "display_name": item.display_name, "quant": item.quant, "size_bytes": item.size_bytes, "integrity": item.integrity, "file_path": item.file_path} for item in ordered],
                "potential_reclaim_upper_bound_bytes": sum(item.size_bytes for item in ordered[1:]),
                "message": "仅为量化版本/副本审查建议；容量上限可能因共享文件而高估。请核对许可证、上下文、完整性和实际引用后自行决定，服务未删除任何文件。",
            })
        return suggestions

    quant_deduplication_suggestions = deduplication_suggestions

    def pull_model(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        model_ref: str = "",
        *,
        resource_id: str = "",
    ) -> OperationResult:
        from .downloads import DownloadService
        return DownloadService(self.store, self.registry).pull_ollama(token, progress, model_ref, resource_id=resource_id)

    def _log_failure(self, action: str, target: str, exc: Exception, details: dict[str, Any] | None = None) -> None:
        try:
            self.store.operations.add(action, target, status="error", message=str(exc), details=details or {})
        except Exception:
            pass  # 日志故障不能掩盖原始 Provider 异常。

    def close(self) -> None:
        if self._owns_registry:
            self.registry.close()


ModelManagerService = ModelService
