# 웹 화면이 쓰는 외부 자원 (로컬 제공)

외부 CDN을 쓰지 않습니다. 아래 파일은 npm 레지스트리의 공개 패키지에서 꺼낸 브라우저용 빌드를 **수정 없이** 그대로 넣은 것입니다.

- 반입일: 2026-10-14
- 원본 확인 방법: 패키지 압축 파일(tarball)의 sha512가 레지스트리가 공개한 `dist.integrity`와 같은지 확인했습니다.
- 나머지 파일(소스, 소스맵, 다른 빌드, 실행 파일)은 넣지 않았습니다.

| 파일 | 패키지 | 버전 | 라이선스 | 원본 | tarball sha512 (= 레지스트리 integrity) | 파일 sha256 |
|---|---|---|---|---|---|---|
| `marked.min.js` | marked | 12.0.2 | MIT (`LICENSE-marked.md`) | https://registry.npmjs.org/marked/-/marked-12.0.2.tgz → `package/marked.min.js` | `sha512-qXUm7e/YKFoqFPYPa3Ukg9xlI5cyAtGmyEIzMfW//m6kXwCy2Ps9DYf5ioijFKQ8qyuscrHoY04iJGctu2Kg0Q==` | `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894` |
| `purify.min.js` | dompurify | 3.4.16 | Apache-2.0 OR MPL-2.0 (`LICENSE-dompurify.txt`, `LICENSE-dompurify-MPL.txt`) | https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz → `package/dist/purify.min.js` | `sha512-sqo+pNp3qRhCIpbgRi1y8Tgk27Bo2Ry7w0dC1NBeNTdZChWjz9Xb/KOoZbRP/R6pQZ80Qw8YhXw13hWWBbMRnQ==` | `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2` |

## 버전 선택

- **marked:** 이전 화면이 CDN에서 쓰던 12.0.2를 그대로 씁니다. 메이저 버전을 올리면 보고서 표시가 달라질 수 있어 유지했습니다. 출력은 항상 DOMPurify로 정화한 뒤 표시합니다.
- **DOMPurify:** 이전에는 3.1.6을 썼습니다. 정화기는 보안 수정이 중요하므로, 같은 메이저(3.x)에서 공개된 지 2주가 지난 가장 최신 판인 3.4.16으로 올렸습니다. 공개 직후 판은 공급망 위험 때문에 피했습니다.

## 글꼴

이전에는 Pretendard 웹폰트를 CDN에서 불러왔지만, 이제 쓰지 않습니다. 시스템 한글 글꼴(Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR 등)을 씁니다. 글꼴 파일을 반입하지 않아 라이선스·용량 부담이 없습니다.

## 업데이트 절차

1. 레지스트리에서 새 버전의 `dist.integrity`를 확인합니다.
2. tarball을 받아 sha512를 대조합니다.
3. 필요한 빌드 파일만 복사합니다.
4. 이 표와 `tests/test_web_quality.py`의 sha256을 함께 갱신합니다.
