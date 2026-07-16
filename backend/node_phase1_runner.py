# -*- coding: utf-8 -*-
"""
既存の node.py を大きく壊さず、Web UI から生成した node_id / node_api_key / capacity を
そのまま与えて起動できるようにする薄いラッパーである。

改善点:
- node.py の配置場所が「runner と同じフォルダ」でも「runner の親フォルダ」でも動くようにする
- 環境変数 PHASE1_ORIGINAL_NODE_PATH / ORIGINAL_NODE_PATH で明示指定できるようにする
- CLI 引数 --original-node-path で最優先指定できるようにする

使い方例:
python node_phase1_runner.py --node-id "..." --node-api-key "..." --capacity-gb 200
python node_phase1_runner.py --node-id "..." --node-api-key "..." --capacity-gb 200 --original-node-path ./node.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


THIS_DIR = Path(__file__).resolve().parent


def _configure_console_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True, write_through=True)
        except Exception:
            pass


def _log(level: str, message: str, **fields: object) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    suffix = ""
    if fields:
        try:
            suffix = " " + json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            suffix = " " + repr(fields)
    print(f"[tricloud-runner {timestamp}] {level.upper()} {message}{suffix}", flush=True)


_configure_console_streams()


_TRICLOUD_DLL_HANDLES = []

def _ensure_bundled_python_runtime_paths() -> None:
    """Make wheel packages bundled next to the embedded python.exe importable.

    Windows embeddable Python is isolated by pythonXY._pth. In most cases the
    _pth file is enough, but packaged Electron paths can differ from build-time
    paths. This helper is intentionally defensive: it adds the bundled
    site-packages directory and native wheel DLL directories at runtime before
    node.py imports zmq.
    """
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
        exe_dir = Path()

    candidates = []
    if exe_dir:
        candidates.append(exe_dir / "Lib" / "site-packages")

    # Also support running from a development tree where packages may be placed
    # relative to this backend folder.
    candidates.append(THIS_DIR.parent / "runtime" / "python" / "Lib" / "site-packages")

    for site_packages in candidates:
        try:
            if not site_packages.is_dir():
                continue
            sp = str(site_packages)
            if sp not in sys.path:
                sys.path.insert(0, sp)

            dll_dirs = [site_packages / "pyzmq.libs", site_packages / "zmq.libs"]
            try:
                dll_dirs.extend([p for p in site_packages.iterdir() if p.is_dir() and p.name.endswith(".libs")])
            except Exception:
                pass

            for dll_dir in dll_dirs:
                try:
                    if not dll_dir.is_dir():
                        continue
                    dll_s = str(dll_dir)
                    if hasattr(os, "add_dll_directory"):
                        try:
                            _TRICLOUD_DLL_HANDLES.append(os.add_dll_directory(dll_s))
                        except Exception:
                            pass
                    old_path = os.environ.get("PATH", "")
                    parts = [x for x in old_path.split(os.pathsep) if x]
                    if dll_s not in parts:
                        os.environ["PATH"] = dll_s + os.pathsep + old_path
                except Exception:
                    pass
        except Exception:
            pass


_ensure_bundled_python_runtime_paths()
# Windows embeddable Python uses a pythonXY._pth file and can run in isolated mode.
# Make the backend folder explicit so node.py and crypto_common_keywrap.py are importable.
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))


def _candidate_node_paths(explicit_path: Optional[str] = None) -> Iterable[Path]:
    """node.py の候補パスを優先順に返す。"""
    seen: set[Path] = set()

    def add(value: Optional[str | Path]) -> Iterable[Path]:
        if value is None:
            return []
        raw = str(value).strip()
        if not raw:
            return []
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if p in seen:
            return []
        seen.add(p)
        return [p]

    # 1) CLI明示指定を最優先
    yield from add(explicit_path)

    # 2) 環境変数指定
    yield from add(os.environ.get("PHASE1_ORIGINAL_NODE_PATH"))
    yield from add(os.environ.get("ORIGINAL_NODE_PATH"))

    # 3) runner と同じフォルダ
    yield from add(THIS_DIR / "node.py")

    # 4) runner の親フォルダ
    yield from add(THIS_DIR.parent / "node.py")

    # 5) 旧日本語ファイル名を残している環境への互換フォールバック
    yield from add(THIS_DIR / "ノード.py")
    yield from add(THIS_DIR.parent / "ノード.py")


def _resolve_original_node_path(explicit_path: Optional[str] = None) -> Path:
    candidates = list(_candidate_node_paths(explicit_path))
    _log("INFO", "searching for node.py", candidates=[str(path) for path in candidates])
    for path in candidates:
        if path.is_file():
            _log("INFO", "node.py resolved", path=str(path))
            return path

    searched = "\n".join(f"  - {path}" for path in candidates) or "  - <no candidates>"
    raise RuntimeError(
        "failed to locate original node module. "
        "Place node.py next to node_phase1_runner.py, place it in the parent folder, "
        "set PHASE1_ORIGINAL_NODE_PATH, or pass --original-node-path.\n"
        f"searched paths:\n{searched}"
    )


def _load_original_node_module(original_node_path: Optional[str] = None):
    path = _resolve_original_node_path(original_node_path)
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    _log("INFO", "loading node module", path=str(path), python_executable=sys.executable)
    spec = importlib.util.spec_from_file_location("phase1_original_node", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load original node module: {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses など一部の標準ライブラリは、実行中モジュールを
    # sys.modules[__module__] から参照する。exec_module() の前に登録しないと、
    # @dataclass の処理中に sys.modules.get(cls.__module__) が None になり得る。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 読み込み失敗時に壊れたモジュールを残さない。
        sys.modules.pop(spec.name, None)
        raise
    _log("INFO", "node module loaded", path=str(path))
    return module


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-id", required=True)
    ap.add_argument("--node-api-key", required=True)
    ap.add_argument("--server", default="tcp://127.0.0.1:9999")
    ap.add_argument("--storage-dir", default="./node_store")
    ap.add_argument("--capacity-gb", type=int, default=None)
    ap.add_argument("--auto-capacity", action="store_true", help="空き容量の90%を自動で提供容量に設定")
    ap.add_argument("--interactive-capacity", action="store_true", help="空き容量の90%を上限として対話的に提供容量(GB)を入力")
    ap.add_argument(
        "--original-node-path",
        default=None,
        help="node.py の場所を明示指定する。未指定なら同一フォルダ、親フォルダ、環境変数から探索する。",
    )
    args = ap.parse_args()

    _log(
        "INFO",
        "runner started",
        node_id=args.node_id,
        server=args.server,
        storage_dir=os.path.abspath(os.path.expanduser(args.storage_dir)),
        capacity_gb=args.capacity_gb,
        api_key_present=bool(args.node_api_key),
        python_executable=sys.executable,
        python_version=sys.version.split()[0],
        runner_path=str(Path(__file__).resolve()),
    )

    mod = _load_original_node_module(args.original_node_path)

    # psutil.disk_usage() は対象パスが存在しない場合に OSError / FileNotFoundError を出す。
    # そのため、容量確認より先に保存ルートを作成する。
    storage_root = os.path.abspath(os.path.expanduser(args.storage_dir))
    os.makedirs(storage_root, exist_ok=True)

    base = os.path.join(storage_root, args.node_id)

    total_b, used_b, free_b = mod._disk_usage_bytes(storage_root)
    offerable_b = int(free_b * 0.90)
    max_gb = mod._bytes_to_gib_floor(offerable_b)

    print("=== ノード用ストレージ情報 ===")
    print(f"対象パス: {storage_root}")
    print(f"総容量: {total_b / (1024**3):.2f} GiB")
    print(f"実空き容量: {free_b / (1024**3):.2f} GiB")
    print(f"提供可能として表示する空き(90%): {offerable_b / (1024**3):.2f} GiB")
    print("※ 全提供にするとOSが不安定化しやすいため、本来の空き容量の90%までを上限としている。")

    cap_gb: Optional[int] = args.capacity_gb
    if cap_gb is None:
        default_gb = min(20, max_gb) if max_gb > 0 else 0
        if args.interactive_capacity:
            cap_gb = mod._prompt_int(
                f"このノードが提供する容量(GB)を入力してください [最大 {max_gb}GB] (Enterで既定 {default_gb}GB): ",
                default_gb,
            )
        elif args.auto_capacity:
            cap_gb = default_gb if default_gb == max_gb else max_gb
        else:
            cap_gb = default_gb

    if max_gb > 0 and cap_gb > max_gb:
        print(f"[WARN] 指定容量 {cap_gb}GB は上限 {max_gb}GB を超えています。上限に丸めます。")
        cap_gb = max_gb
    if cap_gb <= 0:
        raise SystemExit("提供容量が 0GB です。--capacity-gb で指定するか、--interactive-capacity を使用してください。")

    capacity_bytes = int(cap_gb) * 1024 * 1024 * 1024
    _log(
        "INFO",
        "creating node",
        node_id=args.node_id,
        server=args.server,
        storage_base=base,
        capacity_bytes=capacity_bytes,
    )
    node = mod.Node(
        node_id=args.node_id,
        server=args.server,
        storage_base=base,
        capacity_bytes=capacity_bytes,
        node_api_key=args.node_api_key,
    )
    node.serve_forever()
    _log("INFO", "runner stopped normally", node_id=args.node_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("INFO", "runner interrupted")
    except Exception as exc:
        _log("ERROR", "runner failed", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise
