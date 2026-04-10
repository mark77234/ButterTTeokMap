from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv


load_dotenv()

APP_TITLE = "버터떡지도"
PRIMARY_TERMS = ["버터떡", "버터떡 판매점", "버터떡 디저트", "버터 모찌"]
FALLBACK_TERM = "떡집"
SEARCH_RADIUS_M = 20000
MAX_PLACE_COUNT = 80
TARGET_PLACE_COUNT = 24
IMAGE_ENRICH_LIMIT = 28
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
    image_url: str | None = None


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _kakao_headers() -> dict[str, str]:
    rest_key = os.getenv("KAKAO_REST_API_KEY")
    if not rest_key:
        raise RuntimeError(
            "KAKAO_REST_API_KEY가 설정되어 있지 않습니다. `.env`에 추가해주세요."
        )
    return {"Authorization": f"KakaoAK {rest_key}"}


def _estimate_buttertteok_price(name: str, category: str | None) -> str:
    candidate_prices = [3500, 3900, 4200, 4500, 4900, 5500, 6200]
    seed = hashlib.sha256(f"{name}|{category or ''}".encode("utf-8")).hexdigest()
    price = candidate_prices[int(seed[:2], 16) % len(candidate_prices)]
    return f"{price:,}원 (추정가)"


def _kakao_keyword_search(
    lat: float, lon: float, radius_m: int, term: str, *, size: int = 12
) -> list[Place]:
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


def _kakao_image_search_url(query: str) -> str | None:
    data = _http_get_json(
        "https://dapi.kakao.com/v2/search/image",
        params={"query": query, "sort": "accuracy", "page": 1, "size": 1},
        headers=_kakao_headers(),
    )
    docs = data.get("documents") or []
    if not docs:
        return None
    top = docs[0]
    return top.get("thumbnail_url") or top.get("image_url")


@st.cache_data(show_spinner=False, ttl=43200)
def _lookup_place_image(
    name: str, category: str | None, address: str | None
) -> str | None:
    region = ((address or "").strip().split(" ")[0] if address else "").strip()
    queries = [
        f"{name} 버터떡",
        f"{name} 떡집",
        f"{region} 버터떡" if region else "",
        f"{category} 버터떡" if category else "",
        "버터떡 디저트",
    ]
    seen: set[str] = set()
    for q in queries:
        query = q.strip()
        if not query or query in seen:
            continue
        seen.add(query)
        try:
            image_url = _kakao_image_search_url(query)
        except requests.RequestException:
            return None
        if image_url:
            return image_url
    return None


def _enrich_places_with_images(places: list[Place]) -> list[Place]:
    enriched: list[Place] = []
    for idx, p in enumerate(places):
        image_url: str | None = None
        if idx < IMAGE_ENRICH_LIMIT:
            image_url = _lookup_place_image(p.name, p.category, p.address)
        enriched.append(
            Place(
                name=p.name,
                lat=p.lat,
                lon=p.lon,
                address=p.address,
                phone=p.phone,
                category=p.category,
                source=p.source,
                url=p.url,
                price_text=p.price_text,
                image_url=image_url,
            )
        )
    return enriched


@st.cache_data(show_spinner=False, ttl=1800)
def _discover_buttertteok_places() -> list[Place]:
    collected: list[Place] = []

    for term in PRIMARY_TERMS:
        for _, lat, lon in SEARCH_HUBS:
            collected.extend(_kakao_keyword_search(lat, lon, SEARCH_RADIUS_M, term))
        unique = _dedupe_places(collected)
        if len(unique) >= TARGET_PLACE_COUNT:
            return _enrich_places_with_images(unique[:MAX_PLACE_COUNT])

    if collected:
        return _enrich_places_with_images(_dedupe_places(collected)[:MAX_PLACE_COUNT])

    fallback: list[Place] = []
    for _, lat, lon in SEARCH_HUBS:
        fallback.extend(_kakao_keyword_search(lat, lon, SEARCH_RADIUS_M, FALLBACK_TERM))
    return _enrich_places_with_images(_dedupe_places(fallback)[:MAX_PLACE_COUNT])


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
    <linearGradient id='butter' x1='0' x2='1' y1='0' y2='1'>
      <stop offset='0%' stop-color='#ffe7ab'/>
      <stop offset='100%' stop-color='#ffcf6f'/>
    </linearGradient>
    <filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'>
      <feDropShadow dx='0' dy='3' stdDeviation='2.5' flood-color='#000' flood-opacity='0.2'/>
    </filter>
  </defs>
  <g filter='url(#shadow)'>
    <path d='M44 82c-8-10-24-18-24-36 0-13 11-24 24-24s24 11 24 24c0 18-16 26-24 36z' fill='#e79f46'/>
    <rect x='24' y='27' width='40' height='24' rx='12' fill='url(#butter)'/>
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
  <circle cx='20' cy='20' r='16' fill='rgba(52,140,255,0.2)'/>
  <circle cx='20' cy='20' r='10' fill='#2c82ff'/>
  <circle cx='20' cy='20' r='4' fill='white'/>
