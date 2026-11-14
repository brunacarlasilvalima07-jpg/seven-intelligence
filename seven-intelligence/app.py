import io
import html
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Seven Intelligence", page_icon="✈️", layout="wide")

SOURCES = {
    "Passageiro de Primeira": "https://passageirodeprimeira.com/feed/",
    "Melhores Destinos": "https://www.melhoresdestinos.com.br/feed",
    "Melhores Cartões": "https://www.melhorescartoes.com.br/feed",
    "Pontos pra Voar": "https://pontospravoar.com/feed/",
}
KEYWORDS = ["milhas", "pontos", "smiles", "livelo", "latam pass", "azul", "esfera", "bonificação", "transferência", "accor", "passagens", "promoção"]
PROGRAMS = ["Smiles", "Livelo", "LATAM Pass", "Azul", "Esfera", "ALL Accor", "Iberia", "TAP", "Outros"]
FIELDS = ["id", "titulo", "fonte", "url", "publicado", "programa", "categoria", "prazo", "bonus", "link_oficial", "status", "observacoes", "imagem"]
STATUS = ["Não verificado", "Em análise", "Confirmado no oficial", "Encerrado", "Descartado"]


def initial_df():
    return pd.DataFrame(columns=FIELDS)

# Persistência v2.3: a chave secreta é lida apenas no servidor do Streamlit.
# A tabela existente no Supabase é public.seven_state (id integer, dados jsonb).
def supabase_settings():
    try:
        url = str(st.secrets.get("SUPABASE_URL", "")).strip().rstrip("/")
        key = str(st.secrets.get("SUPABASE_KEY", "")).strip()
    except Exception:
        return "", ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not key or "COLE_AQUI" in key:
        return "", ""
    return url, key


def db_request(method, **kwargs):
    url, key = supabase_settings()
    if not url or not key:
        raise RuntimeError("Credenciais SUPABASE_URL e SUPABASE_KEY não configuradas.")
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Chaves sb_secret_ e sb_publishable_ não são JWTs.
    # A API Supabase aceita essas chaves no cabeçalho apikey.
    if not key.startswith(("sb_secret_", "sb_publishable_")):
        headers["Authorization"] = f"Bearer {key}"
    headers.update(kwargs.pop("headers", {}))
    response = requests.request(
        method, f"{url}/rest/v1/seven_state", headers=headers,
        timeout=15, **kwargs
    )
    if not response.ok:
        # Nunca exibir corpo de resposta ou credenciais: apenas código HTTP.
        raise RuntimeError(f"Supabase retornou HTTP {response.status_code} ({method}).")
    return response.json() if response.content else None


def load_saved_items():
    result = db_request("GET", params={"id": "eq.1", "select": "dados", "limit": "1"})
    if not result:
        return initial_df()
    records = result[0].get("dados", [])
    if not isinstance(records, list):
        raise ValueError("Formato de dados inesperado no Supabase")
    return (pd.DataFrame(records).reindex(columns=FIELDS, fill_value="")
            .fillna("").drop_duplicates(subset=["id"]).reset_index(drop=True))


def save_items():
    if not all(supabase_settings()):
        st.session_state["db_error"] = "Supabase não configurado. Dados apenas nesta sessão; baixe um backup CSV."
        return False
    try:
        records = st.session_state["items"].reindex(columns=FIELDS, fill_value="").fillna("").to_dict(orient="records")
        db_request("POST", params={"on_conflict": "id"},
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                   json={"id": 1, "dados": records})
        st.session_state["db_error"] = ""
        return True
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        st.session_state["db_error"] = f"Não foi possível salvar no Supabase: {exc if isinstance(exc, RuntimeError) else type(exc).__name__}. Exporte um backup CSV."
        return False


if "items" not in st.session_state:
    st.session_state["items"] = initial_df()
    st.session_state["db_error"] = ""
    if all(supabase_settings()):
        try:
            st.session_state["items"] = load_saved_items()
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            st.session_state["db_error"] = f"Não foi possível carregar o Supabase: {exc if isinstance(exc, RuntimeError) else type(exc).__name__}. Não edite antes de restabelecer a conexão."
            st.session_state["db_load_failed"] = True


def clean(text):
    return re.sub(r"\s+", " ", BeautifulSoup(str(text or ""), "html.parser").get_text(" ")).strip()


