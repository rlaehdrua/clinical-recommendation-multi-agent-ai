"""독립 GT 도구 (I-D1). 엔진(ctrec)을 import하지 않습니다. 엔진을 부르는 것은 scripts/gt_engine_adapter.py뿐입니다.

모듈:
- models: G-U / G-C / G-P 스키마 (pydantic)
- stats: Wilson, 정확(Clopper-Pearson) 구간, McNemar 정확 검정, Cohen's kappa
- leak: 기존 held-out·실패 세트·테스트 문장과의 중복, 엔진 출력 필드, 블라인딩 증명 검사
- freeze: FROZEN_GT.json 작성·확인, 상태 전이
- metrics: 설계 문서 D 3절 지표 계산

이 도구는 GT가 아닙니다. 도구가 있다고 정확도에 대해 증명되는 것은 없습니다.
"""

from pathlib import Path

GT_LIB_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = GT_LIB_DIR.parent
REPO_ROOT = SCRIPTS_DIR.parent
GT_DIR = REPO_ROOT / "gt"
