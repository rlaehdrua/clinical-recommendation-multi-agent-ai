"""시험 기관 위치 처리 - 팀 결정 D5 Option C (사용자 의도 기반).

- 기본(일반 탐색): 위치로 후보를 빼지 않고, 환자 국가에 기관이 있는지 추천·보고서에 표시만 합니다.
  현재 시험 26개 중 한국 기관이 있는 시험은 0개라, 항상 hard filter를 쓰면 후보가 모두 사라집니다.
- 사용자가 "이 나라에서 참여 가능한 시험만" 원할 때(--country-only): 그 나라 기관이 없는 시험을 후보에서 뺍니다.
  기관 목록이 비어 있는 시험은 위치를 알 수 없으므로 빼지 않고 '위치 미확인'으로 표시합니다.
"""

from __future__ import annotations

from collections import Counter

_ALIASES = {
    "south korea": {"south korea", "korea, republic of", "republic of korea", "korea", "한국", "대한민국"},
}


def _canon(country: str | None) -> str:
    c = (country or "").strip().lower()
    for canon, names in _ALIASES.items():
        if c in names:
            return canon
    return c


def countries(trial: dict) -> Counter:
    return Counter(_canon(loc.get("country")) for loc in trial.get("locations") or [] if loc.get("country"))


def has_site_in(trial: dict, country: str) -> bool | None:
    """None = 기관 목록이 없어 알 수 없음."""
    c = countries(trial)
    if not c:
        return None
    return _canon(country) in c


def note(trial: dict, country: str | None) -> str:
    c = countries(trial)
    if not c:
        return "기관 위치 정보 없음 (위치 미확인)"
    listed = ", ".join(f"{name.title()} {n}곳" for name, n in c.most_common(3))
    if len(c) > 3:
        listed += f" 외 {len(c) - 3}개국"
    if country:
        here = "있음" if _canon(country) in c else "없음"
        return f"{country} 기관 {here} (등록 기관: {listed})"
    return f"등록 기관: {listed}"


def hard_filter(trials: dict[str, dict], country: str) -> tuple[dict[str, dict], dict[str, str]]:
    """반환: (남은 시험, 뺀 시험 {trial_id: 사유})."""
    kept, removed = {}, {}
    for tid, t in trials.items():
        if has_site_in(t, country) is False:
            removed[tid] = f"{country} 기관 없음 ({note(t, None)})"
        else:
            kept[tid] = t
    return kept, removed


def report_section(trials: dict[str, dict], ranked_ids: list[str], country: str | None,
                   removed: dict[str, str] | None = None) -> str:
    """보고서에 코드가 고정으로 붙이는 '참여 기관 위치' 섹션."""
    if not ranked_ids and not removed:
        return ""
    lines = ["## 참여 기관 위치", ""]
    if removed is not None:
        lines.append(f"요청에 따라 {country}에 기관이 있는 시험만 평가했습니다. 제외한 시험 {len(removed)}개.")
    else:
        lines.append("위치는 적격성 판정에 쓰지 않았습니다. 실제 참여 가능성은 기관 위치를 함께 확인하세요.")
    lines.append("")
    lines += [f"- **{tid}**: {note(trials[tid], country)}" for tid in ranked_ids]
    return "\n".join(lines) + "\n\n"
