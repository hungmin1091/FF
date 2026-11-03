"""농부의 판매비서 | 실행: streamlit run app.py

설정 순서
1) GitHub 저장소에 app.py, requirements.txt 두 파일을 올립니다(비밀번호 업로드 금지).
2) Streamlit Community Cloud에서 저장소 연결, Main file path=app.py로 배포합니다.
3) 앱 Settings > Secrets에 아래 TOML 값을 입력합니다.
   ADMIN_PASSWORD = "충분히-긴-관리자-비밀번호"
   APP_URL = "https://실제주소.streamlit.app"
   SUPABASE_URL = "https://프로젝트.supabase.co"
   SUPABASE_SERVICE_ROLE_KEY = "서버용-service_role-키"
   비밀 키는 GitHub/고객에게 공유하지 마세요. Secrets 변경 후 앱을 재시작하세요.
4) Supabase 프로젝트 SQL Editor에서 다음 SQL을 실행합니다.
   create table public.farm_products (
     id uuid primary key,
     public_data jsonb not null default '{}'::jsonb,
     private_data jsonb not null default '{}'::jsonb,
     updated_at timestamptz not null default now()
   );
   alter table public.farm_products enable row level security;
   revoke all on table public.farm_products from anon, authenticated;
   -- 서버의 service_role만 테이블에 접근. 공개 정책을 추가하지 마세요.
   -- 이 앱은 농가 하나를 위한 구성입니다. 여러 농가 공용 운영에는 권한 설계가 필요합니다.
5) APP_URL은 쿼리 문자열 없는 실제 배포 주소로 설정합니다.
   관리자 화면에서 상품 저장 → QR 다운로드 → 인쇄해 상자에 붙입니다.
   고객 주소: APP_URL?product=상품UUID. 관리자 비밀/원가는 공개하지 않습니다.
6) 상품 및 사진은 Supabase DB에 저장합니다. 사진은 1MB 이하 JPEG로 압축해
   public_data에 포함합니다. DB 용량/백업은 Supabase에서 관리하세요.
   저장소 미설정 시 세션 내 체험 모드(새 세션/재시작 시 소실).
   체험 상품의 QR은 다른 기기에서 사용할 수 없습니다.
7) 수수료·세금·광고비·반품·인건비 등은 실제 조건을 확인하세요.
   이 앱의 계산은 입력 비용 기준 예상치이며 정산/세무 도구가 아닙니다.
"""
import base64
import hashlib
import hmac
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from urllib.parse import urlsplit, urlencode

import qrcode
import requests
import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="농부의 판매비서", page_icon="🌱", layout="centered")


def secret(name):
    try:
        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def valid_url(value):
    try:
        u = urlsplit(value)
        return u.scheme in ("http", "https") and bool(u.hostname) and not u.username and not u.password
    except ValueError:
        return False


