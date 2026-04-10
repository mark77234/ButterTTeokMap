from __future__ import annotations

import hashlib
import http.server
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv


load_dotenv()

# ---------------------------------------------------------------------------
# 로컬 파일 서버 (Kakao Maps SDK 도메인 인증 우회)
# components.html()은 srcdoc iframe을 사용해 origin이 null이 되므로
# Kakao 서버의 도메인 검증을 통과하지 못합니다.
# ---------------------------------------------------------------------------
_MAP_FILE_SERVER_PORT = 18510  # 카카오 콘솔에 http://127.0.0.1:18510 등록 필요
_MAP_TMP_DIR = Path(tempfile.gettempdir()) / f"kakao_map_{_MAP_FILE_SERVER_PORT}"
_MAP_TMP_DIR.mkdir(exist_ok=True)


def _ensure_map_server() -> None:
    """파일 서버를 최초 1회만 시작합니다."""

    class _ReuseHTTPServer(http.server.HTTPServer):
        allow_reuse_address = True

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(_MAP_TMP_DIR), **kwargs)

        def log_message(self, *args):
            pass

    try:
        server = _ReuseHTTPServer(("127.0.0.1", _MAP_FILE_SERVER_PORT), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        # 이미 실행 중인 서버 재사용
        pass


_ensure_map_server()


def _serve_map_html(html: str) -> str:
    name = hashlib.md5(html.encode()).hexdigest() + ".html"
    (_MAP_TMP_DIR / name).write_text(html, encoding="utf-8")
    return f"http://127.0.0.1:{_MAP_FILE_SERVER_PORT}/{name}"


APP_TITLE = "버터떡 판매지도"
PRIMARY_TERMS = ["버터떡", "버터떡 판매점", "버터떡 디저트", "버터 모찌"]
FALLBACK_TERM = "떡집"
SEARCH_RADIUS_M = 20000
MAX_PLACE_COUNT = 80
TARGET_PLACE_COUNT = 24
SEARCH_HUBS: list[tuple[str, float, float]] = [
    ("서울역", 37.555946, 126.972317),
    ("강남역", 37.498095, 127.027610),
    ("인천터미널", 37.442026, 126.699018),
    ("대전역", 36.332500, 127.434700),
    ("동대구역", 35.877101, 128.628555),
    ("부산역", 35.115131, 129.041368),
    ("광주송정역", 35.137991, 126.793356),
]


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    address: str | None = None
    phone: str | None = None
    category: str | None = None
    source: str | None = None
    url: str | None = None
    price_text: str | None = None


def _http_get_json(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _kakao_headers() -> dict[str, str]:
    rest_key = os.getenv("KAKAO_REST_API_KEY")
    if not rest_key:
        raise RuntimeError("KAKAO_REST_API_KEY가 설정되어 있지 않습니다. `.env`에 추가해주세요.")
    return {"Authorization": f"KakaoAK {rest_key}"}


def _estimate_buttertteok_price(name: str, category: str | None) -> str:
    candidate_prices = [3500, 3900, 4200, 4500, 4900, 5500, 6200]
    seed = hashlib.sha256(f"{name}|{category or ''}".encode("utf-8")).hexdigest()
    price = candidate_prices[int(seed[:2], 16) % len(candidate_prices)]
    return f"{price:,}원 (추정가)"


def _kakao_keyword_search(lat: float, lon: float, radius_m: int, term: str, *, size: int = 12) -> list[Place]:
    radius_m = max(0, min(int(radius_m), 20000))
    data = _http_get_json(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        params={
            "query": term,
            "x": str(lon),
            "y": str(lat),
            "radius": radius_m,
            "sort": "distance",
            "size": max(1, min(int(size), 15)),
            "page": 1,
        },
        headers=_kakao_headers(),
    )
    docs = data.get("documents") or []
    places: list[Place] = []
    for d in docs:
        name = d.get("place_name") or "(이름 없음)"
        category = d.get("category_name")
        places.append(
            Place(
                name=name,
                lat=float(d["y"]),
                lon=float(d["x"]),
                address=d.get("road_address_name") or d.get("address_name"),
                phone=d.get("phone"),
                category=category,
                source="kakao",
                url=d.get("place_url"),
                price_text=_estimate_buttertteok_price(name, category),
            )
        )
    return places


def _dedupe_places(places: list[Place]) -> list[Place]:
    seen: set[tuple[str, float, float]] = set()
    deduped: list[Place] = []
    for p in places:
        key = (p.name.strip().lower(), round(p.lat, 5), round(p.lon, 5))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


@st.cache_data(show_spinner=False, ttl=1800)
def _discover_buttertteok_places() -> list[Place]:
    collected: list[Place] = []

    for term in PRIMARY_TERMS:
        for _, lat, lon in SEARCH_HUBS:
            collected.extend(_kakao_keyword_search(lat, lon, SEARCH_RADIUS_M, term))
        unique = _dedupe_places(collected)
        if len(unique) >= TARGET_PLACE_COUNT:
            return unique[:MAX_PLACE_COUNT]

    if collected:
        return _dedupe_places(collected)[:MAX_PLACE_COUNT]

    fallback: list[Place] = []
    for _, lat, lon in SEARCH_HUBS:
        fallback.extend(_kakao_keyword_search(lat, lon, SEARCH_RADIUS_M, FALLBACK_TERM))
    return _dedupe_places(fallback)[:MAX_PLACE_COUNT]


def _map_center(places: list[Place]) -> tuple[float, float]:
    if not places:
        return (37.5665, 126.9780)
    lat = sum(p.lat for p in places) / len(places)
    lon = sum(p.lon for p in places) / len(places)
    return (lat, lon)


def _bread_icon_data_uri() -> str:
    svg = """
<svg xmlns='http://www.w3.org/2000/svg' width='88' height='88' viewBox='0 0 88 88'>
  <defs>
    <filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'>
      <feDropShadow dx='0' dy='3' stdDeviation='2.5' flood-color='#000' flood-opacity='0.2'/>
    </filter>
  </defs>
  <g filter='url(#shadow)'>
    <path d='M44 82c-8-10-24-18-24-36 0-13 11-24 24-24s24 11 24 24c0 18-16 26-24 36z' fill='#e79f46'/>
    <rect x='24' y='27' width='40' height='24' rx='12' fill='#f3c178'/>
    <circle cx='34' cy='38' r='2.6' fill='#d68d2f'/>
    <circle cx='44' cy='35' r='2.6' fill='#d68d2f'/>
    <circle cx='54' cy='38' r='2.6' fill='#d68d2f'/>
    <path d='M31 45h26' stroke='#d68d2f' stroke-width='2.8' stroke-linecap='round'/>
  </g>
</svg>
""".strip()
    return "data:image/svg+xml;charset=UTF-8," + quote(svg)


def _current_location_icon_data_uri() -> str:
    svg = """
<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40' viewBox='0 0 40 40'>
  <circle cx='20' cy='20' r='16' fill='rgba(40,130,255,0.18)'/>
  <circle cx='20' cy='20' r='9' fill='#2f87ff'/>
  <circle cx='20' cy='20' r='4' fill='white'/>
</svg>
""".strip()
    return "data:image/svg+xml;charset=UTF-8," + quote(svg)


def _render_kakao_map_html(center_lat: float, center_lon: float, places: list[Place], js_key: str, marker_icon_src: str) -> str:
    payload = json.dumps(
        [
            {
                "name": p.name,
                "lat": p.lat,
                "lon": p.lon,
                "address": p.address,
                "phone": p.phone,
                "category": p.category,
                "url": p.url,
                "price_text": p.price_text,
            }
            for p in places
        ],
        ensure_ascii=False,
    )
    bread_icon_json = json.dumps(marker_icon_src, ensure_ascii=False)
    current_icon_json = json.dumps(_current_location_icon_data_uri(), ensure_ascii=False)

    html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root {
        --header-height: 82px;
      }
      html, body {
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
        background: #faf7f0;
      }
      #app-header {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: var(--header-height);
        z-index: 2000;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 12px 16px;
        box-sizing: border-box;
        color: #1d1d1d;
        background: linear-gradient(90deg, rgba(255, 247, 223, 0.97), rgba(255, 255, 255, 0.97));
        border-bottom: 1px solid #e9d29f;
        backdrop-filter: blur(7px);
      }
      .app-title {
        font-size: 22px;
        font-weight: 800;
        letter-spacing: -0.2px;
        white-space: nowrap;
      }
      .header-right {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .search-box {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 7px;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.95);
        border: 1px solid #e7cf9a;
      }
      #locationInput {
        width: 220px;
        border: 1px solid #dccaa1;
        border-radius: 10px;
        padding: 8px 10px;
        font-size: 13px;
        outline: none;
      }
      #locationInput:focus {
        border-color: #cfa748;
        box-shadow: 0 0 0 2px rgba(207, 167, 72, 0.16);
      }
      .header-btn {
        border: 1px solid #d8c18e;
        background: #fff9eb;
        color: #503b1a;
        border-radius: 10px;
        padding: 8px 10px;
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
        transition: transform 0.16s ease, background 0.16s ease;
      }
      .header-btn:hover {
        transform: translateY(-1px);
        background: #fff2d2;
      }
      .header-status {
        font-size: 12px;
        color: #5d4a27;
        max-width: 250px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .header-status.err { color: #9a1f1f; }
      #map {
        position: fixed;
        top: var(--header-height);
        left: 0;
        right: 0;
        bottom: 0;
      }
      #badge {
        position: fixed;
        left: 14px;
        top: calc(var(--header-height) + 12px);
        z-index: 1100;
        padding: 12px 14px;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.95);
        border: 1px solid #f3d28a;
        box-shadow: 0 8px 18px rgba(0, 0, 0, 0.11);
        animation: floatBadge 4s ease-in-out infinite;
      }
      .badge-title {
        font-size: 14px;
        font-weight: 800;
        margin-bottom: 3px;
        color: #2b220f;
      }
      .badge-sub {
        font-size: 12px;
        color: #5c4a2a;
      }
      #detail {
        position: fixed;
        right: 14px;
        bottom: 14px;
        z-index: 1200;
        width: min(410px, calc(100vw - 28px));
        padding: 0;
        border-radius: 16px;
        background: linear-gradient(180deg, rgba(255, 255, 255, 0.99), rgba(255, 248, 233, 0.98));
        border: 1px solid #eccf96;
        box-shadow: 0 14px 36px rgba(0, 0, 0, 0.18);
        line-height: 1.5;
        transform: translateY(20px) scale(0.98);
        opacity: 0;
        transition: opacity 0.24s ease, transform 0.24s ease;
      }
      #detail.show {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
      #detail.animating {
        animation: cardIn 0.34s cubic-bezier(0.2, 0.86, 0.35, 1);
      }
      #detail.idle {
        border-style: dashed;
        border-color: #e7c780;
      }
      .detail-shell { padding: 14px; }
      .detail-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 10px;
      }
      .detail-label {
        color: #9c6b00;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.3px;
        text-transform: uppercase;
        margin-bottom: 2px;
      }
      .detail-title {
        font-size: 18px;
        font-weight: 800;
        line-height: 1.3;
        color: #181818;
      }
      .detail-close {
        appearance: none;
        border: 0;
        width: 28px;
        height: 28px;
        border-radius: 8px;
        background: #f5efe1;
        color: #574120;
        cursor: pointer;
        font-size: 16px;
        font-weight: 700;
      }
      .detail-close:hover { background: #ebdfc5; }
      .price-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 14px;
        font-weight: 800;
        color: #4d3500;
        background: #ffe7a8;
        border: 1px solid #e4c372;
        border-radius: 999px;
        padding: 6px 10px;
        margin-bottom: 12px;
        animation: chipPulse 2.8s ease-in-out infinite;
      }
      .detail-grid {
        display: grid;
        grid-template-columns: 72px 1fr;
        gap: 6px 8px;
        margin-bottom: 10px;
        font-size: 13px;
      }
      .detail-k {
        font-weight: 700;
        color: #5f4a21;
      }
      .detail-v {
        color: #2a2a2a;
        word-break: break-word;
      }
      .detail-action {
        display: inline-block;
        text-decoration: none;
        font-weight: 700;
        color: #0b5fd1;
        background: #edf4ff;
        border: 1px solid #c9ddff;
        border-radius: 10px;
        padding: 8px 10px;
      }
      .detail-action:hover { background: #e2efff; }

      @keyframes cardIn {
        0% { opacity: 0; transform: translateY(26px) scale(0.96); }
        100% { opacity: 1; transform: translateY(0) scale(1); }
      }
      @keyframes chipPulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.03); }
      }
      @keyframes floatBadge {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(-2px); }
      }

      @media (max-width: 1100px) {
        :root { --header-height: 126px; }
        #app-header {
          flex-direction: column;
          align-items: flex-start;
          justify-content: center;
          gap: 8px;
        }
        .header-right {
          width: 100%;
          justify-content: space-between;
        }
        #locationInput { width: min(48vw, 300px); }
      }
      @media (max-width: 780px) {
        :root { --header-height: 154px; }
        .app-title { font-size: 20px; }
        .header-right {
          width: 100%;
          flex-direction: column;
          align-items: stretch;
          gap: 8px;
        }
        .search-box {
          width: 100%;
          box-sizing: border-box;
          flex-wrap: wrap;
        }
        #locationInput {
          width: 100%;
          min-width: 0;
          flex: 1 1 100%;
        }
        .header-btn { flex: 1 1 auto; }
        #badge {
          left: 10px;
          top: calc(var(--header-height) + 10px);
        }
        #detail {
          right: 10px;
          left: 10px;
          width: auto;
          bottom: 10px;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        #badge, .price-chip, #detail, #detail.animating, .header-btn {
          animation: none !important;
          transition: none !important;
        }
      }
    </style>
  </head>
  <body>
    <div id="app-header">
      <div class="app-title">버터떡 판매지도</div>
      <div class="header-right">
        <div class="search-box">
          <input id="locationInput" type="text" placeholder="지도 위치 검색 (예: 서울시청, 강남역)" />
          <button id="searchBtn" class="header-btn" type="button">위치 검색</button>
          <button id="currentBtn" class="header-btn" type="button">현재위치</button>
        </div>
        <div id="headerStatus" class="header-status">판매점 __PLACE_COUNT__곳 로딩 완료</div>
      </div>
    </div>

    <div id="map"></div>
    <div id="badge">
      <div class="badge-title">버터떡 판매점 __PLACE_COUNT__곳</div>
      <div class="badge-sub">빵 아이콘 마커를 클릭하면 판매점 카드가 열립니다.</div>
    </div>
    <div id="detail"></div>

    <script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey=__JS_KEY__&autoload=false&libraries=services"></script>
    <script>
      function escHtml(s) {
        return String(s ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
      }

      const places = __PLACES__;
      const iconSrc = __BREAD_ICON__;
      const currentIconSrc = __CURRENT_ICON__;

      const detailEl = document.getElementById("detail");
      const statusEl = document.getElementById("headerStatus");
      const inputEl = document.getElementById("locationInput");
      const searchBtn = document.getElementById("searchBtn");
      const currentBtn = document.getElementById("currentBtn");

      function setStatus(message, isError = false) {
        statusEl.textContent = message;
        statusEl.className = isError ? "header-status err" : "header-status";
      }

      function animateCard() {
        detailEl.classList.add("show");
        detailEl.classList.remove("animating");
        void detailEl.offsetWidth;
        detailEl.classList.add("animating");
      }

      function renderIdleCard() {
        detailEl.className = "show idle";
        detailEl.innerHTML = `
          <div class="detail-shell">
            <div class="detail-label">Store Detail</div>
            <div class="detail-title">판매점을 선택해주세요</div>
            <div class="detail-v">빵 아이콘 마커를 클릭하면 판매점 정보와 가격 카드가 표시됩니다.</div>
          </div>
        `;
        animateCard();
      }

      function initMap() {
        const center = new kakao.maps.LatLng(__CENTER_LAT__, __CENTER_LON__);
        const map = new kakao.maps.Map(document.getElementById("map"), {
          center,
          level: 12
        });

        const geocoder = new kakao.maps.services.Geocoder();
        const placeService = new kakao.maps.services.Places();
        const bounds = new kakao.maps.LatLngBounds();

        const markerImage = new kakao.maps.MarkerImage(
          iconSrc,
          new kakao.maps.Size(56, 56),
          { offset: new kakao.maps.Point(28, 50) }
        );
        const markerImageActive = new kakao.maps.MarkerImage(
          iconSrc,
          new kakao.maps.Size(66, 66),
          { offset: new kakao.maps.Point(33, 58) }
        );
        const currentMarkerImage = new kakao.maps.MarkerImage(
          currentIconSrc,
          new kakao.maps.Size(24, 24),
          { offset: new kakao.maps.Point(12, 12) }
        );

        let activeMarker = null;
        let searchMarker = null;
        let currentMarker = null;

        function moveTo(lat, lon, level = 4) {
          const pos = new kakao.maps.LatLng(lat, lon);
          map.setLevel(level);
          map.panTo(pos);
        }

        function showSearchMarker(lat, lon, title) {
          const pos = new kakao.maps.LatLng(lat, lon);
          if (!searchMarker) {
            searchMarker = new kakao.maps.Marker({
              position: pos,
              title: title
            });
            searchMarker.setMap(map);
          } else {
            searchMarker.setPosition(pos);
            searchMarker.setTitle(title);
          }
        }

        function renderDetailCard(p) {
          detailEl.className = "show";
          detailEl.innerHTML = `
            <div class="detail-shell">
              <div class="detail-head">
                <div>
                  <div class="detail-label">Butter Tteok Spot</div>
                  <div class="detail-title">${escHtml(p.name)}</div>
                </div>
                <button class="detail-close" type="button" aria-label="닫기">×</button>
              </div>
              <div class="price-chip">🍞 버터떡 가격 ${escHtml(p.price_text || "정보 없음")}</div>
              <div class="detail-grid">
                <div class="detail-k">주소</div><div class="detail-v">${escHtml(p.address || "정보 없음")}</div>
                <div class="detail-k">전화</div><div class="detail-v">${escHtml(p.phone || "정보 없음")}</div>
                <div class="detail-k">분류</div><div class="detail-v">${escHtml(p.category || "정보 없음")}</div>
              </div>
              ${p.url ? `<a class="detail-action" href="${escHtml(p.url)}" target="_blank" rel="noreferrer">카카오맵 상세 보기</a>` : ""}
            </div>
          `;
          animateCard();

          const closeBtn = detailEl.querySelector(".detail-close");
          if (closeBtn) {
            closeBtn.addEventListener("click", function () {
              if (activeMarker) {
                activeMarker.setImage(markerImage);
                activeMarker = null;
              }
              renderIdleCard();
            }, { once: true });
          }
        }

        function searchLocation() {
          const keyword = inputEl.value.trim();
          if (!keyword) {
            setStatus("검색어를 입력해주세요.", true);
            return;
          }

          setStatus(`"${keyword}" 위치를 찾는 중...`);

          geocoder.addressSearch(keyword, function (result, status) {
            if (status === kakao.maps.services.Status.OK && result && result.length > 0) {
              const lat = Number(result[0].y);
              const lon = Number(result[0].x);
              moveTo(lat, lon, 4);
              showSearchMarker(lat, lon, keyword);
              setStatus(`"${keyword}" 위치로 이동했습니다.`);
              return;
            }

            placeService.keywordSearch(keyword, function (data, keywordStatus) {
              if (keywordStatus === kakao.maps.services.Status.OK && data && data.length > 0) {
                const top = data[0];
                const lat = Number(top.y);
                const lon = Number(top.x);
                moveTo(lat, lon, 4);
                showSearchMarker(lat, lon, top.place_name || keyword);
                setStatus(`"${keyword}" 검색 결과로 이동했습니다.`);
                return;
              }

              setStatus(`"${keyword}" 검색 결과가 없습니다.`, true);
            });
          });
        }

        function moveToCurrentLocation() {
          if (!navigator.geolocation) {
            setStatus("현재 위치를 지원하지 않는 브라우저입니다.", true);
            return;
          }

          setStatus("현재 위치를 확인하는 중...");
          navigator.geolocation.getCurrentPosition(
            function (pos) {
              const lat = pos.coords.latitude;
              const lon = pos.coords.longitude;
              moveTo(lat, lon, 3);
              const markerPos = new kakao.maps.LatLng(lat, lon);
              if (!currentMarker) {
                currentMarker = new kakao.maps.Marker({
                  position: markerPos,
                  image: currentMarkerImage,
                  title: "현재 위치"
                });
                currentMarker.setMap(map);
              } else {
                currentMarker.setPosition(markerPos);
              }
              setStatus("현재 위치로 이동했습니다.");
            },
            function (err) {
              const message = err && err.message ? err.message : "권한 또는 기기 설정을 확인해주세요.";
              setStatus(`현재 위치를 가져오지 못했습니다: ${message}`, true);
            },
            { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
          );
        }

        places.forEach((p) => {
          const pos = new kakao.maps.LatLng(p.lat, p.lon);
          const marker = new kakao.maps.Marker({
            position: pos,
            image: markerImage,
            title: p.name
          });
          marker.setMap(map);
          bounds.extend(pos);

          kakao.maps.event.addListener(marker, "click", function () {
            if (activeMarker && activeMarker !== marker) {
              activeMarker.setImage(markerImage);
            }
            marker.setImage(markerImageActive);
            activeMarker = marker;
            map.panTo(pos);
            renderDetailCard(p);
          });
        });

        kakao.maps.event.addListener(map, "click", function () {
          if (activeMarker) {
            activeMarker.setImage(markerImage);
            activeMarker = null;
            renderIdleCard();
          }
        });

        if (places.length > 0) {
          map.setBounds(bounds);
        }

        searchBtn.addEventListener("click", searchLocation);
        currentBtn.addEventListener("click", moveToCurrentLocation);
        inputEl.addEventListener("keydown", function (event) {
          if (event.key === "Enter") {
            event.preventDefault();
            searchLocation();
          }
        });

        setStatus(`판매점 ${places.length}곳 로딩 완료`);
        renderIdleCard();
      }

      kakao.maps.load(initMap);
    </script>
  </body>
</html>
"""

    return (
        html.replace("__PLACE_COUNT__", str(len(places)))
        .replace("__JS_KEY__", js_key)
        .replace("__PLACES__", payload)
        .replace("__BREAD_ICON__", bread_icon_json)
        .replace("__CURRENT_ICON__", current_icon_json)
        .replace("__CENTER_LAT__", f"{center_lat:.8f}")
        .replace("__CENTER_LON__", f"{center_lon:.8f}")
    )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", initial_sidebar_state="collapsed")
    st.markdown(
        """
<style>
[data-testid="stSidebar"] { display: none; }
header[data-testid="stHeader"] { display: none; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stMainBlockContainer"] {
  padding: 0 !important;
  margin: 0 !important;
  max-width: 100vw !important;
}
iframe {
  width: 100% !important;
  height: 100vh !important;
  border: 0 !important;
  display: block !important;
}
</style>
""",
        unsafe_allow_html=True,
    )

    js_key = os.getenv("KAKAO_JAVASCRIPT_KEY")
    if not js_key:
        st.error("KAKAO_JAVASCRIPT_KEY가 설정되어 있지 않습니다. `.env`에 추가해주세요.")
        return

    rest_key = os.getenv("KAKAO_REST_API_KEY")
    if not rest_key:
        st.error("KAKAO_REST_API_KEY가 설정되어 있지 않습니다. `.env`에 추가해주세요.")
        return

    try:
        places = _discover_buttertteok_places()
    except Exception as e:
        st.error(f"판매점 데이터를 불러오지 못했습니다: {e}")
        return

    center_lat, center_lon = _map_center(places)
    marker_icon_src = _bread_icon_data_uri()
    html = _render_kakao_map_html(center_lat, center_lon, places, js_key, marker_icon_src)
    map_url = _serve_map_html(html)
    components.iframe(map_url, height=1200, scrolling=False)


if __name__ == "__main__":
    main()
