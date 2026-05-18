# 건축물대장 주택 500m 이격 지역 Shapefile 생성

건축물대장 총괄표제부 데이터에서 **주택 용도 건물**을 추출하고,  
해당 건물로부터 **500m 이상 떨어진 지역**을 Shapefile로 생성합니다.

## 데이터 다운로드

1. [hub.go.kr 건축물대장 총괄표제부](https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do) 접속
2. 회원가입 및 로그인
3. 원하는 지역·기간 선택 후 파일 다운로드
4. 다운로드된 CSV/ZIP 파일을 `data/` 폴더에 저장

```
data/
  mart_djy_08.csv       ← 건축물대장 총괄표제부 파일
  mart_djy_08_2.csv     ← (여러 파일 가능)
```

## 설치

```bash
pip install geopandas pandas requests shapely pyproj openpyxl tqdm
```

## 실행

### 기본 실행 (데이터 파일이 `data/` 폴더에 있는 경우)

```bash
python building_distance_shp.py
```

### 주요 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--data-dir` | `./data` | 건축물대장 CSV 파일 폴더 |
| `--buffer` | `500` | 버퍼 거리 (미터) |
| `--out-dir` | `./output` | 결과 SHP 저장 폴더 |
| `--study-area` | (없음) | 연구 범위 SHP (없으면 자동 생성) |

### 예시

```bash
# 서울 행정경계 기준으로 서울 내 주택 500m 이격 지역 생성
python building_distance_shp.py \
  --data-dir ./data \
  --study-area ./seoul_boundary.shp \
  --buffer 500 \
  --out-dir ./output
```

### 주소 지오코딩 (파일에 좌표가 없는 경우)

건축물대장 파일에 위도/경도 컬럼이 없으면 카카오 지오코딩 API를 사용합니다.

```bash
export KAKAO_API_KEY=your_kakao_rest_api_key
python building_distance_shp.py --data-dir ./data
```

카카오 REST API 키 발급: https://developers.kakao.com

## 출력 파일

`output/` 폴더에 3개의 Shapefile이 생성됩니다.

| 파일 | 설명 |
|------|------|
| `housing_far_500m.shp` | **주택으로부터 500m 이상 이격된 지역** (최종 결과) |
| `housing_buffer_500m.shp` | 주택 500m 버퍼 영역 (주택 인근 지역) |
| `housing_points.shp` | 주택 위치 포인트 |

## 좌표계

- 버퍼 계산: `EPSG:5186` (한국 중부원점 TM, 미터 단위)
- 입력/출력: `EPSG:4326` (WGS84 위경도)

## 필터링 기준

아래 주용도코드에 해당하는 건물을 주택으로 분류합니다.

| 코드 | 용도 |
|------|------|
| 01000 | 단독주택 |
| 01001~01004 | 단독주택 세분류 |
| 02000 | 공동주택 |
| 02001~02004 | 공동주택 세분류 (아파트, 연립, 다세대, 기숙사) |

주용도코드 컬럼이 없는 경우 주용도코드명에서 키워드(`주택`, `아파트`, `연립` 등)로 필터링합니다.
