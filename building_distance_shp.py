"""
건축물대장 총괄표제부 데이터를 활용하여
주택으로부터 500m 이상 떨어진 지역의 Shapefile 생성

데이터 출처: https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do
"""

import os
import re
import sys
import glob
import zipfile
import argparse
import requests
import pandas as pd
import geopandas as gpd
from pathlib import Path
from tqdm import tqdm
from shapely.geometry import Point
from shapely.ops import unary_union


# ── 한국 표준 좌표계 ──────────────────────────────────────────────
CRS_INPUT  = "EPSG:4326"   # WGS84 (위경도)
CRS_METRIC = "EPSG:5186"   # 한국 중부원점 TM (미터 단위 거리 계산용)
CRS_OUTPUT = "EPSG:4326"   # 결과물 저장 좌표계

BUFFER_DISTANCE_M = 500     # 버퍼 거리 (미터)

# 주택 용도 코드 목록 (주용도코드명 기준)
# 건축물대장 주용도코드: 01000=단독주택, 02000=공동주택 등
HOUSING_USE_KEYWORDS = [
    "단독주택", "공동주택", "다세대주택", "연립주택",
    "아파트", "기숙사", "다가구주택", "주택",
]
HOUSING_USE_CODES = {
    "01000",  # 단독주택
    "01001",  # 단독주택(단독주택)
    "01002",  # 단독주택(다중주택)
    "01003",  # 단독주택(다가구주택)
    "01004",  # 단독주택(공관)
    "02000",  # 공동주택
    "02001",  # 공동주택(아파트)
    "02002",  # 공동주택(연립주택)
    "02003",  # 공동주택(다세대주택)
    "02004",  # 공동주택(기숙사)
}


# ── 1. 데이터 다운로드 ─────────────────────────────────────────────

def download_hub_data(save_dir: Path) -> list[Path]:
    """
    hub.go.kr에서 건축물대장 총괄표제부 데이터를 다운로드합니다.

    hub.go.kr는 회원가입 후 파일 다운로드가 필요합니다.
    이 함수는 이미 다운로드된 파일을 save_dir에서 찾거나,
    --data-dir 옵션으로 지정된 경로의 파일을 사용합니다.

    수동 다운로드 방법:
        1. https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do 접속
        2. 로그인 후 원하는 지역/기간 선택
        3. 파일 다운로드 (ZIP 또는 CSV 형식)
        4. 다운로드된 파일을 --data-dir 경로에 저장
    """
    save_dir.mkdir(parents=True, exist_ok=True)

    # ZIP 파일 자동 압축 해제
    for zip_path in save_dir.glob("*.zip"):
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(save_dir)
        print(f"압축 해제: {zip_path.name}")

    csv_files = list(save_dir.glob("**/*.csv")) + list(save_dir.glob("**/*.CSV"))
    txt_files = list(save_dir.glob("**/*.txt")) + list(save_dir.glob("**/*.TXT"))
    found = csv_files + txt_files

    if not found:
        print("\n[오류] 데이터 파일을 찾을 수 없습니다.")
        print("아래 절차에 따라 데이터를 다운로드해 주세요:\n")
        print("  1. https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do 접속")
        print("  2. 회원가입 / 로그인")
        print("  3. 조회 조건 설정 후 [다운로드] 클릭")
        print(f"  4. 다운로드된 파일을 '{save_dir}' 폴더에 저장")
        print("  5. 스크립트 재실행\n")
        sys.exit(1)

    return found


# ── 2. 데이터 로드 & 주택 필터링 ──────────────────────────────────