def money(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def profit(price, shipping_received, cost, packing, shipping_paid, discount, rate, fee_shipping):
    # 할인은 상품 판매대금에서 차감, 수수료는 할인 후 상품대금 기준.
    goods = max(0, price - discount)
    basis = goods + (shipping_received if fee_shipping else 0)
    fee = money(Decimal(str(basis)) * Decimal(str(rate)) / 100)
    return goods + shipping_received - cost - packing - shipping_paid - fee, fee


DB_URL = secret("SUPABASE_URL").rstrip("/")
DB_KEY = secret("SUPABASE_SERVICE_ROLE_KEY")
PERSISTENT = bool(DB_URL and DB_KEY)


def database(method, params=None, payload=None):
    if not valid_url(DB_URL) or not DB_URL.startswith("https://"):
        raise RuntimeError("외부 저장소 주소는 올바른 HTTPS 주소여야 합니다.")
    try:
        response = requests.request(method, DB_URL + "/rest/v1/farm_products",
            headers={"apikey": DB_KEY, "Authorization": "Bearer " + DB_KEY,
                     "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
            params=params, json=payload, timeout=20)
        response.raise_for_status()
        return response.json() if method == "GET" else None
    except requests.RequestException:
        raise RuntimeError("저장소 연결에 실패했습니다. 관리자에게 연결 설정과 테이블 권한 확인을 요청하세요.") from None


def read_products(product_id=None):
    if PERSISTENT:
        params = {"select": "id,public_data" if product_id else "id,public_data,private_data", "order": "updated_at.desc"}
        if product_id:
            params["id"] = "eq." + product_id
        return database("GET", params=params)
    records = list(st.session_state.get("demo_products", {}).values())
    return [r for r in records if r["id"] == product_id] if product_id else records


def save_product(record):
    if PERSISTENT:
        from datetime import datetime, timezone
        database("POST", params={"on_conflict": "id"}, payload={**record, "updated_at": datetime.now(timezone.utc).isoformat()})
    else:
        st.session_state.setdefault("demo_products", {})[record["id"]] = record


def photo_data(upload):
    if upload.size > 8 * 1024 * 1024:
        raise ValueError("원본 사진은 8MB 이하로 올려 주세요.")
    with Image.open(upload) as source:
        if source.width * source.height > 25000000:
            raise ValueError("사진 해상도가 너무 큽니다. 크기를 줄여 주세요.")
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1100, 1100))
        out = BytesIO()
        image.save(out, "JPEG", quality=78, optimize=True)
    if len(out.getvalue()) > 1024 * 1024:
        raise ValueError("압축 사진은 1MB 이하여야 합니다. 더 작은 사진을 선택하세요.")
    return base64.b64encode(out.getvalue()).decode("ascii")


def show_photo(data):
    if data:
        try:
            st.image(base64.b64decode(data), use_container_width=True)
        except Exception:
            st.caption("사진을 표시하지 못했습니다.")


def make_copy(p):
    title = f"{p['name']} {p['weight']} · {p['unit']}"
    lines = [p['name'] + (" (" + p['variety'] + ")" if p['variety'] else ""),
             "구성: " + p['weight'] + " / " + p['unit'],
             "상품 특징: " + (p['features'] or "판매자에게 문의해 주세요."),
             "출고 안내: " + p['dispatch']]
    return title, "\n".join(lines)


# Streamlit 테마 색상을 그대로 사용하여 다크/라이트 대비 문제를 방지합니다.
st.markdown("""<style>
.block-container{max-width:760px;padding-top:1.5rem;padding-bottom:2rem}
.stButton button,.stLinkButton a,.stDownloadButton button{min-height:48px;font-size:1rem}
[data-testid="stMetricValue"]{font-size:1.65rem}
[data-testid="stExpander"] summary{min-height:48px}
footer{display:none}
</style>""", unsafe_allow_html=True)

# 고객 경로는 관리자 인증/조회보다 먼저 분리합니다.
public_id = st.query_params.get("product", "")
if public_id:
    try:
        public_id = str(uuid.UUID(public_id))
    except ValueError:
        st.error("잘못된 상품 주소입니다."); st.stop()
    st.title("🌱 밭에서 식탁까지")
    if not PERSISTENT:
        st.warning("체험용 안내입니다. 다른 기기와 공유되지 않으며 저장이 유지되지 않습니다.")
    try:
        matches = read_products(public_id)
    except RuntimeError as exc:
        st.error(str(exc)); st.stop()
    if not matches:
        st.info("상품을 찾을 수 없습니다. 판매자에게 문의해 주세요."); st.stop()
    p = matches[0]["public_data"]
    st.header(p.get("name", "농산물"))
    show_photo(p.get("photo", ""))
    st.write("구성: " + p.get("weight", "") + " · " + p.get("unit", ""))
    if p.get("variety"): st.write("품종: " + p["variety"])
    st.subheader("농산물 소개"); st.text(p.get("features") or "판매자에게 문의해 주세요.")
    st.subheader("📦 출고 안내"); st.text(p.get("dispatch", ""))
    st.subheader("❄️ 보관 방법"); st.text(p.get("storage") or "상품 판매 페이지의 안내를 확인해 주세요.")
    st.subheader("🍽️ 먹는 방법"); st.text(p.get("recipe") or "상품 판매 페이지의 안내를 확인해 주세요.")
    st.subheader("💬 문의 방법"); st.text(p.get("contact") or "구매하신 판매처의 문의 기능을 이용해 주세요.")
    if valid_url(p.get("shop", "")):
        st.link_button("🛒 다시 구매하기 · 기존 판매처로 이동", p["shop"], use_container_width=True)
    st.caption("주문·결제·배송 관련 처리는 구매하신 판매처에서 진행합니다. 이 화면에서는 개인정보를 수집하지 않습니다.")
    st.stop()

