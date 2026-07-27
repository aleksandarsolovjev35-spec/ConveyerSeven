from __future__ import annotations

import importlib
import importlib.metadata
import platform
import sys

from common import ROOT, print_header, require

PACKAGES = (
    ("numpy", "numpy"),
    ("opencv-python", "cv2"),
    ("pyserial", "serial"),
    ("ultralytics", "ultralytics"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pywebview", "webview"),
    ("httpx", "httpx"),
)


def main() -> int:
    print_header("01 — ENVIRONMENT CHECK")
    print(f"project={ROOT}")
    print(f"python={sys.version}")
    print(f"executable={sys.executable}")
    print(f"platform={platform.platform()}")
    print(f"implementation={platform.python_implementation()}")
    require(sys.version_info[:2] == (3, 11), "CPython 3.11 is required")
    require(
        platform.python_implementation() == "CPython",
        "CPython implementation is required",
    )
    require(sys.platform == "win32", "Target preflight must run on Windows")

    errors = []
    for package, module in PACKAGES:
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
        except Exception as exc:
            errors.append(f"{package}: {type(exc).__name__}: {exc}")
            print(f"ERROR  {package}: {exc}")
        else:
            print(f"OK     {package}=={version}")
    require(not errors, "Dependency errors: " + "; ".join(errors))
    print("ENVIRONMENT CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