def load_and_filter(file_paths: list[Path]) -> pd.DataFrame:
    """CSV/TXT 파일을 읽어 주택 용도만 필터링합니다."""
    dfs = []
    for fp in tqdm(file_paths, desc="파일 읽기"):
        try:
            df = pd.read_csv(
                fp,
                encoding="cp949",
                low_memory=False,
                dtype=str,
            )
        except UnicodeDecodeError:
            df = pd.read_csv(fp, encoding="utf-8", low_memory=False, dtype=str)

        dfs.append(df)

    data = pd.concat(dfs, ignore_index=True)
    print(f"전체 레코드: {len(data):,}건")

    # 컬럼명 정규화
    data.columns = data.columns.str.strip()

    # 주용도 필터링 (코드 또는 명칭 기준)
    use_code_col  = _find_col(data, ["주용도코드", "사용용도코드", "용도코드"])
    use_name_col  = _find_col(data, ["주용도코드명", "사용용도", "주용도명", "용도명"])

    mask = pd.Series(False, index=data.index)

    if use_code_col:
        mask |= data[use_code_col].str.strip().isin(HOUSING_USE_CODES)

    if use_name_col:
        pattern = "|".join(HOUSING_USE_KEYWORDS)
        mask |= data[use_name_col].str.contains(pattern, na=False)

    housing = data[mask].copy()
    print(f"주택 레코드: {len(housing):,}건")

    if housing.empty:
        print("\n[경고] 주택 데이터가 없습니다. 컬럼 목록:")
        print(data.columns.tolist())
        sys.exit(1)

    return housing


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    # 부분 일치
    for c in df.columns:
        for cand in candidates:
            if cand in c:
                return c
    return None


# ── 3. 좌표 확보 ──────────────────────────────────────────────────

def ensure_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    """
    건축물대장 데이터에서 위경도 좌표를 확보합니다.

    우선순위:
      1) 파일에 위도/경도 컬럼이 이미 있는 경우
      2) 도로명 주소 또는 지번 주소로 카카오 지오코딩 (API 키 필요)
    """
    lat_col = _find_col(df, ["위도", "lat", "latitude", "y좌표", "y_coord"])
    lon_col = _find_col(df, ["경도", "lon", "longitude", "x좌표", "x_coord"])

    if lat_col and lon_col:
        df = df.dropna(subset=[lat_col, lon_col])
        df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
        df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
        df = df.dropna(subset=[lat_col, lon_col])
        df = df[(df[lat_col].between(33, 39)) & (df[lon_col].between(124, 132))]
        print(f"좌표 보유 레코드: {len(df):,}건")
        df = df.rename(columns={lat_col: "_lat", lon_col: "_lon"})
        return df

    # 좌표 없는 경우 → 주소 지오코딩 필요
    print("\n[안내] 파일에 좌표 컬럼이 없습니다. 주소 지오코딩을 수행합니다.")
    kakao_key = os.environ.get("KAKAO_API_KEY", "")
    if not kakao_key:
        print("  KAKAO_API_KEY 환경변수를 설정하거나 --coord-file 옵션을 사용하세요.")
        print("  카카오 REST API 키: https://developers.kakao.com")
        sys.exit(1)

    addr_col = _find_col(df, ["도로명주소", "도로명전체주소", "주소", "소재지도로명주소", "대지위치"])
    if not addr_col:
        addr_col = _find_col(df, ["지번주소", "소재지지번주소", "대지위치"])
    if not addr_col:
        print("[오류] 주소 컬럼을 찾을 수 없습니다.")
        sys.exit(1)

    lats, lons = geocode_kakao_batch(df[addr_col].fillna("").tolist(), kakao_key)
    df["_lat"] = lats
    df["_lon"] = lons
    df = df.dropna(subset=["_lat", "_lon"])
    print(f"지오코딩 완료: {len(df):,}건")
    return df


def geocode_kakao_batch(addresses: list[str], api_key: str) -> tuple[list, list]:
    """카카오 지오코딩 API로 주소 → 좌표 변환."""
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {api_key}"}
    lats, lons = [], []

    for addr in tqdm(addresses, desc="지오코딩"):
        if not addr.strip():
            lats.append(None); lons.append(None)
            continue
        try:
            res = requests.get(url, headers=headers, params={"query": addr}, timeout=5)
            docs = res.json().get("documents", [])
            if docs:
                lats.append(float(docs[0]["y"]))
                lons.append(float(docs[0]["x"]))
            else:
                lats.append(None); lons.append(None)
        except Exception:
            lats.append(None); lons.append(None)

    return lats, lons


# ── 4. GeoDataFrame 생성 ──────────────────────────────────────────