st.title("🌱 농부의 판매비서")
st.caption("상품 준비 → 가격 확인 → 소개글 복사 → 택배 QR")
password = secret("ADMIN_PASSWORD")
if not password:
    st.error("관리자 보호를 위해 먼저 Streamlit Secrets에 ADMIN_PASSWORD를 설정해 주세요. 설정 방법은 app.py 상단에 있습니다.")
    st.stop()
fingerprint = hashlib.sha256(password.encode()).hexdigest()
if st.session_state.get("auth") != fingerprint:
    with st.form("login"):
        entered = st.text_input("관리자 비밀번호", type="password")
        login = st.form_submit_button("관리 화면 열기", use_container_width=True)
    if login:
        if time.time() < st.session_state.get("locked_until", 0):
            st.error("잠시 후 다시 시도해 주세요.")
        elif hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.auth = fingerprint
            st.session_state.attempts = 0
            st.rerun()
        else:
            st.session_state.attempts = st.session_state.get("attempts", 0) + 1
            if st.session_state.attempts >= 5:
                st.session_state.locked_until = time.time() + 60
                st.session_state.attempts = 0
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()
if st.button("로그아웃"):
    st.session_state.pop("auth", None); st.rerun()
if PERSISTENT:
    st.caption("☁️ 외부 저장 모드 · 저장 버튼을 눌러야 변경 사항이 반영됩니다.")
else:
    st.warning("임시 체험 모드: 상품은 현재 세션에만 저장됩니다. 영구 저장과 공유 QR은 Supabase 연결 후 사용하세요.")
try:
    records = read_products()
except RuntimeError as exc:
    st.error(str(exc)); st.stop()
by_id = {r["id"]: r for r in records}
selected = st.selectbox("작업할 상품", ["new"] + list(by_id), format_func=lambda k: "➕ 새 상품" if k == "new" else by_id[k]["public_data"].get("name", "상품"))
r = by_id.get(selected, {})
p = r.get("public_data", {})
v = r.get("private_data", {})
# 상품 변경 시 위젯 상태를 상품별로 독립시킵니다.
prefix = selected + "_"

def text(label, key, default="", area=False):
    widget = st.text_area if area else st.text_input
    return widget(label, value=p.get(key, default), key=prefix + key, max_chars=3000 if area else 300)


def number(label, key, default=0):
    return st.number_input(label, min_value=0, max_value=100000000, value=int(v.get(key, default)), step=100, key=prefix + key)