def detect_program(text):
    t = text.lower()
    for word, name in [("smiles", "Smiles"), ("livelo", "Livelo"), ("latam", "LATAM Pass"), ("azul", "Azul"), ("esfera", "Esfera"), ("accor", "ALL Accor"), ("iberia", "Iberia"), ("tap", "TAP")]:
        if word in t:
            return name
    return "Outros"


def detect_category(text):
    t = text.lower()
    if "transfer" in t or "bônus" in t or "bonus" in t:
        return "Transferência"
    if "comprar pontos" in t or "compra de pontos" in t:
        return "Compra de pontos"
    if "passagem" in t or "voo" in t:
        return "Passagens"
    return "Outros"


def format_date(value):
    if not value:
        return "Data não informada"
    try:
        return parsedate_to_datetime(str(value)).strftime("%d/%m/%Y")
    except (ValueError, TypeError, IndexError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            return str(value)[:30]


def trusted_news_url(url):
    try:
        parsed = urlparse(str(url))
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == urlparse(source).hostname or host.endswith("." + urlparse(source).hostname)
            for source in SOURCES.values()
        )
    except ValueError:
        return False


def rss_image(entry):
    candidates = []
    for field in ("media_content", "media_thumbnail", "enclosures"):
        for obj in entry.get(field, []) or []:
            if isinstance(obj, dict):
                candidates.append(obj.get("url", ""))
    for obj in entry.get("links", []) or []:
        if isinstance(obj, dict) and str(obj.get("type", "")).startswith("image/"):
            candidates.append(obj.get("href", ""))
    content = entry.get("content", []) or []
    for markup in (entry.get("summary", ""), content[0].get("value", "") if content else ""):
        img = BeautifulSoup(str(markup), "html.parser").find("img")
        if img:
            candidates.append(img.get("src", ""))
    for url in candidates:
        if str(url).startswith("https://"):
            return str(url)
    return ""


@st.cache_data(ttl=21600, show_spinner=False)
def article_image(url):
    """Only use actual Open Graph images from supported news publishers."""
    if not trusted_news_url(url):
        return ""
    try:
        response = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0 (compatible; SevenIntelligence/2.3)"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text[:450000], "html.parser")
        for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "og:image:url"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                image_url = urljoin(url, tag["content"])
                if urlparse(image_url).scheme == "https":
                    return image_url
    except (requests.RequestException, ValueError):
        pass
    return ""


