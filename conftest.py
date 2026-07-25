"""pytest 루트 conftest — 저장소 루트를 import 경로에 추가.

백엔드는 평면 레이아웃(모든 모듈이 홈 디렉터리에 직접 위치)이라
tests/ 하위에서 `import format_utils` 등이 되도록 루트를 sys.path에 넣는다.
운영 실행(run_all.py)에는 영향 없음 — pytest 수집 시에만 로드된다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