with st.expander("① 상품 정보 · 사진 · 고객 안내", expanded=True):
    categories = ["채소", "과일", "곡물", "버섯", "기타"]
    category = st.selectbox("카테고리", categories, index=categories.index(p.get("category", "기타")), key=prefix + "category")
    name = text("상품명 *", "name")
    variety = text("품종", "variety")
    c1, c2 = st.columns(2)
    with c1: weight = text("중량 * (예: 3kg)", "weight")
    with c2: unit = text("판매 단위 * (예: 1상자)", "unit", "1상자")
    quantity = st.number_input("판매 가능 수량 (위 판매 단위 기준)", min_value=0, max_value=1000000, value=int(v.get("quantity", 10)), key=prefix + "quantity")
    dispatch = text("출고 가능일·일정 *", "dispatch")
    features = text("실제 상품 특징", "features", area=True)
    storage = text("보관 방법", "storage", area=True)
    recipe = text("먹는 방법·요리 아이디어", "recipe", area=True)
    contact = text("고객 문의 방법 (판매처 문의 경로 권장)", "contact")
    shop = text("기존 판매처의 상품 주소 (https://...)", "shop")
    upload = st.file_uploader("사진 등록·교체 (JPG/PNG/WEBP, 8MB 이하)", type=["jpg", "jpeg", "png", "webp"], key=prefix + "upload")
    remove_photo = st.checkbox("기존 사진 제거", key=prefix + "remove_photo")
    if p.get("photo"): show_photo(p["photo"])

with st.expander("② 가격 설정 · 손해 방지 계산", expanded=True):
    st.caption("아래 비용은 판매 단위 1개 기준입니다. 예: 1상자. 할인액은 상품 가격에서 차감합니다.")
    c1, c2 = st.columns(2)
    with c1:
        price = number("상품 판매 가격 (원)", "price", 20000)
        cost = number("농산물 원가·추정 원가 (원)", "cost")
        packing = number("포장비 (원)", "packing")
        discount = number("판매자 부담 할인액 (원)", "discount")
    with c2:
        shipping_paid = number("실제 배송비 (원)", "shipping_paid", 3500)
        shipping_received = number("고객에게 받는 배송비 (원)", "shipping_received")
        target = number("목표 이익 (원/판매 단위)", "target", 5000)
        rate = st.number_input("판매 수수료율 (%)", min_value=0.0, max_value=99.0, value=float(v.get("rate", 5)), step=0.1, key=prefix + "rate")
    fee_shipping = st.checkbox("할인 후 상품대금 + 고객 배송비에 수수료 적용", value=bool(v.get("fee_shipping", False)), key=prefix + "fee_shipping")
    st.caption("체크 해제 시 할인 후 상품대금에만 수수료를 적용합니다. 플랫폼의 실제 정산 조건을 확인하세요.")
    result, fee = profit(price, shipping_received, cost, packing, shipping_paid, discount, rate, fee_shipping)
    c1, c2 = st.columns(2)
    c1.metric("1개 판매 예상 이익", f"{result:,}원")
    c2.metric(f"{quantity:,}개 판매 예상 이익", f"{result * quantity:,}원")
    if discount > price:
        st.error("할인액이 상품 가격보다 큽니다. 저장 전 수정해 주세요.")
    elif result < 0: st.error("입력한 조건에서는 손실이 예상됩니다. 가격과 비용을 확인해 주세요.")
    elif result < target: st.warning(f"목표 이익까지 {target-result:,}원 부족합니다.")
    else: st.success("현재 조건은 목표 이익을 충족합니다.")
    free, _ = profit(price, 0, cost, packing, shipping_paid, discount, rate, fee_shipping)
    st.write(f"상품가·비용을 그대로 유지할 때: 무료배송 {free:,}원 / 현재 배송비 조건 {result:,}원")
    st.caption(f"입력 비용 기준 예상 이익 · 반영 수수료 {fee:,}원. 입력하지 않은 세금·인건비·광고비·반품 손실 등은 제외됩니다.")