</svg>
""".strip()
    return "data:image/svg+xml;charset=UTF-8," + quote(svg)


def _render_kakao_map_html(
    center_lat: float, center_lon: float, places: list[Place], js_key: str
) -> str:
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
                "image_url": p.image_url,
            }
            for p in places
        ],
        ensure_ascii=False,
    )
    bread_icon_json = json.dumps(_bread_icon_data_uri(), ensure_ascii=False)
    current_icon_json = json.dumps(
        _current_location_icon_data_uri(), ensure_ascii=False
    )

    template = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <link rel="icon" href="__BREAD_ICON__" />
    <title>__APP_TITLE__</title>
    <style>
      :root {
        --header-height: 102px;
      }
      html, body {
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        font-family: "Pretendard", "Apple SD Gothic Neo", "Noto Sans KR", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: linear-gradient(180deg, #fffaf1 0%, #fff6df 100%);
      }
      #app-header {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: var(--header-height);
        z-index: 3000;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 10px 14px;
        box-sizing: border-box;
        background:
          radial-gradient(circle at 14% 0%, rgba(255, 224, 157, 0.35), transparent 45%),
          radial-gradient(circle at 90% 0%, rgba(255, 240, 205, 0.5), transparent 44%),
          rgba(255, 251, 241, 0.94);
        border-bottom: 1px solid #e8d0a2;
        backdrop-filter: blur(8px);
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 10px;
        min-width: 160px;
      }
      .brand-icon {
        width: 44px;
        height: 44px;
        border-radius: 12px;
        display: grid;
        place-items: center;
        font-size: 24px;
        background: linear-gradient(180deg, #ffe9b1, #ffd882);
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.8), 0 6px 14px rgba(167, 118, 39, 0.22);
      }
      .brand-title {
        font-size: 24px;
        line-height: 1.05;
        font-weight: 900;
        letter-spacing: -0.4px;
        color: #2e2312;
      }
      .brand-sub {
        font-size: 12px;
        color: #6c562f;
        margin-top: 2px;
      }
      .toolbar {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 8px;
        min-width: 300px;
      }
      .toolbar-row {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }
      #locationInput {
        min-width: 180px;
        flex: 1 1 230px;
        border: 1px solid #dcc9a3;
        border-radius: 12px;
        padding: 10px 12px;
        font-size: 13px;
        background: rgba(255,255,255,0.95);
        color: #1f1a12;
        outline: none;
      }
      #locationInput:focus {
        border-color: #cfab58;
        box-shadow: 0 0 0 2px rgba(207, 171, 88, 0.18);
      }
      .btn {
        border: 1px solid #d8be88;
        background: linear-gradient(180deg, #fff6dc, #ffefc6);
        color: #4a3616;
        border-radius: 11px;
        padding: 9px 11px;
        font-size: 13px;
        font-weight: 800;
        cursor: pointer;
        white-space: nowrap;
        transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
      }
      .btn:hover {
        transform: translateY(-1px);
        box-shadow: 0 8px 16px rgba(159, 117, 45, 0.15);
        background: linear-gradient(180deg, #fff7e0, #ffe7ad);
      }
      #headerStatus {
        font-size: 12px;
        color: #5f4b28;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      #headerStatus.err {
        color: #9e2424;
      }
      #map {
        position: fixed;
        top: var(--header-height);
        left: 0;
        right: 0;
        bottom: 0;
      }
      #badge {
        position: fixed;
        left: 12px;
        top: calc(var(--header-height) + 12px);
        z-index: 2000;
        padding: 12px 14px;
        border-radius: 14px;
        background: rgba(255,255,255,0.93);
        border: 1px solid #edd19a;
        box-shadow: 0 12px 24px rgba(0, 0, 0, 0.12);
        animation: softFloat 4s ease-in-out infinite;
      }
      .badge-title {
        font-size: 14px;
        font-weight: 900;
        color: #33260f;
        margin-bottom: 2px;
      }
      .badge-sub {
        font-size: 12px;
        color: #654f28;
      }
      #detail {
        position: fixed;
        right: 12px;
        bottom: 12px;
        z-index: 2400;
        width: min(430px, calc(100vw - 24px));
        border-radius: 18px;
        border: 1px solid #e7c57f;
        background:
          radial-gradient(circle at 95% 0%, rgba(255, 226, 150, 0.35), transparent 30%),
          radial-gradient(circle at 6% 100%, rgba(255, 240, 205, 0.6), transparent 34%),
          linear-gradient(180deg, rgba(255,255,255,0.99), rgba(255,246,222,0.98));
        box-shadow: 0 20px 42px rgba(0, 0, 0, 0.2);
        overflow: hidden;
        opacity: 0;
        transform: translateY(16px) scale(0.98);
        transition: opacity .24s ease, transform .24s ease;
      }
      #detail.show {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
      #detail.animating {
        animation: cardPop .34s cubic-bezier(0.2, 0.86, 0.35, 1);
      }
      #detail.idle {
        border-style: dashed;
        border-color: #e6c170;
      }
      .detail-shell {
        padding: 14px;
      }
      .detail-head {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 8px;
        margin-bottom: 10px;
      }
      .detail-label {
        color: #9b6e11;
        font-size: 11px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        margin-bottom: 2px;
      }
      .detail-title {
        color: #1f180c;
        font-size: 20px;
        font-weight: 900;
        line-height: 1.25;
      }
      .close-btn {
        border: 0;
        width: 30px;
        height: 30px;
        border-radius: 10px;
        background: #f5ead1;
        color: #62461b;
        font-size: 17px;
        font-weight: 800;
        cursor: pointer;
      }
      .close-btn:hover { background: #eadab8; }
      .detail-media {
        position: relative;
        width: 100%;
        height: 160px;
        border-radius: 14px;
        overflow: hidden;
        margin-bottom: 12px;
        border: 1px solid #efd8a8;
        background: linear-gradient(145deg, #fff2cf, #ffe4a8);
      }
      .detail-media img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
      }
      .meta-row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 10px;
      }
      .price-chip, .distance-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 800;
      }
      .price-chip {
        color: #543700;
        background: #ffe299;
        border: 1px solid #e0be6f;
        animation: pulseChip 2.8s ease-in-out infinite;
      }
      .distance-chip {
        color: #114e9d;
        background: #e7f1ff;
        border: 1px solid #b5d0ff;
      }
      .detail-grid {
        display: grid;
        grid-template-columns: 72px 1fr;
        gap: 6px 10px;
        margin-bottom: 12px;
      }
      .detail-k {
        color: #5f4a21;
        font-size: 12px;
        font-weight: 800;
      }
      .detail-v {
        color: #2c2518;
        font-size: 13px;
        word-break: break-word;
      }
      .detail-action {
        display: inline-block;
        text-decoration: none;
        font-size: 13px;
        font-weight: 800;
        color: #0a5ccf;
        background: #ecf3ff;
        border: 1px solid #c6dbff;
        border-radius: 11px;
        padding: 8px 11px;
      }
      .detail-action:hover { background: #e1eeff; }

      @keyframes cardPop {
        0% { opacity: 0; transform: translateY(24px) scale(0.97); }
        100% { opacity: 1; transform: translateY(0) scale(1); }
      }
      @keyframes pulseChip {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.03); }
      }
      @keyframes softFloat {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(-2px); }
      }

      @media (max-width: 1180px) {
        :root { --header-height: 144px; }
        #app-header {
          flex-direction: column;
          align-items: stretch;
          gap: 8px;
        }
        .toolbar { min-width: 0; }
      }
      @media (max-width: 768px) {
        :root { --header-height: 132px; }
        #app-header {
          padding: 8px 10px;
          gap: 6px;
        }
        .brand { gap: 8px; }
        .brand-icon {
          width: 34px;
          height: 34px;
          border-radius: 10px;
          font-size: 18px;
        }
        .brand-title { font-size: 18px; }
        .brand-sub { font-size: 11px; }
        .toolbar { gap: 6px; }
        .toolbar-row {
          gap: 6px;
          flex-wrap: nowrap;
          overflow-x: auto;
          -webkit-overflow-scrolling: touch;
          padding-bottom: 2px;
          scrollbar-width: none;
        }
        .toolbar-row::-webkit-scrollbar { display: none; }
        .btn {
          flex: 0 0 auto;
          text-align: center;
          padding: 8px 9px;
          font-size: 12px;
          border-radius: 10px;
        }
        #locationInput {
          flex: 0 0 150px;
          min-width: 150px;
          padding: 8px 9px;
          font-size: 12px;
          border-radius: 10px;
        }
        #headerStatus { font-size: 11px; }
        #badge { display: none; }
        #detail {
          left: 8px;
          right: 8px;
          width: auto;
          bottom: 8px;
          max-height: 46vh;
          overflow: auto;
          border-radius: 14px;
        }
        .detail-shell { padding: 10px; }
        .detail-title { font-size: 17px; }
        .detail-media {
          height: 108px;
          margin-bottom: 10px;
        }
        .price-chip, .distance-chip {
          font-size: 12px;
          padding: 5px 8px;
        }
        .detail-grid {
          grid-template-columns: 58px 1fr;
          gap: 5px 8px;
          margin-bottom: 10px;
        }
        .detail-k { font-size: 11px; }
        .detail-v { font-size: 12px; }
        .detail-action {
          font-size: 12px;
          padding: 7px 9px;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        #badge, .price-chip, #detail, #detail.animating, .btn {
          animation: none !important;
          transition: none !important;
        }
      }
    </style>
  </head>
  <body>
    <div id="app-header">
      <div class="brand">
        <div class="brand-icon">🍞</div>
        <div>
          <div class="brand-title">__APP_TITLE__</div>
          <div class="brand-sub">버터떡 판매점 지도 서비스</div>
        </div>
      </div>

      <div class="toolbar">
        <div class="toolbar-row">
          <input id="locationInput" type="text" placeholder="지도 위치 검색 (예: 서울시청, 강남역, 부산역)" />
          <button id="searchBtn" class="btn" type="button">위치 검색</button>
          <button id="currentBtn" class="btn" type="button">현재위치 이동</button>
          <button id="nearbyBtn" class="btn" type="button">근처 버터떡 지점 보기</button>
          <button id="allBtn" class="btn" type="button">전체 지점 보기</button>
        </div>
        <div id="headerStatus">버터떡 판매점 __PLACE_COUNT__곳 로딩 완료</div>
      </div>
    </div>

    <div id="map"></div>

    <div id="badge">
      <div class="badge-title">버터떡 판매점 __PLACE_COUNT__곳</div>
      <div class="badge-sub">빵 아이콘 마커를 누르면 판매 정보 카드가 열립니다.</div>
    </div>

    <div id="detail"></div>

    <script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey=__JS_KEY__&autoload=false&libraries=services&https=true"></script>
    <script>
      const APP_TITLE = "__APP_TITLE__";
      const STORE_NEARBY_RADIUS_M = 5000;
      const STORE_NEARBY_LIMIT = 12;
      const rawPlaces = __PLACES__;
      const breadIcon = __BREAD_ICON__;
      const currentIcon = __CURRENT_ICON__;
      const defaultCenter = { lat: __CENTER_LAT__, lon: __CENTER_LON__ };

      const detailEl = document.getElementById("detail");
      const statusEl = document.getElementById("headerStatus");
      const inputEl = document.getElementById("locationInput");
      const searchBtn = document.getElementById("searchBtn");
      const currentBtn = document.getElementById("currentBtn");
      const nearbyBtn = document.getElementById("nearbyBtn");
      const allBtn = document.getElementById("allBtn");

      function escHtml(value) {
        return String(value ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
      }

      function setStatus(message, isError = false) {
        statusEl.textContent = message;
        statusEl.className = isError ? "err" : "";
      }

      function estimatePrice(name) {
        const prices = [3500, 3900, 4200, 4500, 4900, 5500, 6200];
        let h = 0;
        for (let i = 0; i < name.length; i += 1) {
          h = (h * 31 + name.charCodeAt(i)) >>> 0;
        }
        const picked = prices[h % prices.length];
        return `${picked.toLocaleString("ko-KR")}원 (추정가)`;
      }

      function normalizePlace(p) {
        return {
          ...p,
          lat: Number(p.lat),
          lon: Number(p.lon),
          price_text: p.price_text || estimatePrice(p.name || "")
        };
      }

      function storeKey(p) {
        return `${String(p.name || "").trim().toLowerCase()}|${p.lat.toFixed(5)}|${p.lon.toFixed(5)}`;
      }

      function toLatLng(lat, lon) {
        return new kakao.maps.LatLng(Number(lat), Number(lon));
      }

      function metersBetween(lat1, lon1, lat2, lon2) {
        const toRad = (v) => (v * Math.PI) / 180;
        const R = 6371000;
        const dLat = toRad(lat2 - lat1);
        const dLon = toRad(lon2 - lon1);
        const a =
          Math.sin(dLat / 2) ** 2 +
          Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
        return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      }

      function formatDistance(meters) {
        if (meters >= 1000) return `${(meters / 1000).toFixed(1)}km`;
        return `${Math.round(meters)}m`;
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
            <div class="detail-label">${escHtml(APP_TITLE)} 안내</div>
            <div class="detail-title">버터떡 판매점을 찾아보세요</div>
            <div class="detail-v">
              마커를 클릭하면 판매 정보와 이미지가 표시됩니다.
              <br/>상단에서 위치 검색, 현재위치 이동, 근처 지점 보기 기능을 사용할 수 있습니다.
            </div>
          </div>
        `;
        animateCard();
      }

      function renderDetailCard(place, distanceText = "") {
        const imageHtml = place.image_url
          ? `
            <div class="detail-media">
              <img src="${escHtml(place.image_url)}" alt="${escHtml(place.name)} 이미지"
                onerror="this.parentElement.remove();" />
            </div>
          `
          : "";

        const distanceHtml = distanceText
          ? `<span class="distance-chip">📍 ${escHtml(distanceText)}</span>`
          : "";

        detailEl.className = "show";
        detailEl.innerHTML = `
          <div class="detail-shell">
            <div class="detail-head">
              <div>
                <div class="detail-label">${escHtml(APP_TITLE)} 판매점</div>
                <div class="detail-title">${escHtml(place.name || "이름 없음")}</div>
              </div>
              <button class="close-btn" type="button" aria-label="닫기">×</button>
            </div>

            ${imageHtml}

            <div class="meta-row">
              <span class="price-chip">🧈 ${escHtml(place.price_text || "가격 정보 없음")}</span>
              ${distanceHtml}
            </div>

            <div class="detail-grid">
              <div class="detail-k">주소</div><div class="detail-v">${escHtml(place.address || "정보 없음")}</div>
              <div class="detail-k">전화</div><div class="detail-v">${escHtml(place.phone || "정보 없음")}</div>
              <div class="detail-k">분류</div><div class="detail-v">${escHtml(place.category || "정보 없음")}</div>
            </div>

            ${place.url ? `<a class="detail-action" href="${escHtml(place.url)}" target="_blank" rel="noreferrer">상세 보기</a>` : ""}
          </div>
        `;
        animateCard();
        const closeBtn = detailEl.querySelector(".close-btn");
        if (closeBtn) {
          closeBtn.addEventListener("click", () => {
            resetFocus();
            renderIdleCard();
          }, { once: true });
        }
      }

      function getCurrentPosition() {
        return new Promise((resolve, reject) => {
          if (!navigator.geolocation) {
            reject(new Error("브라우저에서 위치 기능을 지원하지 않습니다."));
            return;
          }
          navigator.geolocation.getCurrentPosition(
            (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
            (err) => reject(new Error(err && err.message ? err.message : "위치 권한을 확인해주세요.")),
            { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
          );
        });
      }

      function initMap() {
        const places = rawPlaces.map(normalizePlace).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
        const map = new kakao.maps.Map(document.getElementById("map"), {
          center: toLatLng(defaultCenter.lat, defaultCenter.lon),
          level: 12
        });
        const geocoder = new kakao.maps.services.Geocoder();
        const placeService = new kakao.maps.services.Places();

        const markerImage = new kakao.maps.MarkerImage(
          breadIcon,
          new kakao.maps.Size(56, 56),
          { offset: new kakao.maps.Point(28, 50) }
        );
        const markerImageActive = new kakao.maps.MarkerImage(
          breadIcon,
          new kakao.maps.Size(68, 68),
          { offset: new kakao.maps.Point(34, 60) }
        );
        const currentMarkerImage = new kakao.maps.MarkerImage(
          currentIcon,
          new kakao.maps.Size(24, 24),
          { offset: new kakao.maps.Point(12, 12) }
        );

        const markerByKey = new Map();
        const markerRecords = [];
        let activeRecord = null;
        let currentMarker = null;
        let currentPos = null;
        let searchMarker = null;

        function resetFocus() {
          if (activeRecord) {
            activeRecord.marker.setImage(markerImage);
            activeRecord = null;
          }
        }

        function focusRecord(record, move = true, distanceText = "") {
          if (!record) return;
          if (activeRecord && activeRecord !== record) {
            activeRecord.marker.setImage(markerImage);
          }
          record.marker.setImage(markerImageActive);
          activeRecord = record;
          if (move) {
            map.panTo(toLatLng(record.place.lat, record.place.lon));
          }
          renderDetailCard(record.place, distanceText);
        }

        function showAllRecords() {
          markerRecords.forEach((r) => r.marker.setMap(map));
        }

        function showOnlyRecords(targetKeys) {
          markerRecords.forEach((r) => r.marker.setMap(targetKeys.has(r.key) ? map : null));
          if (activeRecord && !targetKeys.has(activeRecord.key)) {
            resetFocus();
          }
        }

        function fitBoundsForRecords(records, includeCurrent = false) {
          if (!records.length && !includeCurrent) return;
          const bounds = new kakao.maps.LatLngBounds();
          records.forEach((r) => bounds.extend(toLatLng(r.place.lat, r.place.lon)));
          if (includeCurrent && currentPos) {
            bounds.extend(toLatLng(currentPos.lat, currentPos.lon));
          }
          map.setBounds(bounds);
        }

        function renderAllStoreMarkers() {
          const allBounds = new kakao.maps.LatLngBounds();
          markerRecords.length = 0;
          markerByKey.clear();

          places.forEach((place) => {
            const marker = new kakao.maps.Marker({
              position: toLatLng(place.lat, place.lon),
              image: markerImage,
              title: place.name
            });
            marker.setMap(map);
            allBounds.extend(toLatLng(place.lat, place.lon));
            const record = { key: storeKey(place), place, marker };
            markerRecords.push(record);
            markerByKey.set(record.key, record);

            kakao.maps.event.addListener(marker, "click", () => {
              focusRecord(record, true);
            });
          });

          if (markerRecords.length > 0) {
            map.setBounds(allBounds);
          }
        }

        function updateCurrentMarker(lat, lon) {
          const pos = toLatLng(lat, lon);
          if (!currentMarker) {
            currentMarker = new kakao.maps.Marker({
              position: pos,
              image: currentMarkerImage,
              title: "현재 위치"
            });
            currentMarker.setMap(map);
          } else {
            currentMarker.setPosition(pos);
          }
        }

        async function ensureCurrentPosition(moveMap = true) {
          if (!currentPos) {
            currentPos = await getCurrentPosition();
          }
          updateCurrentMarker(currentPos.lat, currentPos.lon);
          if (moveMap) {
            map.setLevel(3);
            map.panTo(toLatLng(currentPos.lat, currentPos.lon));
          }
          return currentPos;
        }

        function setSearchMarker(lat, lon, title) {
          const pos = toLatLng(lat, lon);
          if (!searchMarker) {
            searchMarker = new kakao.maps.Marker({
              position: pos,
              image: markerImage,
              title
            });
            searchMarker.setMap(map);
          } else {
            searchMarker.setPosition(pos);
            searchMarker.setTitle(title);
          }
        }

        function searchLocation() {
          const keyword = inputEl.value.trim();
          if (!keyword) {
            setStatus("검색어를 입력해주세요.", true);
            return;
          }

          setStatus(`"${keyword}" 위치 검색 중...`);
          geocoder.addressSearch(keyword, (result, status) => {
            if (status === kakao.maps.services.Status.OK && result && result.length > 0) {
              const lat = Number(result[0].y);
              const lon = Number(result[0].x);
              map.setLevel(4);
              map.panTo(toLatLng(lat, lon));
              setSearchMarker(lat, lon, keyword);
              setStatus(`"${keyword}" 위치로 이동했습니다.`);
              return;
            }

            placeService.keywordSearch(keyword, (data, placeStatus) => {
              if (placeStatus === kakao.maps.services.Status.OK && data && data.length > 0) {
                const top = data[0];
                const lat = Number(top.y);
                const lon = Number(top.x);
                map.setLevel(4);
                map.panTo(toLatLng(lat, lon));
                setSearchMarker(lat, lon, top.place_name || keyword);
                setStatus(`"${keyword}" 검색 결과로 이동했습니다.`);
                return;
              }
              setStatus(`"${keyword}" 검색 결과가 없습니다.`, true);
            });
          });
        }

        async function moveCurrentLocation() {
          try {
            setStatus("현재 위치를 확인 중...");
            await ensureCurrentPosition(true);
            setStatus("현재 위치로 이동했습니다.");
          } catch (err) {
            setStatus(`현재 위치 이동 실패: ${err && err.message ? err.message : "권한을 확인해주세요."}`, true);
          }
        }

        async function showNearbyStoresFromCurrent() {
          try {
            setStatus("현재 위치 기준 근처 지점을 찾는 중...");
            const pos = await ensureCurrentPosition(false);
            const ranked = markerRecords
              .map((record) => ({
                record,
                distance_m: metersBetween(pos.lat, pos.lon, record.place.lat, record.place.lon)
              }))
              .sort((a, b) => a.distance_m - b.distance_m);

            if (!ranked.length) {
              setStatus("판매점 데이터가 없습니다.", true);
              return;
            }

            let picked = ranked.filter((r) => r.distance_m <= STORE_NEARBY_RADIUS_M).slice(0, STORE_NEARBY_LIMIT);
            let statusText = "";
            if (picked.length === 0) {
              picked = ranked.slice(0, STORE_NEARBY_LIMIT);
              statusText = "근처 지점이 적어 가장 가까운 지점을 표시합니다.";
            } else {
              statusText = `현재위치 기준 ${picked.length}개 지점을 찾았습니다.`;
            }

            const pickedKeys = new Set(picked.map((p) => p.record.key));
            showOnlyRecords(pickedKeys);
            fitBoundsForRecords(picked.map((p) => p.record), true);
            const first = picked[0];
            if (first) {
              focusRecord(first.record, false, formatDistance(first.distance_m));
            }
            setStatus(statusText);
          } catch (err) {
            setStatus(`근처 지점 보기 실패: ${err && err.message ? err.message : "위치 권한을 확인해주세요."}`, true);
          }
        }

        function showAllStores() {
          showAllRecords();
          fitBoundsForRecords(markerRecords, false);
          setStatus(`전체 지점 ${markerRecords.length}곳을 표시합니다.`);
          renderIdleCard();
          resetFocus();
        }

        renderAllStoreMarkers();
        renderIdleCard();
        setStatus(`버터떡 판매점 ${places.length}곳 로딩 완료`);

        searchBtn.addEventListener("click", searchLocation);
        inputEl.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            searchLocation();
          }
        });
        currentBtn.addEventListener("click", moveCurrentLocation);
        nearbyBtn.addEventListener("click", showNearbyStoresFromCurrent);
        allBtn.addEventListener("click", showAllStores);

        kakao.maps.event.addListener(map, "click", () => {
          resetFocus();
        });
      }

      kakao.maps.load(initMap);
    </script>
  </body>
</html>
"""

    return (
        template.replace("__APP_TITLE__", APP_TITLE)
        .replace("__PLACE_COUNT__", str(len(places)))
        .replace("__JS_KEY__", js_key)
        .replace("__PLACES__", payload)
        .replace("__BREAD_ICON__", bread_icon_json)
        .replace("__CURRENT_ICON__", current_icon_json)
        .replace("__CENTER_LAT__", f"{center_lat:.8f}")
        .replace("__CENTER_LON__", f"{center_lon:.8f}")
    )


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🍞",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(
        """
<style>
[data-testid="stSidebar"] { display: none; }
header[data-testid="stHeader"] { display: none; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stAppViewContainer"] {
  max-height: 100vh;
  overflow: hidden;
}
[data-testid="stAppViewContainer"] > .main {
  height: 100vh;
}
[data-testid="stMainBlockContainer"] {
  padding: 0 !important;
  margin: 0 !important;
  max-width: 100vw !important;
  height: 100vh;
}
div[data-testid="stVerticalBlock"] {
  gap: 0 !important;
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
        st.error(
            "KAKAO_JAVASCRIPT_KEY가 설정되어 있지 않습니다. `.env`에 추가해주세요."
        )
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
    html = _render_kakao_map_html(center_lat, center_lon, places, js_key)

    # components.html()로 직접 렌더링 (로컬 파일 서버 불필요)
    components.html(html, height=1000, scrolling=False)


if __name__ == "__main__":
    main()
