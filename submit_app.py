"""兼容旧入口：现在网站的主文件是 app.py。

保留这个文件是为了让旧的 `streamlit run submit_app.py` 仍能用。
"""
from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "app.py"), run_name="__main__")