public = dict(category=category, name=name.strip(), variety=variety.strip(), weight=weight.strip(), unit=unit.strip(), dispatch=dispatch.strip(), features=features.strip(), storage=storage.strip(), recipe=recipe.strip(), contact=contact.strip(), shop=shop.strip(), photo=p.get("photo", ""))
private = dict(quantity=quantity, price=price, cost=cost, packing=packing, discount=discount, shipping_paid=shipping_paid, shipping_received=shipping_received, target=target, rate=rate, fee_shipping=fee_shipping)
if st.button("💾 상품 정보와 가격 저장", type="primary", use_container_width=True):
    if not all([public["name"], public["weight"], public["unit"], public["dispatch"]]):
        st.error("상품명·중량·판매 단위·출고 일정을 입력해 주세요.")
    elif shop.strip() and not valid_url(shop.strip()):
        st.error("판매처 주소는 올바른 http 또는 https 주소여야 합니다.")
    elif discount > price:
        st.error("할인액은 상품 가격을 초과할 수 없습니다.")
    else:
        try:
            if remove_photo: public["photo"] = ""
            if upload is not None: public["photo"] = photo_data(upload)
            product_id = str(uuid.uuid4()) if selected == "new" else selected
            save_product({"id": product_id, "public_data": public, "private_data": private})
            st.success("저장했습니다. 위 상품 선택에서 해당 상품을 선택하면 저장한 정보와 QR을 확인할 수 있습니다.")
            st.session_state["last_saved"] = product_id
        except (RuntimeError, ValueError, OSError, Image.DecompressionBombError) as exc:
            st.error(str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "사진을 처리하지 못했습니다. 다른 사진을 선택해 주세요.")

with st.expander("③ 소개글 복사 · 판매 준비 확인"):
    title, description = make_copy(public)
    st.caption("현재 입력값 기준 초안입니다. 코드 상자 오른쪽 위 복사 버튼으로 복사하세요. 판매처에 게시하기 전에 직접 검토하세요.")
    st.write("상품명 초안"); st.code(title, language=None)
    st.write("짧은 홍보글"); st.code(public["name"] + " · " + (public["features"] or "상품 특징을 입력해 주세요."), language=None)
    st.write("상세 소개글"); st.code(description, language=None)
    st.write("출고 안내문"); st.code(public["dispatch"] or "출고 일정을 입력해 주세요.", language=None)
    missing = [label for key, label in [("name", "상품명"), ("weight", "중량"), ("dispatch", "출고 일정"), ("storage", "보관 방법"), ("shop", "판매 링크"), ("photo", "상품 사진")] if not public.get(key)]
    if missing: st.info("판매 준비 점검: " + ", ".join(missing) + "을 확인해 주세요. 사진은 저장한 뒤 반영됩니다.")
    else: st.success("기본 판매 정보가 준비되었습니다. 사실관계와 판매처 등록 요건을 별도로 확인하세요.")

with st.expander("④ 고객용 QR · 안내 페이지"):
    configured = secret("APP_URL")
    base = st.text_input("실제 앱 배포 주소 (쿼리 문자열 제외)", value=configured, key="base_url")
    qr_id = selected if selected != "new" else st.session_state.get("last_saved")
    st.caption("QR에는 저장된 상품이 연결됩니다. 변경 사항은 먼저 저장하세요. 주소를 바꾸면 QR도 다시 내려받아야 합니다.")
    if not PERSISTENT:
        st.warning("체험 모드에서는 공유 QR을 발급하지 않습니다. 외부 저장소를 먼저 연결하세요.")
    elif not qr_id:
        st.info("상품을 먼저 저장해 주세요.")
    elif not valid_url(base) or urlsplit(base).query or urlsplit(base).fragment:
        st.info("정확한 배포 주소를 입력해 주세요. 예: https://농장앱.streamlit.app (실제 발급 주소 사용)")
    else:
        link = base.rstrip("/") + "/?" + urlencode({"product": qr_id})
        image = qrcode.make(link)
        buf = BytesIO(); image.save(buf, format="PNG")
        st.image(buf.getvalue(), width=220)
        st.download_button("📥 QR 이미지 다운로드", buf.getvalue(), file_name="farm-product-qr.png", mime="image/png", use_container_width=True)
        st.link_button("고객 안내 페이지 확인", link, use_container_width=True)
        st.code(link, language=None)
        st.caption("인쇄 전에 다른 스마트폰에서 QR을 찍어 상품과 구매 링크를 확인하세요.")
