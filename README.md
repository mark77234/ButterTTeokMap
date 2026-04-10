# 버터떡지도

카카오 지도 기반으로 버터떡 판매점을 표시하는 Streamlit 앱입니다.

- 빵 아이콘 마커로 판매점 위치 표시
- 마커 클릭 시 판매점 **이미지/가격/정보 카드** 표시
- 상단 고정 헤더에서 **지도 위치 검색**
- **현재위치 이동**, **근처 버터떡 지점 보기**, **전체 지점 보기** 지원
- 모바일/데스크톱 반응형 전체화면 지도

## 1. 사전 준비

- Python 3.10 이상
- 카카오 개발자 계정

## 2. 카카오 API 키 설정

카카오 개발자 콘솔에서 앱 생성 후 아래 키를 준비하세요.

- `KAKAO_REST_API_KEY`
- `KAKAO_JAVASCRIPT_KEY`

플랫폼(Web) 도메인에 아래를 등록하세요.

- `http://127.0.0.1:18510` (지도 HTML이 서빙되는 도메인)
- `http://localhost:8501`
- `http://127.0.0.1:8501`

그리고 카카오맵 관련 사용 설정이 활성화되어 있어야 합니다.

## 3. 로컬 실행

프로젝트 폴더로 이동:

```bash
cd buttertteok-sales-map
```

가상환경 생성/활성화:

```bash
python -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

패키지 설치:

```bash
pip install -r requirements.txt
```

환경변수 파일 생성:

```bash
cp .env.example .env
```

`.env` 파일에 발급받은 키 입력:

```env
KAKAO_REST_API_KEY=your_rest_api_key
KAKAO_JAVASCRIPT_KEY=your_javascript_key
```

앱 실행:

```bash
streamlit run app.py
```

브라우저에서 `http://localhost:8501` 접속.

## 4. 사용 방법

1. 지도에서 빵 아이콘 마커 클릭  
2. 우측 하단 카드에서 판매점 이미지/가격/상세 정보 확인  
3. 상단 검색창에 장소 입력 후 `위치 검색` 클릭  
4. `현재위치 이동`으로 내 위치로 이동 (브라우저 위치 권한 허용 필요)  
5. `근처 버터떡 지점 보기`로 현재 위치 기준 가까운 지점만 보기  
6. `전체 지점 보기`로 전체 마커 다시 보기

## 5. 자주 발생하는 문제

- 지도 SDK 로드 실패: JavaScript 키 또는 Web 도메인 등록 확인
- 검색/데이터 실패: REST API 키 확인
- 현재위치 실패: 브라우저 위치 권한 허용 확인