def to_geodataframe(df: pd.DataFrame) -> gpd.GeoDataFrame:
    geometry = [Point(lon, lat) for lon, lat in zip(df["_lon"], df["_lat"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=CRS_INPUT)
    return gdf


# ── 5. 버퍼 & 역 영역 생성 ────────────────────────────────────────

def create_non_housing_zone(
    housing_gdf: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame | None,
    buffer_m: int = BUFFER_DISTANCE_M,
) -> gpd.GeoDataFrame:
    """
    주택 포인트에서 buffer_m 버퍼를 만들고,
    study_area(연구 범위)에서 버퍼 영역을 제거하여
    '주택으로부터 buffer_m 이상 떨어진 지역' 반환.
    """
    # 미터 단위 좌표계로 변환
    housing_proj = housing_gdf.to_crs(CRS_METRIC)

    print(f"{buffer_m}m 버퍼 생성 중...")
    housing_proj["geometry"] = housing_proj.geometry.buffer(buffer_m)
    buffer_union = unary_union(housing_proj.geometry)

    if study_area is None:
        # 연구 범위 미지정 → 데이터 전체 외곽 Convex Hull 사용
        hull = housing_proj.unary_union.convex_hull.buffer(buffer_m * 2)
        study_geom = hull
    else:
        study_proj = study_area.to_crs(CRS_METRIC)
        study_geom = unary_union(study_proj.geometry)

    # 주택 버퍼 제거 → 500m 이상 거리 영역
    far_from_housing = study_geom.difference(buffer_union)

    result = gpd.GeoDataFrame(
        {"description": [f"주택으로부터 {buffer_m}m 이상 이격 지역"]},
        geometry=[far_from_housing],
        crs=CRS_METRIC,
    ).to_crs(CRS_OUTPUT)

    # 버퍼 영역도 함께 저장용
    buffer_gdf = gpd.GeoDataFrame(
        {"description": [f"주택 {buffer_m}m 버퍼 (주택 인근 지역)"]},
        geometry=[buffer_union],
        crs=CRS_METRIC,
    ).to_crs(CRS_OUTPUT)

    return result, buffer_gdf


# ── 6. Shapefile 저장 ─────────────────────────────────────────────

def save_shapefile(gdf: gpd.GeoDataFrame, out_path: Path, layer_name: str):
    out_path.mkdir(parents=True, exist_ok=True)
    shp_path = out_path / f"{layer_name}.shp"
    gdf.to_file(shp_path, driver="ESRI Shapefile", encoding="utf-8")
    print(f"저장 완료: {shp_path}")


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="건축물대장 주택으로부터 500m 이상 이격 지역 SHP 생성"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"),
        help="건축물대장 CSV/TXT 파일이 있는 폴더 (기본: ./data)",
    )
    parser.add_argument(
        "--study-area", type=Path, default=None,
        help="연구 범위 SHP 파일 경로 (없으면 데이터 범위 자동 생성)",
    )
    parser.add_argument(
        "--buffer", type=int, default=500,
        help="버퍼 거리 (미터, 기본: 500)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("output"),
        help="결과물 저장 폴더 (기본: ./output)",
    )
    parser.add_argument(
        "--coord-file", type=Path, default=None,
        help="이미 좌표가 포함된 CSV 파일 (건축물대장 대신 사용)",
    )
    args = parser.parse_args()

    # 1. 데이터 파일 목록
    if args.coord_file:
        file_paths = [args.coord_file]
    else:
        file_paths = download_hub_data(args.data_dir)

    # 2. 로드 & 주택 필터링
    housing_df = load_and_filter(file_paths)

    # 3. 좌표 확보
    housing_df = ensure_coordinates(housing_df)

    # 4. GeoDataFrame
    housing_gdf = to_geodataframe(housing_df)
    print(f"주택 포인트: {len(housing_gdf):,}개")

    # 5. 연구 범위 (선택)
    study_area = None
    if args.study_area and args.study_area.exists():
        study_area = gpd.read_file(args.study_area)
        print(f"연구 범위 로드: {args.study_area}")

    # 6. 버퍼 & 역 영역
    far_zone, buffer_zone = create_non_housing_zone(
        housing_gdf, study_area, buffer_m=args.buffer
    )

    # 7. 저장
    save_shapefile(
        far_zone, args.out_dir,
        f"housing_far_{args.buffer}m",
    )
    save_shapefile(
        buffer_zone, args.out_dir,
        f"housing_buffer_{args.buffer}m",
    )
    save_shapefile(
        housing_gdf[["geometry"]], args.out_dir,
        "housing_points",
    )

    print("\n완료! 생성된 파일:")
    for f in args.out_dir.glob("*.shp"):
        print(f"  {f}")


if __name__ == "__main__":
    main()
