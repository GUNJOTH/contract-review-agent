"""任务文件存储"""

from __future__ import annotations

import os
import json
import shutil
from pathlib import Path
from typing import Any

from contract_review_app.config import settings


class TaskFileStore:
    """任务输入文件存储"""

    def __init__(self):
        self._input_root = settings.TASK_INPUT_PATH
        self.ensure_directories()

    def ensure_directories(self) -> None:
        self._input_root.mkdir(parents=True, exist_ok=True)

    def save_file(
        self,
        *,
        task_id: str,
        filename: str | None,
        content_type: str | None,
        data: bytes,
        options: dict[str, Any],
    ) -> str:
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename or "upload.bin").name
        file_path = task_dir / safe_name
        file_path.write_bytes(data)
        manifest = {
            "input_mode": "file",
            "filename": safe_name,
            "content_type": content_type,
            "payload": {
                "file_path": str(file_path),
            },
            "options": options,
        }
        return self._write_manifest(task_dir, manifest)

    def save_files(
        self,
        *,
        task_id: str,
        files: list[tuple[str, bytes, str | None]],
        options: dict[str, Any],
    ) -> str:
        """保存多个输入文件（合同包），manifest 以 files 模式记录各文件路径。"""
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for index, (filename, data, content_type) in enumerate(files):
            safe_name = Path(filename or f"file-{index}").name
            file_path = task_dir / f"{index:03d}-{safe_name}"
            file_path.write_bytes(data)
            entries.append(
                {
                    "file_path": str(file_path),
                    "filename": safe_name,
                    "content_type": content_type,
                }
            )
        manifest = {
            "input_mode": "files",
            "payload": {"file_paths": entries},
            "options": options,
        }
        return self._write_manifest(task_dir, manifest)

    def save_base64(
        self, *, task_id: str, encoded: str, options: dict[str, Any]
    ) -> str:
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "input_mode": "base64",
            "payload": {
                "ImageBase64": encoded,
            },
            "options": options,
        }
        return self._write_manifest(task_dir, manifest)

    def save_url(self, *, task_id: str, url: str, options: dict[str, Any]) -> str:
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "input_mode": "url",
            "payload": {
                "ImageUrl": url,
            },
            "options": options,
        }
        return self._write_manifest(task_dir, manifest)

    def load_manifest(self, manifest_path: str) -> dict[str, Any]:
        return json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    def input_path_for(self, task_id: str) -> str:
        """返回预留的 manifest 路径，但不创建任务目录或写入任何字节。"""

        return str(self._task_dir(task_id) / "input.json")

    def delete_task_files(self, task_id: str) -> None:
        task_dir = self._task_dir(task_id)
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

    def list_stale_task_ids(self, *, older_than: float, limit: int) -> list[str]:
        if not self._input_root.exists():
            return []

        stale_task_ids: list[str] = []
        with os.scandir(self._input_root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                if entry.stat().st_mtime > older_than:
                    continue
                stale_task_ids.append(entry.name)
                if len(stale_task_ids) >= limit:
                    break
        return stale_task_ids

    def _task_dir(self, task_id: str) -> Path:
        return self._input_root / task_id

    def _write_manifest(self, task_dir: Path, manifest: dict[str, Any]) -> str:
        manifest_path = task_dir / "input.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return str(manifest_path)


task_file_store = TaskFileStore()