def fetch_feed(name, url):
    # RSS feeds may be blocked or unavailable; report errors instead of inventing articles.
    response = requests.get(url, timeout=12, headers={"User-Agent": "SevenIntelligence/0.1 (RSS reader)"})
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise ValueError("RSS indisponível ou inválido")
    rows = []
    for entry in feed.entries[:40]:
        title = clean(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        excerpt = clean(entry.get("summary", ""))
        if not any(k in (title + " " + excerpt).lower() for k in KEYWORDS):
            continue
        rows.append({"id": link, "titulo": title, "fonte": name, "url": link,
                     "publicado": entry.get("published", entry.get("updated", "")),
                     "programa": detect_program(title + " " + excerpt),
                     "categoria": detect_category(title + " " + excerpt),
                     "prazo": "", "bonus": "", "link_oficial": "", "status": "Não verificado", "observacoes": "", "imagem": rss_image(entry)})
    return rows


def add_rows(rows):
    if not rows:
        return 0
    if st.session_state.get("db_load_failed"):
        st.error("Carregamento do banco falhou. Recarregue o aplicativo antes de adicionar notícias.")
        return 0
    old = st.session_state["items"]
    incoming = pd.DataFrame(rows).reindex(columns=FIELDS, fill_value="").fillna("")
    old = old.reindex(columns=FIELDS, fill_value="").fillna("")
    combined = pd.concat([old, incoming], ignore_index=True)
    combined = combined.drop_duplicates(subset=["id"], keep="first")
    st.session_state["items"] = combined.reset_index(drop=True)
    if len(combined) > len(old):
        save_items()
    return len(combined) - len(old)


def is_valid_official(url):
    if not url or not str(url).startswith("https://"):
        return False
    host = (urlparse(str(url)).hostname or "").lower()
    return host not in ["passageirodeprimeira.com", "www.passageirodeprimeira.com", "melhoresdestinos.com.br", "www.melhoresdestinos.com.br", "melhorescartoes.com.br", "www.melhorescartoes.com.br", "pontospravoar.com", "www.pontospravoar.com"]


def alert_text(item):
    title = item["titulo"]
    deadline = item["prazo"] or "Prazo não confirmado"
    bonus = item["bonus"] or "Condição a conferir no regulamento"
    official = item["link_oficial"] or "Link oficial não confirmado"
    return f"🔥 *OPORTUNIDADE SEVEN TRAVEL*\n\n✈️ *{title.upper()}*\n\n📆 *Prazo:* {deadline}\n🎯 *Condição:* {bonus}\n\n⚠️ Confira elegibilidade, limites, cadastro e regulamento antes de participar.\n\n🔗 *Link oficial:* {official}\n\n✈️ *SEVEN TRAVEL | Inteligência para viajar melhor.*"


# Seven Intelligence 2.3 — visual aprovado + persistência Supabase.
# O conteúdo de notícias é real; números e imagens ilustrativas não são inseridos.
st.markdown("""
<style>
:root { --gold:#bd881b; --gold-light:#f9edcd; --ink:#111317; --line:#e9e5dd; }
.stApp {background:#fcfbf9;color:var(--ink);}
header[data-testid="stHeader"] {background:transparent !important; height:2.5rem;}
.block-container {max-width:1500px !important;padding:1.35rem 2rem 4rem !important;}
[data-testid="stSidebar"] {background:linear-gradient(150deg,#101114,#181a1e) !important; min-width:250px;}
[data-testid="stSidebar"] > div {padding-top:1.4rem;}
[data-testid="stSidebar"] * {color:#f8f5ee;}
[data-testid="stSidebar"] [data-testid="stRadio"] label {font-weight:650;}
[data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] label {padding:.42rem .6rem;border-radius:9px;}
[data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] label:has(input:checked) {background:linear-gradient(100deg,#a87813,#d3a132);}
[data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] label:has(input:checked) * {color:white !important;}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {color:#b9b7b2;}
.seven-logo {text-align:center;padding:12px 0 27px;line-height:1;}
.seven-logo .brand {font:italic 2.45rem Georgia,serif;letter-spacing:.11em;color:#fff;}
.seven-logo .travel {font: .8rem Arial,sans-serif;letter-spacing:.36em;color:#fff;margin-top:8px;}
.seven-logo .orbit {border-right:3px solid #d3a132;border-top:2px solid #d3a132;border-radius:50%;width:145px;height:83px;display:flex;align-items:center;justify-content:center;flex-direction:column;margin:auto;transform:rotate(-4deg);}
.seven-logo .orbit > * {transform:rotate(4deg);}
.seven-kicker {color:#b48117;font-size:.85rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase;}
.seven-hero {position:relative;overflow:hidden;padding:1.0rem 0 1.5rem;}
.seven-hero:after {content:"✈";position:absolute;right:4%;top:-12px;color:#c99a37;font-size:4.2rem;transform:rotate(-17deg);opacity:.82;pointer-events:none;}
.seven-hero h1 {font-family:Georgia,serif !important;font-size:clamp(2rem,3.1vw,3.25rem) !important;line-height:1.12;font-weight:700;letter-spacing:-.035em;color:#0e0f12;margin:.3rem 0 .5rem;padding:0;}
.seven-hero p {font-size:1rem;color:#54565c;margin:0;max-width:90%;}
.seven-section {font-family:Georgia,serif;font-size:1.8rem;font-weight:700;color:#121317;margin:0;padding:0;}
.seven-underline {width:55px;height:3px;border-radius:3px;background:#bb881f;margin:9px 0 14px;}
.seven-card {background:#fff;border:1px solid var(--line);border-radius:15px;box-shadow:0 3px 12px rgba(20,19,17,.04);padding:16px 17px;min-height:115px;}
.seven-card.featured {background:linear-gradient(110deg,#fff9e9,#fff);border-color:#e8d5a4;}
.seven-card .card-top {font-size:.91rem;font-weight:600;color:#33363a;margin-bottom:10px;}
.seven-card .card-num {font-size:2.2rem;font-weight:800;color:#121317;line-height:1;}
.seven-card .card-icon {float:right;font-size:1.8rem;color:#b78622;}
.seven-article {background:#fff;border:1px solid #ece9e3;border-radius:16px;padding:14px 16px;box-shadow:0 3px 10px rgba(0,0,0,.045);margin:4px 0;}
.seven-article-title {font-size:1.04rem;line-height:1.32;font-weight:760;color:#111;margin-bottom:8px;}
.seven-article-meta {color:#67686b;font-size:.81rem;line-height:1.5;}
.seven-article-badge {display:inline-block;padding:5px 11px;border-radius:20px;background:#f1f2f4;color:#53565c;font-size:.76rem;font-weight:600;}
.seven-article-badge.ok {background:#edf7ed;color:#28723a;}
.seven-article-badge.progress {background:#fff5dc;color:#966a12;}
.seven-thumb {height:82px;width:100%;display:flex;align-items:center;justify-content:center;border-radius:11px;background:linear-gradient(135deg,#f7eed7,#f0f1f2);font-size:2.15rem;}
.seven-photo {width:100%;height:82px;object-fit:cover;display:block;border-radius:11px;background:#f7f0e3;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.seven-article-title) {box-shadow:0 3px 13px rgba(18,17,15,.045);background:#fff;border:1px solid #ece8e0;}
[data-testid="stMetric"] {background:#fff;padding:15px 18px;border:1px solid var(--line);border-radius:14px;}
[data-testid="stMetricValue"] {color:#111;}
div[data-testid="stVerticalBlockBorderWrapper"] > div {border-color:var(--line);border-radius:15px;}
.stButton button[kind="primary"],[data-testid="stFormSubmitButton"] button[kind="primary"] {background:linear-gradient(100deg,#ae7b13,#c99528);color:#fff !important;border:0;}
.stButton button {border-radius:9px;min-height:39px;font-weight:650;}
.stButton button:not([kind="primary"]) {background:#fff;color:#9c6c0b;border:1px solid #d8b16b;}
.stButton button:disabled {opacity:.55;}
.stTextInput label,.stSelectbox label {color:#333 !important;}
[data-testid="stWidgetLabel"] p {color:#42444a !important;}
[data-testid="stAlert"] {border-radius:11px;}
@media(max-width:850px){.block-container{padding:1rem .8rem 3rem !important}.seven-hero:after{opacity:.25}.seven-hero h1{font-size:2rem !important}}
</style>
""", unsafe_allow_html=True)

def safe(value):
    return html.escape(str(value or ""), quote=True)

def category_symbol(category):
    return {"Passagens":"✈️","Compra de pontos":"◉","Transferência":"⇄","Transferência bonificada":"⇄","Cartões":"▣","Hotéis":"▤","Acúmulo":"✦"}.get(category,"✧")

def goto_review(item_id):
    st.session_state["selected_id"] = str(item_id)
    st.session_state["nav"] = "Revisão e alertas"
    st.rerun()

with st.sidebar:
    st.markdown('<div class="seven-logo"><div class="orbit"><div class="brand">SEVEN</div><div class="travel">TRAVEL</div></div></div>',unsafe_allow_html=True)
    if "nav" not in st.session_state:
        st.session_state["nav"] = "Painel"
    page = st.radio("Navegação",["Painel","Coleta","Revisão e alertas","Backup"],key="nav")
    st.divider()
    st.markdown('<div style="color:#e4bd68;font-weight:750;letter-spacing:.08em">✦ SEVEN INTELLIGENCE</div>',unsafe_allow_html=True)
    st.caption("Monitoramento inteligente de promoções, pontos e viagens.")
    st.divider()
    st.caption("Notícias não são promoções validadas. Confira o regulamento oficial e exporte backups.")

if st.session_state.get("db_error"):
    st.warning(st.session_state["db_error"])
elif all(supabase_settings()):
    st.caption("☁️ Supabase conectado · Notícias e revisões com salvamento persistente")
else:
    st.warning("Supabase não configurado: dados temporários. Configure as credenciais em App settings → Secrets.")

st.markdown("""<section class="seven-hero">
<div class="seven-kicker">SEVEN INTELLIGENCE</div>
<h1>Central de Promoções e Oportunidades</h1>
<p>Monitoramento inteligente de promoções, programas de fidelidade e benefícios do mercado de viagens.</p>
</section>""",unsafe_allow_html=True)

df = st.session_state["items"]

if page == "Painel":
    counts = [
        ("Notícias coletadas",len(df),"▤"),
        ("Em análise",int((df["status"]=="Em análise").sum()) if len(df) else 0,"⌕"),
        ("Confirmadas",int((df["status"]=="Confirmado no oficial").sum()) if len(df) else 0,"✓"),
        ("Encerradas",int((df["status"]=="Encerrado").sum()) if len(df) else 0,"⊗"),
    ]
    for col,(name,value,symbol) in zip(st.columns(4,gap="medium"),counts):
        with col:
            featured = " featured" if name=="Notícias coletadas" else ""
            st.markdown(f'<div class="seven-card{featured}"><div class="card-icon">{symbol}</div><div class="card-top">{safe(name)}</div><div class="card-num">{value}</div></div>',unsafe_allow_html=True)
    st.write("")
    heading, action = st.columns([3.4,1],vertical_alignment="center")
    with heading:
        st.markdown('<div class="seven-section">Oportunidades Identificadas</div><div class="seven-underline"></div>',unsafe_allow_html=True)
    with action:
        if st.button("↻ Buscar notícias agora",type="primary",use_container_width=True):
            st.session_state["nav"]="Coleta"
            st.rerun()
    qcol,pcol,ccol,scol=st.columns([2.2,1,1,1],gap="medium")
    with qcol:
        query=st.text_input("Buscar",placeholder="Buscar por título, programa ou palavra-chave...")
    with pcol:
        program=st.selectbox("Programa",["Todos"]+PROGRAMS)
    with ccol:
        categories=sorted(set(["Passagens","Compra de pontos","Transferência","Cartões","Hotéis","Acúmulo","Outros"]+df["categoria"].dropna().tolist()))
        category=st.selectbox("Categoria",["Todas"]+categories)
    with scol:
        status=st.selectbox("Status",["Todos"]+STATUS)
    filtered=df.copy()
    if query:
        search_text=filtered[["titulo","programa","fonte","categoria"]].fillna("").agg(" ".join,axis=1)
        filtered=filtered[search_text.str.contains(query,case=False,regex=False,na=False)]
    if program!="Todos":filtered=filtered[filtered["programa"]==program]
    if category!="Todas":filtered=filtered[filtered["categoria"]==category]
    if status!="Todos":filtered=filtered[filtered["status"]==status]
    st.write("")
    tab_names=["Todas","Passagens","Compra de pontos","Transferência","Cartões","Hotéis","Outros"]
    if "category_tab" not in st.session_state:st.session_state["category_tab"]="Todas"
    tabs=st.columns([1.0,1.1,1.5,1.35,.9,.8,.8],gap="small")
    for col,cat in zip(tabs,tab_names):
        n=len(filtered) if cat=="Todas" else int((filtered["categoria"]==cat).sum())
        if col.button(f"{'✈ ' if cat=='Passagens' else ''}{cat} ({n})",key=f"cat_{cat}",use_container_width=True,
                      type="primary" if st.session_state["category_tab"]==cat else "secondary"):
            st.session_state["category_tab"]=cat
            st.rerun()
    selected_tab=st.session_state["category_tab"]
    if selected_tab!="Todas":filtered=filtered[filtered["categoria"]==selected_tab]
    st.caption(f"{len(filtered)} notícia(s) encontrada(s) · Dados reais das fontes RSS · Revisão manual obrigatória")
    if filtered.empty:
        st.info("Nenhuma notícia para esses filtros. Use Coleta para buscar notícias ou restaure seu backup CSV.")
    else:
        limit=st.select_slider("Quantidade de notícias",options=[10,20,40,80,150],value=20)
        visible=filtered.head(limit)
        missing=[(idx,item["url"]) for idx,item in visible.iterrows() if not str(item.get("imagem", "") or "").startswith("https://")]
        image_urls={}
        if missing:
            with st.spinner("Carregando fotos reais das matérias..."):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results=list(executor.map(article_image,[url for _,url in missing]))
                image_urls=dict(zip([idx for idx,_ in missing],results))
        for idx,item in visible.iterrows():
            with st.container(border=True):
                thumb,body,badge,review,alert,open_link=st.columns([.7,3.15,1.0,.75,.95,.65],vertical_alignment="center",gap="small")
                with thumb:
                    photo = str(item.get("imagem", "") or image_urls.get(idx, "") or "")
                    if photo.startswith("https://"):
                        fallback = category_symbol(item["categoria"])
                        st.markdown(f'<img class="seven-photo" src="{safe(photo)}" alt="Foto da matéria" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'"><div class="seven-thumb" style="display:none">{fallback}</div>',unsafe_allow_html=True)
                    else:
                        st.markdown(f'<div class="seven-thumb">{category_symbol(item["categoria"])}</div>',unsafe_allow_html=True)
                with body:
                    st.markdown(f'<div class="seven-article-title">{safe(item["titulo"])}</div><div class="seven-article-meta">{safe(item["programa"])} &nbsp; · &nbsp; {safe(item["categoria"])} &nbsp; · &nbsp; {safe(item["fonte"])}<br>{safe(item["prazo"] or format_date(item["publicado"]))}</div>',unsafe_allow_html=True)
                with badge:
                    style="ok" if item["status"]=="Confirmado no oficial" else "progress" if item["status"]=="Em análise" else ""
                    st.markdown(f'<span class="seven-article-badge {style}">{safe(item["status"])}</span>',unsafe_allow_html=True)
                with review:
                    if st.button("Revisar",key=f"review_{idx}",use_container_width=True):
                        goto_review(item["id"])
                with alert:
                    if st.button("Gerar alerta",key=f"alert_{idx}",use_container_width=True,
                                 type="primary",disabled=item["status"]!="Confirmado no oficial",
                                 help="Disponível após confirmação no regulamento oficial"):
                        goto_review(item["id"])
                with open_link:
                    st.link_button("Abrir ↗",item["url"],use_container_width=True)

elif page == "Coleta":
    st.subheader("Coleta de notícias")
    st.caption("Feeds RSS públicos, quando disponíveis. A coleta não confirma as condições das promoções.")
    with st.container(border=True):
        selected = st.multiselect("Fontes monitoradas", list(SOURCES), default=list(SOURCES))
        if st.button("↻ Buscar notícias agora", type="primary", disabled=bool(st.session_state.get("db_load_failed"))):
            all_rows = []
            for name in selected:
                try:
                    rows = fetch_feed(name, SOURCES[name])
                    all_rows.extend(rows)
                    st.success(f"{name}: {len(rows)} notícias relevantes encontradas")
                except Exception as exc:
                    st.warning(f"{name}: não foi possível consultar o RSS ({type(exc).__name__}).")
            added = add_rows(all_rows)
            st.success(f"{added} novas notícias adicionadas" + (" e salvas no Supabase" if added and not st.session_state.get("db_error") else ""))
    st.subheader("Adicionar matéria manualmente")
    with st.form("manual"):
        title = st.text_input("Título da matéria")
        url = st.text_input("URL pública da matéria")
        source = st.text_input("Fonte")
        submitted = st.form_submit_button("Adicionar notícia", disabled=bool(st.session_state.get("db_load_failed")))
    if submitted:
        if title.strip() and url.startswith("https://"):
            n = add_rows([{"id": url, "titulo": title.strip(), "fonte": source.strip(), "url": url,
                           "publicado": "", "programa": detect_program(title), "categoria": detect_category(title),
                           "prazo": "", "bonus": "", "link_oficial": "", "status": "Não verificado", "observacoes": ""}])
            st.success("Matéria adicionada" if n else "Matéria já cadastrada")
        else:
            st.error("Informe título e URL HTTPS válida.")

elif page == "Revisão e alertas":
    st.subheader("Revisão e alertas WhatsApp")
    st.caption("A confirmação exige conferência humana do regulamento oficial. O sistema não valida automaticamente a campanha.")
    if df.empty:
        st.info("Colete ou adicione notícias antes de revisar.")
    else:
        ids = df.index.tolist()
        selected_id = st.session_state.get("selected_id")
        matches = df.index[df["id"].astype(str) == str(selected_id)].tolist() if selected_id else []
        default_idx = ids.index(matches[0]) if matches else 0
        chosen = st.selectbox("Selecione a notícia", ids, index=default_idx,
                              format_func=lambda i: f"{df.loc[i, 'titulo'][:90]} — {df.loc[i, 'fonte']}")
        item = df.loc[chosen].to_dict()
        st.link_button("Abrir matéria original", item["url"])
        with st.container(border=True):
            with st.form("review"):
                program = st.selectbox("Programa", PROGRAMS, index=PROGRAMS.index(item["programa"]) if item["programa"] in PROGRAMS else len(PROGRAMS)-1)
                categories = ["Transferência", "Compra de pontos", "Passagens", "Acúmulo", "Outros"]
                category = st.selectbox("Categoria", categories, index=categories.index(item["categoria"]) if item["categoria"] in categories else 4)
                deadline = st.text_input("Prazo verificado (data e horário)", value=item["prazo"])
                bonus = st.text_input("Condição / bônus confirmado", value=item["bonus"])
                official = st.text_input("Link OFICIAL do regulamento", value=item["link_oficial"])
                notes = st.text_area("Regras, elegibilidade e observações", value=item["observacoes"])
                current_status = st.selectbox("Status", STATUS, index=STATUS.index(item["status"]) if item["status"] in STATUS else 0)
                save = st.form_submit_button("Salvar revisão", type="primary")
            if save:
                if st.session_state.get("db_load_failed"):
                    st.error("Banco não carregado. Não é seguro salvar esta revisão.")
                elif current_status == "Confirmado no oficial" and (not deadline.strip() or not bonus.strip() or not is_valid_official(official)):
                    st.error("Para confirmar, preencha prazo, condição e URL HTTPS oficial (não a matéria de terceiros). Confira manualmente se a URL realmente pertence ao programa.")
                else:
                    for col, val in {"programa": program, "categoria": category, "prazo": deadline, "bonus": bonus, "link_oficial": official, "observacoes": notes, "status": current_status}.items():
                        st.session_state["items"].at[chosen, col] = val
                    if save_items():
                        st.session_state["flash"] = "Revisão salva no Supabase."
                    else:
                        st.session_state["flash"] = "Revisão alterada apenas nesta sessão; faça backup CSV."
                    st.rerun()
        if st.session_state.pop("flash", None):
            st.info("Revisão processada. Confira o aviso de conexão acima.")
        item = st.session_state["items"].loc[chosen].to_dict()
        if item["status"] == "Confirmado no oficial":
            st.subheader("Alerta Seven Travel para WhatsApp")
            st.code(alert_text(item), language=None)
            st.download_button("Baixar alerta .txt", alert_text(item), file_name="alerta_seven_travel.txt", mime="text/plain")
            st.caption("Revise a mensagem antes de enviar. Não há publicação automática.")
        else:
            st.warning("O alerta só será liberado quando a promoção estiver marcada como Confirmado no oficial.")

elif page == "Backup":
    st.subheader("Backup e restauração")
    if all(supabase_settings()) and not st.session_state.get("db_error"):
        st.info("Dados persistidos no Supabase. Recomendamos também baixar backups CSV regularmente.")
    else:
        st.warning("Sem conexão confirmada com o Supabase, os dados podem desaparecer após reiniciar. Baixe o backup CSV.")
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Baixar backup CSV", csv_bytes, file_name="seven_intelligence_backup.csv", mime="text/csv", type="primary")
    if st.session_state.pop("flash_backup", None):
        st.info("Restauração concluída. Confira o status do Supabase acima.")
    uploaded = st.file_uploader("Restaurar backup CSV", type=["csv"])
    if uploaded and st.button("Restaurar backup", disabled=bool(st.session_state.get("db_load_failed"))):
        try:
            restored = pd.read_csv(uploaded, dtype=str).fillna("")
            if not (set(FIELDS) - {"imagem"}).issubset(restored.columns):
                st.error("CSV não contém todas as colunas necessárias.")
            else:
                st.session_state["items"] = restored.reindex(columns=FIELDS, fill_value="").fillna("").drop_duplicates(subset=["id"]).reset_index(drop=True)
                if save_items():
                    st.session_state["flash_backup"] = "Backup restaurado e salvo no Supabase."
                else:
                    st.session_state["flash_backup"] = "Backup restaurado nesta sessão, mas NÃO salvo no Supabase."
                st.rerun()
        except Exception:
            st.error("Não foi possível ler o arquivo CSV.")
