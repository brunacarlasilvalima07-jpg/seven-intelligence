import io
import base64
import html
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import json
import re
import unicodedata
from datetime import datetime, timezone, date, time
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import feedparser
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
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
CATEGORIES = ["Transferência", "Compra de pontos", "Passagens", "Cartões", "Hotéis", "Acúmulo", "Outros"]
FIELDS = ["id", "titulo", "fonte", "url", "publicado", "programa", "categoria", "prazo", "bonus", "link_oficial", "status", "observacoes", "imagem", "origem_pontos", "destino_milhas", "percentual_bonus", "verificado_em", "auto_link_candidato", "auto_percentual", "auto_prazo", "auto_analisado_em", "validade_iso", "validade_evidencia", "situacao_fonte", "bonus_base", "bonus_maximo", "custo_milheiro", "moeda_milheiro", "origem_auto", "destino_auto", "status_previo", "auto_versao", "auto_tipo_percentual", "auto_desconto", "auto_custo_sugerido", "auto_evidencia_bonus", "auto_origem_evidencia", "auto_data_referencia", "validade_hora", "auto_falha", "auto_processado_em"]
STATUS = ["Não verificado", "Em análise", "Confirmado no oficial", "Encerrado", "Descartado"]
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


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
    if parsed.scheme != "https" or not parsed.hostname or not key.startswith("sb_secret_") or "COLE_AQUI" in key:
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


def normalize_text(text):
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


MONTHS_PT = {"janeiro":1,"fevereiro":2,"marco":3,"abril":4,"maio":5,"junho":6,
             "julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12}


def publication_date(value):
    try:
        return parsedate_to_datetime(str(value)).date()
    except (ValueError, TypeError, IndexError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            return None


def promotion_deadline(text, reference=None):
    """Extract explicit campaign closing dates, not travel dates or publication timestamps.

    Relative weekday expressions are resolved against a real article publication date only.
    The evidence is preserved to make each automatic decision auditable.
    """
    t = normalize_text(text)
    # Exclude paragraphs explicitly about travel rather than joining two unrelated dates.
    if re.search(r"(?:datas? de viagem|periodo de viagem|embarques? entre|check.?in|hospedagens? de)", t[:110]):
        return "", ""
    contexts = (r"(?:valida|valido)\s+ate|validade\s+ate|vigente\s+ate|disponivel\s+ate|"
                r"prazo\s+(?:final\s+)?(?:ate|termina)|"
                r"(?:promocao|campanha|oferta|bonificacao|cadastro|transferencias?)\s+(?:e\s+)?(?:valida?\s+)?(?:vai\s+)?ate|"
                r"encerra(?:-se|mento)?\s+(?:em|no|dia|ate)|termina\s+(?:em|no|dia|ate)|"
                r"expira\s+(?:em|no|dia|ate)|cadastre-se\s+ate|"
                r"(?:somente|so|apenas)\s+ate|ate\s+o\s+dia|"
                r"(?:promocao|campanha|oferta)\s+termina\s+(?:no\s+dia|em)|"
                r"(?:ate|ate\s+as)\s+\d{1,2}(?:h|:)\d{2}\s+do\s+dia")
    calendar = (r"(?P<d>\d{1,2})\s*[/.-]\s*(?P<m>\d{1,2})(?:\s*[/.-]\s*(?P<y>20\d{2}))?|"
                r"(?P<dd>\d{1,2})\s+de\s+(?P<month>janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)"
                r"(?:\s+de\s+(?P<yy>20\d{2}))?")
    # Limit distance from 'valid until' to date: excludes paragraphs with many unrelated dates.
    pat = rf"(?:{contexts})\s*(?:as?\s+\d{{1,2}}(?::\d{{2}})?\s*(?:h|horas)?\s+do\s+dia\s+|o\s+dia\s+|dia\s+|em\s+)?(?:{calendar})"
    for m in re.finditer(pat, t):
        g = m.groupdict()
        try:
            day = int(g.get('d') or g.get('dd'))
            month = int(g['m']) if g.get('m') else MONTHS_PT[g['month']]
            year = int(g.get('y') or g.get('yy')) if (g.get('y') or g.get('yy')) else None
            if year is None:
                if reference is None:
                    continue
                candidates = (date(reference.year, month, day), date(reference.year + 1, month, day))
                deadline = next((x for x in candidates if -5 <= (x - reference).days <= 65), None)
                if deadline is None:
                    continue
            else:
                deadline = date(year, month, day)
            # Preserve an explicit end time, including trailing 'às 18h'.
            tail = t[m.end():m.end()+45]
            tm = re.search(r"^\s*(?:as|ate as)\s*(\d{1,2})(?::|h)(\d{2})\b|^\s*(?:as|ate as)\s*(\d{1,2})(?:h|horas)?\b", tail)
            evidence = t[m.start():m.end()]
            if tm:
                hr = int(tm.group(1) or tm.group(3))
                mn = int(tm.group(2) or '0')
                if hr <= 23 and mn <= 59:
                    evidence += ' ' + tm.group(0).strip()
            return deadline.isoformat(), evidence[:170]
        except (ValueError, TypeError, KeyError):
            continue
    # Publication-anchored 'today' only when an explicit deadline is stated.
    if reference is not None and re.search(rf"(?:{contexts})\s*(?:hoje)\b", t):
        return reference.isoformat(), "até hoje [referência: publicação]"
    # Explicit relative deadline only when publication date is available.
    if reference is not None:
        weekdays = {'segunda':0, 'terca':1, 'quarta':2, 'quinta':3, 'sexta':4, 'sabado':5, 'domingo':6}
        relative = re.search(rf"(?:{contexts})\s*(?:esta|este|proxima|proximo)?\s*(segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?\b", t)
        if relative:
            target = weekdays[relative.group(1)]
            delta = (target-reference.weekday()) % 7
            from datetime import timedelta
            return (reference + timedelta(days=delta)).isoformat(), relative.group(0)[:170] + ' [referência: publicação]'
    return "", ""


def deadline_clock(evidence):
    """Extract clock time only when attached to the identified closing-date evidence."""
    t = normalize_text(evidence)
    m = re.search(r"(?:as|ate as)\s*(\d{1,2})(?::|h)(\d{2})\b|(?:as|ate as)\s*(\d{1,2})(?:h|horas)?\b", t)
    if not m:
        return ""
    h, minute = int(m.group(1) or m.group(3)), int(m.group(2) or '0')
    return f'{h:02d}:{minute:02d}' if h <= 23 and minute <= 59 else ""


PAIR_NAMES = {
    "itau": "Itaú", "livelo": "Livelo", "esfera": "Esfera", "azul fidelidade": "Azul", "azul": "Azul",
    "smiles": "Smiles", "latam pass": "LATAM Pass", "latam": "LATAM Pass", "caixa": "Caixa",
    "banco do brasil": "BB", "bb": "BB", "brb": "BRB", "sicredi": "Sicredi",
    "sicoob coopera": "Sicoob Coopera", "sicoob": "Sicoob", "banrisul": "Banrisul",
    "unicred": "Unicred", "cresol": "Cresol", "all accor": "ALL Accor", "accor": "ALL Accor",
    "iberia": "Iberia", "qatar airways": "Qatar Airways"
}


def infer_transfer_pair(text):
    """Requires an explicit directional transfer expression, never mere co-occurrence."""
    t=normalize_text(text)
    names="|".join(re.escape(k) for k in sorted(PAIR_NAMES,key=len,reverse=True))
    patterns=[
      rf"(?:pontos|milhas)\s+(?:da|do|de)\s+(?P<a>{names})\s+(?:para|ao|a|→|->)\s+(?P<b>{names})\b",
      rf"(?P<a>{names})\s*(?:→|->)\s*(?P<b>{names})\b",
      rf"(?:transf(?:erir|ira|erencia)(?:\s+de)?\s+(?:pontos|milhas)?\s*(?:da|do|de)?\s*)(?P<a>{names})\s+(?:para|ao|a)\s+(?P<b>{names})\b",
      rf"(?P<a>{names})\s+(?:para|ao)\s+(?P<b>{names})\s+(?:com|oferece|ate|ganhe|bonus|bonificacao)\b"
    ]
    for pat in patterns:
        m=re.search(pat,t)
        if m and PAIR_NAMES[m.group('a')] != PAIR_NAMES[m.group('b')]:
            return PAIR_NAMES[m.group('a')],PAIR_NAMES[m.group('b')]
    return "",""


def extract_percentages(text, category):
    """Strict local-context extraction. Never treat discount/cashback as transfer bonus.
    Returns (bonus_base, bonus_max, discount, type, evidence).
    """
    t=normalize_text(text)
    if category != "Transferência":
        m=re.search(r"(?:ate\s+)?(\d{1,3})\s*%\s*(?:de\s+)?(?:desconto|off|cashback)|(?:desconto|cashback|off)\s+(?:de|ate)\s+(\d{1,3})\s*%",t)
        if m:
            val=int(m.group(1) or m.group(2))
            if val<=100:return "","",f"{val}%", "cashback" if "cashback" in m.group(0) else "desconto", m.group(0)[:140]
        return "","","","", ""
    if not re.search(r"transfer|bonificad|bonificac|bonus",t):
        return "","","","", ""
    # only percentages explicitly tied to bonuses in a short local span
    findings=[]
    for m in re.finditer(r"\b(\d{1,3})\s*%",t):
        val=int(m.group(1))
        if val>200:continue
        nearby=t[max(0,m.start()-65):min(len(t),m.end()+50)]
        if re.search(r"desconto|cashback|\boff\b|taxa|economia",nearby):continue
        if not re.search(r"bonus|bonificac|bonificad|transfer",nearby):continue
        findings.append((m,val,nearby))
    if not findings:return "","","","", ""
    # 'até X%' denotes maximum only; multiple percentages require explicit base/max markers
    for m,val,context in findings:
        before=t[max(0,m.start()-12):m.start()]
        if re.search(r"\bate\s*$",before):
            base=""
            explicit=re.search(r"(\d{1,3})\s*%\s*(?:de\s+)?(?:bonus\s+)?base",t)
            if explicit and int(explicit.group(1))<=200:base=explicit.group(1)+"%"
            return base, str(val)+"%", "", "bonus_transferencia",context[:140]
    for m,val,context in findings:
        if re.search(r"base|padrao|geral",context):
            return str(val)+"%","","","bonus_transferencia",context[:140]
    # Single bonus without 'até' is known but not identified as base or max.
    return "","","","bonus_nao_especificado",findings[0][2][:140]


def bonus_bounds(text):
    base,top,_,_,_=extract_percentages(text,"Transferência")
    return base,top


def inferred_cost_per_thousand(text,category):
    if category != "Compra de pontos":return ""
    t=normalize_text(text)
    m=re.search(r"(?:milheiro|1\.?000\s+(?:pontos|milhas))\s*(?:a|por|custa|sai\s+por|de)\s*r\$\s*(\d{1,3}(?:[.,]\d{1,2})?)",t)
    return m.group(1).replace(".",",") if m else ""

def expiry_state(row, on_date=None):
    """Only date evidenced in source; date-only promotion closes at day's end in Brazil."""
    iso = str(row.get("validade_iso", "") or "")
    if iso:
        try:
            deadline = date.fromisoformat(iso)
            today = on_date or datetime.now(BRAZIL_TZ).date()
            expired = deadline < today
            if deadline == today and not on_date:
                clock = str(row.get("validade_hora", "") or "")
                if re.fullmatch(r"\d{2}:\d{2}", clock):
                    hour, minute = (int(x) for x in clock.split(':'))
                    current = datetime.now(BRAZIL_TZ)
                    expired = (current.hour, current.minute) > (hour, minute)
            return "Encerrada pela data divulgada" if expired else "Ativa segundo a fonte"
        except ValueError:
            pass
    return "Prazo não identificado"


def sync_deadlines():
    """On each session run, archive elapsed explicit-date campaigns; preserve earlier manual status."""
    if st.session_state.get("db_load_failed") or st.session_state.get("db_error"):
        return
    data=st.session_state["items"]
    changed=False
    for idx,row in data.iterrows():
        state=expiry_state(row)
        if str(row.get("situacao_fonte", ""))!=state:
            data.at[idx,"situacao_fonte"]=state
            changed=True
        if state=="Encerrada pela data divulgada" and row.get("status") not in ("Encerrado","Descartado"):
            if not str(row.get("status_previo", "")):
                data.at[idx,"status_previo"]=str(row.get("status", "Não verificado"))
            data.at[idx,"status"]="Encerrado"
            changed=True
    if changed:
        st.session_state["items"]=data
        save_items()

def detect_program(text):
    t = normalize_text(text)
    patterns = [
        (r"\bsmiles\b", "Smiles"), (r"\blivelo\b", "Livelo"),
        (r"\blatam(?:\s+pass)?\b", "LATAM Pass"),
        (r"\bazul(?:\s+fidelidade)?\b", "Azul"),
        (r"\besfera\b", "Esfera"), (r"\b(?:all\s+accor|accor)\b", "ALL Accor"),
        (r"\b(?:iberia|avios)\b", "Iberia"), (r"\btap\b", "TAP"),
    ]
    for pattern, name in patterns:
        if re.search(pattern, t):
            return name
    return "Outros"


def detect_category(text):
    # Classificação sugerida por regras, nunca confirmação da validade da oferta.
    t = normalize_text(text)
    if re.search(r"transferenc|transferir|transfira|bonificad", t):
        return "Transferência"
    if re.search(r"compr(ar|a|e) (?:de )?(?:pontos|milhas)|desconto (?:na|para) compra (?:de )?(?:pontos|milhas)|\b(?:vende|venda|compre|comprar)\s+(?:de\s+)?(?:pontos|milhas)\b", t):
        return "Compra de pontos"
    if re.search(r"\b(?:cartoes|cartao|credito|anuidade|porto bank|portoplus|mastercard|visa)\b", t):
        return "Cartões"
    if re.search(r"\b(?:hoteis|hotel|hospedagem|resort|diarias|all accor)\b", t):
        return "Hotéis"
    if re.search(r"\b(?:passagens|passagem|voos|voo|tarifa|aerea|aereo)\b", t):
        return "Passagens"
    if re.search(r"\b(?:acumulo|acumule|ganhe pontos|pontue)\b", t):
        return "Acúmulo"
    return "Outros"


def review_history(dataframe):
    # Histórico inclui campanhas datadas pela notícia ou confirmadas oficialmente.
    # A coluna "Comprovação" distingue ambas as situações.
    history = dataframe[(dataframe["status"].isin(["Confirmado no oficial", "Encerrado"])) &
                        ((dataframe["validade_iso"].fillna("") != "") |
                         (dataframe["verificado_em"].fillna("") != "")) &
                        ((dataframe["auto_versao"].fillna("")=="3.0") | (dataframe["verificado_em"].fillna("")!=""))].copy()
    if history.empty:
        return history
    history["Comprovação"] = history.apply(lambda row:
        "Oficial conferido" if str(row.get("verificado_em", "")) else "Prazo divulgado pela notícia", axis=1)
    history["Data de referência"] = history.apply(lambda row: row.get("validade_iso") or format_date(row.get("publicado")),axis=1)
    return history.sort_values(by="Data de referência", ascending=False, kind="stable")


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
                     "categoria": detect_category(title),
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


# Análise automática assistida: coleta evidências, mas nunca confirma uma promoção.
# Os campos auto_* são separados das condições verificadas por uma pessoa.
OFFICIAL_HOSTS = (
    "livelo.com.br", "smiles.com.br", "voeazul.com.br", "azul.com.br",
    "latamairlines.com", "latampass.latam.com", "esfera.com.vc",
    "itau.com.br", "bb.com.br", "caixa.gov.br", "santander.com.br",
    "brb.com.br", "sicredi.com.br", "sicoob.com.br", "banrisul.com.br",
    "premmia.com.br", "all.accor.com", "iberia.com", "flytap.com",
)

def is_known_official(link):
    try:
        parsed = urlparse(str(link))
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in OFFICIAL_HOSTS)
    except ValueError:
        return False


def guess_promotion(title, category=None):
    category=category or detect_category(title)
    b,top,discount,kind,_=extract_percentages(title,category)
    return (top or b or discount), ""


@st.cache_data(ttl=21600, show_spinner=False)
def analyze_article(article_url, title, category=None, published=""):
    """Conservative evidence-backed suggestions; no automatic official verification."""
    category=category or detect_category(title)
    reference=publication_date(published)
    result={"percentual":"","prazo":"","link":"","sucesso":False,
            "validade_iso":"","validade_evidencia":"","origem":"","destino":"",
            "bonus_base":"","bonus_maximo":"","tipo_percentual":"","desconto":"",
            "custo_sugerido":"","evidencia_bonus":"","origem_evidencia":"titulo", "validade_hora":""}
    result["origem"],result["destino"]=infer_transfer_pair(title) if category=="Transferência" else ("","")
    result["validade_iso"],result["validade_evidencia"]=promotion_deadline(title,reference)
    result["validade_hora"] = deadline_clock(result["validade_evidencia"])
    b,m,d,k,e=extract_percentages(title,category)
    result.update(bonus_base=b,bonus_maximo=m,desconto=d,tipo_percentual=k,evidencia_bonus=e,
                  custo_sugerido=inferred_cost_per_thousand(title,category))
    result["percentual"]=m or b or d
    if not trusted_news_url(article_url):return result
    try:
        response=requests.get(article_url,timeout=9,
                              headers={"User-Agent":"Mozilla/5.0 (SevenIntelligence/2.7)"})
        response.raise_for_status()
        soup=BeautifulSoup(response.text[:550000],"html.parser")
        # Article-only paragraphs; skip unrelated promotions in sidebars/navigation.
        article_root = soup.select_one("article .entry-content, article .post-content, article .td-post-content, article .content, article")
        snippets = [el.get_text(" ", strip=True) for el in article_root.select("p, li")[:140]] if article_root else []
        # Use only date assertions clearly tied to offer validity, not travel or published dates.
        for paragraph in snippets:
            if not result["validade_iso"]:
                iso,ev=promotion_deadline(paragraph[:500],reference)
                if iso:
                    result["validade_iso"],result["validade_evidencia"]=iso,ev
                    result["validade_hora"] = deadline_clock(ev)
            if category=="Transferência" and not (result["origem"] and result["destino"]):
                a,z=infer_transfer_pair(paragraph[:320])
                if a and z:result["origem"],result["destino"],result["origem_evidencia"]=a,z,"texto da matéria"
            if not result["bonus_base"] and not result["bonus_maximo"] and not result["desconto"]:
                b,m,d,k,e=extract_percentages(paragraph[:280],category)
                if b or m or d:result.update(bonus_base=b,bonus_maximo=m,desconto=d,tipo_percentual=k,evidencia_bonus=e)
            if not result["custo_sugerido"]:
                result["custo_sugerido"]=inferred_cost_per_thousand(paragraph[:280],category)
        result["percentual"]=result["bonus_maximo"] or result["bonus_base"] or result["desconto"]
        candidates=[]
        for link in (article_root.select("a[href]")[:240] if article_root else []):
            href=urljoin(article_url,str(link.get("href","")))
            if is_known_official(href):
                label=normalize_text(link.get_text(" ",strip=True))
                score=3 if re.search("regulamento|promocao|campanha|participe|confira|saiba mais",label) else 1
                candidates.append((score,href))
        if candidates:result["link"]=sorted(candidates,key=lambda x:x[0],reverse=True)[0][1]
        result["sucesso"]=True
    except (requests.RequestException,ValueError):pass
    return result


AUTO_VERSION = "3.0"


def analyze_pending(max_items=18, notify=False):
    """Processa notícias ainda não analisadas; não modifica condições verificadas."""
    if st.session_state.get("db_load_failed") or st.session_state.get("db_error"):
        return 0
    data = st.session_state["items"]
    pending = data[(data["status"] != "Descartado") &
                   (data["auto_versao"].fillna("") != AUTO_VERSION)].head(max_items)
    if pending.empty:
        return 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    arguments = [(r["url"], r["titulo"],
                  detect_category(r["titulo"]) if r["status"] == "Não verificado" else r["categoria"],
                  r["publicado"]) for _, r in pending.iterrows()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda args: analyze_article(*args), arguments))
    for (idx, old), finding in zip(pending.iterrows(), results):
        if old.get("status") == "Confirmado no oficial":
            # Reprocessar não sobrescreve histórico de revisão humana.
            continue
        updates = {
            "auto_percentual": finding["percentual"], "auto_prazo": finding["validade_evidencia"],
            "auto_link_candidato": finding["link"], "auto_analisado_em": stamp,
            "validade_iso": finding["validade_iso"], "validade_evidencia": finding["validade_evidencia"],
            "validade_hora": finding["validade_hora"], "origem_auto": finding["origem"],
            "destino_auto": finding["destino"], "bonus_base": finding["bonus_base"],
            "bonus_maximo": finding["bonus_maximo"], "auto_tipo_percentual": finding["tipo_percentual"],
            "auto_desconto": finding["desconto"], "auto_custo_sugerido": finding["custo_sugerido"],
            "auto_evidencia_bonus": finding["evidencia_bonus"],
            "auto_origem_evidencia": finding["origem_evidencia"],
            "auto_data_referencia": format_date(old.get("publicado", "")),
            "auto_versao": AUTO_VERSION,
            "auto_falha": "" if finding["sucesso"] else "Página não acessível; somente título",
            "auto_processado_em": stamp,
        }
        for field, value in updates.items():
            data.at[idx, field] = value
        if old.get("status") == "Não verificado":
            data.at[idx, "categoria"] = detect_category(old.get("titulo", ""))
            data.at[idx, "programa"] = detect_program(old.get("titulo", ""))
        if old.get("status") == "Encerrado" and old.get("status_previo") and not old.get("verificado_em"):
            # Corrige automaticamente encerramento incorreto de versões anteriores.
            if not finding["validade_iso"] or expiry_state({"validade_iso": finding["validade_iso"],
                        "validade_hora": finding["validade_hora"]}) != "Encerrada pela data divulgada":
                data.at[idx, "status"] = old["status_previo"]
                data.at[idx, "status_previo"] = ""
    st.session_state["items"] = data
    sync_deadlines()
    saved = save_items()
    if notify:
        if saved:
            st.success(f"{len(pending)} notícias analisadas e salvas no Supabase.")
        else:
            st.error("Análise feita, mas o salvamento não foi confirmado. Faça backup CSV.")
    return len(pending)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_all_feeds_cached():
    """Atualiza notícias de fontes RSS quando o painel é aberto; cache por 30 minutos."""
    rows, outcomes = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {name: pool.submit(fetch_feed, name, url) for name, url in SOURCES.items()}
        for name, job in jobs.items():
            try:
                found = job.result()
                rows.extend(found)
                outcomes.append(f"{name}: {len(found)}")
            except Exception:
                outcomes.append(f"{name}: indisponível")
    return rows, outcomes


def refresh_pipeline(force=False):
    """Coleta e processa sem exigir visita à aba Coleta. Sem agendamento externo."""
    if st.session_state.get("db_error") or st.session_state.get("db_load_failed"):
        return
    if st.session_state.get("pipeline_running"):
        return
    if not force and st.session_state.get("pipeline_ran"):
        return
    st.session_state["pipeline_running"] = True
    try:
        if force:
            fetch_all_feeds_cached.clear()
        rows, results = fetch_all_feeds_cached()
        added = add_rows(rows)
        analyzed = analyze_pending(max_items=24 if force else 12)
        sync_deadlines()
        st.session_state["pipeline_result"] = (added, analyzed, results)
        st.session_state["pipeline_ran"] = True
    finally:
        st.session_state["pipeline_running"] = False


def automatic_review(max_items=12):
    """Compatibilidade com botão manual de reanálise, usando regras 3.0."""
    n = analyze_pending(max_items=max_items, notify=True)
    if not n:
        st.info("Todas as notícias elegíveis já passaram pela análise 3.0.")


def is_valid_official(url):
    if not url or not str(url).startswith("https://"):
        return False
    host = (urlparse(str(url)).hostname or "").lower()
    return host not in ["passageirodeprimeira.com", "www.passageirodeprimeira.com", "melhoresdestinos.com.br", "www.melhoresdestinos.com.br", "melhorescartoes.com.br", "www.melhorescartoes.com.br", "pontospravoar.com", "www.pontospravoar.com"]


def _filled(value):
    """Normalize pandas/CSV empty values without converting NaN into text."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (ValueError, TypeError):
        pass
    return str(value).strip()


def _percent_number(value):
    """Read a clearly supplied percentage, never infer an absent bonus."""
    match = re.fullmatch(r"(\d{1,3})(?:[.,](\d{1,2}))?\s*%", _filled(value))
    if not match:
        return None
    number = float(match.group(1) + ("." + match.group(2) if match.group(2) else ""))
    return number if 0 <= number <= 200 else None


def _number_pt(value):
    return f"{value:,.0f}".replace(",", ".")


def _date_pt(value):
    try:
        return date.fromisoformat(_filled(value)).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return _filled(value)


def comparable_history(item, all_items, limit=4):
    """Same category and same directional pair only; no previous price reused."""
    kind = _filled(item.get("categoria"))
    if kind not in ("Transferência", "Compra de pontos"):
        return []
    origin = _filled(item.get("origem_pontos")) or _filled(item.get("origem_auto"))
    destination = _filled(item.get("destino_milhas")) or _filled(item.get("destino_auto"))
    program = _filled(item.get("programa"))
    if kind == "Transferência" and (not origin or not destination):
        return []
    out = []
    for _, candidate in all_items.iterrows():
        if _filled(candidate.get("id")) == _filled(item.get("id")):
            continue
        if _filled(candidate.get("categoria")) != kind:
            continue
        if kind == "Transferência":
            ca = _filled(candidate.get("origem_pontos")) or _filled(candidate.get("origem_auto"))
            cb = _filled(candidate.get("destino_milhas")) or _filled(candidate.get("destino_auto"))
            if (normalize_text(ca), normalize_text(cb)) != (normalize_text(origin), normalize_text(destination)):
                continue
        elif normalize_text(_filled(candidate.get("programa"))) != normalize_text(program):
            continue
        # Historic entries require verifiable temporal context and a recorded relevant offer.
        when = _filled(candidate.get("validade_iso")) or _filled(candidate.get("publicado"))
        if not when:
            continue
        offer = (_filled(candidate.get("bonus")) or _filled(candidate.get("bonus_maximo")) or
                 _filled(candidate.get("bonus_base")) or _filled(candidate.get("auto_desconto")))
        if not offer:
            continue
        try:
            dt = datetime.fromisoformat(when.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            dt = _filled(candidate.get("validade_iso")) or format_date(when)
        out.append((dt, candidate, offer))
    out.sort(key=lambda value: value[0], reverse=True)
    return out[:limit]


def explicit_title_bonus(title):
    """Only an explicitly described percentage of transfer bonus in the title."""
    t = normalize_text(title)
    patterns = (
        r"\b(\d{1,3})\s*%\s*(?:de\s+)?(?:bonus|bonificacao)\b",
        r"\b(?:bonus|bonificacao)\s*(?:de\s+)?(\d{1,3})\s*%",
    )
    for pattern in patterns:
        m = re.search(pattern, t)
        if m and int(m.group(1)) <= 200:
            return m.group(1) + "%"
    return ""


def copyable_whatsapp_alert(text):
    """Accessible readable alert with one copy action, without a second textarea or download."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # Payload is base64, so source news titles cannot inject HTML or JavaScript.
    preview = html.escape(text)
    height = min(920, max(280, 102 + len(text.splitlines()) * 23 + len(text) // 115 * 15))
    component_html = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"/>
    <style>
      body {{margin:0;background:transparent;color:#191a1d;font-family:Arial,Helvetica,sans-serif}}
      .panel {{border:1px solid #e4ddce;border-radius:13px;background:#fff;padding:18px 19px}}
      .header {{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}}
      .small {{color:#746d61;font-size:13px;font-weight:600}}
      button {{cursor:pointer;border:0;border-radius:9px;background:linear-gradient(95deg,#ac7911,#ce9a30);
        color:#fff;font-size:15px;font-weight:700;padding:12px 17px;white-space:nowrap}}
      button:focus-visible {{outline:3px solid #202020;outline-offset:2px}}
      pre {{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.60 Arial,Helvetica,sans-serif;
        color:#242424;background:#faf9f6;padding:16px;border-radius:10px;margin:0;user-select:text}}
      #feedback {{font-size:12px;color:#226b35;min-height:18px;margin-top:8px}}
      @media(max-width:620px) {{.header{{align-items:flex-start;flex-direction:column}}}}
    </style></head><body><section class="panel">
      <div class="header"><span class="small">Mensagem pronta para copiar e colar no WhatsApp</span>
      <button id="copy" type="button">📋 Copiar para WhatsApp</button></div>
      <pre id="message">{preview}</pre><div id="feedback" role="status" aria-live="polite"></div>
    </section><script>
      const b64 = '{encoded}';
      const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      const message = new TextDecoder('utf-8').decode(bytes);
      document.getElementById('copy').addEventListener('click', async () => {{
        const feedback = document.getElementById('feedback');
        try {{
          if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('clipboard unavailable');
          await navigator.clipboard.writeText(message);
          feedback.textContent = '✅ Copiado! Agora é só colar no WhatsApp.';
        }} catch (error) {{
          const field = document.createElement('textarea');
          field.value = message;field.setAttribute('readonly','');
          field.style.position='fixed';field.style.opacity='0';
          document.body.appendChild(field);field.focus();field.select();
          const copied = document.execCommand('copy');field.remove();
          feedback.textContent = copied ? '✅ Copiado! Agora é só colar no WhatsApp.' :
           'Não foi possível copiar automaticamente. Selecione o texto acima e copie com ⌘C.';
        }}
      }});
    </script></body></html>"""
    components.html(component_html, height=height, scrolling=True)


def alert_text(item, history_items=None):
    """Seven Travel compact WhatsApp templates. No third-party news URL as promo link."""
    confirmed = (_filled(item.get("status")) == "Confirmado no oficial" and
                 bool(_filled(item.get("verificado_em"))) and
                 is_known_official(_filled(item.get("link_oficial"))))
    category = _filled(item.get("categoria")) or "Outros"
    title = _filled(item.get("titulo")) or "Oportunidade de viagem"
    ended = expiry_state(item) == "Encerrada pela data divulgada" or _filled(item.get("status")) == "Encerrado"
    is_transfer = category == "Transferência"
    is_purchase = category == "Compra de pontos"
    is_flights = category == "Passagens"
    is_hotel = category == "Hotéis"
    is_card = category == "Cartões"

    prefix = ("🔄 *TRANSFERÊNCIA BONIFICADA*" if is_transfer else
              "💰 *COMPRA DE PONTOS / MILHAS*" if is_purchase else
              "✈️ *PASSAGENS*" if is_flights else
              "🏨 *HOTÉIS E VIAGENS*" if is_hotel else
              "💳 *CARTÕES E BENEFÍCIOS*" if is_card else
              "✨ *OPORTUNIDADE SEVEN TRAVEL*")
    lines = ["⚠️ *RASCUNHO — CONFERIR ANTES DE ENVIAR*" if not confirmed else prefix]
    if not confirmed:
        lines.append(prefix)
    lines.extend([f"*{title}*", ""])

    origin = _filled(item.get("origem_pontos")) if confirmed else ""
    destination = _filled(item.get("destino_milhas")) if confirmed else ""
    origin = origin or _filled(item.get("origem_auto"))
    destination = destination or _filled(item.get("destino_auto"))
    if is_transfer and origin and destination:
        lines.append(f"🔄 *{origin} → {destination}*")

    verified_terms = _filled(item.get("bonus")) if confirmed else ""
    base = _filled(item.get("bonus_base"))
    maximum = _filled(item.get("bonus_maximo"))
    discount = _filled(item.get("auto_desconto"))
    per_k = _filled(item.get("custo_milheiro")) if confirmed else ""
    if is_transfer:
        if verified_terms:
            lines.append(f"🎯 *Condições verificadas:* {verified_terms}")
        else:
            if base:
                lines.append(f"🔹 Bônus base divulgado: *{base}*")
            if maximum:
                lines.append(f"🔹 Bônus máximo divulgado: *{maximum}*")
            title_bonus = explicit_title_bonus(title) if not base and not maximum else ""
            if title_bonus:
                lines.append(f"🎯 *Bônus divulgado no título:* {title_bonus} (condições a conferir)")
            elif not base and not maximum:
                lines.append("🎯 Bônus: não identificado com segurança")
        # 100k standard simulation only when percentage exists, never an unverified eligibility claim.
        chosen = _percent_number(maximum or base or explicit_title_bonus(title))
        if chosen is not None:
            bonus_points = int(round(100000 * chosen / 100))
            lines.append(f"📊 *Exemplo com 100.000 pontos:* +{_number_pt(bonus_points)} de bônus = {_number_pt(100000 + bonus_points)} no destino")
            lines.append("_Simulação matemática; bônus depende da elegibilidade e dos limites._")
    elif is_purchase:
        if verified_terms:
            lines.append(f"🏷️ *Condições verificadas:* {verified_terms}")
        elif discount:
            lines.append(f"🏷️ *Desconto divulgado:* {discount}")
        else:
            lines.append("🏷️ Percentual de desconto: a conferir")
        if per_k:
            lines.append(f"💰 *Na prática: milheiro a R$ {per_k}* (confirmado para esta campanha)")
        else:
            lines.append("💰 *Na prática:* custo do milheiro ainda não confirmado")
        lines.append("📌 Faixas de clube, cupom e Pix: verificar as condições da campanha")
    else:
        if verified_terms:
            lines.append(f"🎯 *Condição verificada:* {verified_terms}")
        elif discount:
            lines.append(f"🏷️ *Desconto divulgado:* {discount}")
        else:
            lines.append("🎯 Condições específicas: a conferir na matéria")
        if is_flights:
            lines.append("✈️ Rotas, datas, taxas e disponibilidade: conferir antes de divulgar")
        elif is_hotel:
            lines.append("🏨 Datas de hospedagem e regras da tarifa: conferir")
        elif is_card:
            lines.append("💳 Elegibilidade, anuidade e eventuais limites: conferir")

    iso = _filled(item.get("validade_iso"))
    hour = _filled(item.get("validade_hora"))
    deadline = _filled(item.get("prazo")) if confirmed else ""
    if not deadline and iso:
        deadline = _date_pt(iso) + (f" às {hour} (Brasília)" if hour else " (horário não identificado)")
    lines.append(f"📅 *Prazo:* {deadline or 'Não identificado com segurança'}")
    if ended:
        lines.append("⛔ *ENCERRADA SEGUNDO O PRAZO IDENTIFICADO — NÃO DIVULGAR COMO ATIVA*")

    if history_items and (is_transfer or is_purchase):
        lines.extend(["", "📈 *Histórico comparável (fonte / período)*"])
        for when, past, offer in history_items[:4]:
            status = "regulamento conferido" if _filled(past.get("verificado_em")) else "informado na notícia"
            label = _date_pt(when) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) else when
            lines.append(f"🔹 {label}: {offer} — {status}")
        lines.append("_Comparar elegibilidade, limites e Clube; históricos não determinam o custo atual._")

    lines.append("")
    if confirmed:
        lines.append(f"🔗 *Promoção oficial:* {_filled(item.get('link_oficial'))}")
    else:
        lines.append("🔗 *Promoção oficial:* link ainda não confirmado")
        lines.append("⚠️ Verificar regulamento, prazo, clube, cadastro, cupom e limites antes de publicar.")
    lines.append("*SEVEN TRAVEL ✈️*")
    return "\n".join(lines)


# Seven Intelligence 3.0 — alerta direto com botão Copiar + coleta e pré-análise automáticas + histórico seguro.
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
    # Callbacks execute before Streamlit instantiates the navigation widget on rerun.
    st.session_state["selected_id"] = str(item_id)
    st.session_state["nav"] = "Revisão e alertas"


def goto_alert(item_id):
    st.session_state["selected_id"] = str(item_id)
    st.session_state["show_alert_id"] = str(item_id)
    st.session_state["nav"] = "Revisão e alertas"


def go_to_collection():
    # Never mutate a widget-backed session state key after the widget is instantiated.
    st.session_state["nav"] = "Coleta"

# Coleta e leitura automática ao abrir o aplicativo; uma execução por sessão.
# Sites externos podem estar indisponíveis. O painel permanece utilizável com dados salvos.
if not st.session_state.get("pipeline_ran") and not st.session_state.get("db_error"):
    try:
        refresh_pipeline()
    except Exception:
        st.session_state["pipeline_ran"] = True
        st.session_state["pipeline_warning"] = "Algumas fontes não puderam ser atualizadas nesta abertura."
sync_deadlines()

with st.sidebar:
    st.markdown('<div class="seven-logo"><div class="orbit"><div class="brand">SEVEN</div><div class="travel">TRAVEL</div></div></div>',unsafe_allow_html=True)
    if "nav" not in st.session_state:
        st.session_state["nav"] = "Painel"
    page = st.radio("Navegação",["Painel","Coleta","Revisão e alertas","Histórico","Backup"],key="nav")
    st.divider()
    st.markdown('<div style="color:#e4bd68;font-weight:750;letter-spacing:.08em">✦ SEVEN INTELLIGENCE</div>',unsafe_allow_html=True)
    st.caption("Monitoramento inteligente de promoções, pontos e viagens.")
    st.divider()
    st.caption("A classificação é uma sugestão. Confirme cada regulamento oficial e mantenha backups.")

if st.session_state.get("db_error"):
    st.warning(st.session_state["db_error"])
elif all(supabase_settings()):
    st.caption("☁️ Supabase conectado · Notícias e revisões com salvamento persistente")
    if st.session_state.get("pipeline_result"):
        added, analyzed, feeds = st.session_state["pipeline_result"]
        st.caption(f"🔄 Coleta automática ao abrir: {added} novas · {analyzed} analisadas · " + " | ".join(feeds))
    if st.session_state.get("pipeline_warning"):
        st.caption(st.session_state["pipeline_warning"])
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
        ("Encerradas",int(((df["status"]=="Encerrado") | (df["situacao_fonte"]=="Encerrada pela data divulgada")).sum()) if len(df) else 0,"⊗"),
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
        if st.button("↻ Atualizar notícias e prazos", type="primary", use_container_width=True):
            with st.spinner("Consultando as fontes e atualizando prazos..."):
                refresh_pipeline(force=True)
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
    validity=st.selectbox("Validade informada pela notícia",["Todas", "Ativa segundo a fonte", "Encerrada pela data divulgada", "Prazo não identificado"],
                          help="Com base em uma data de encerramento identificada na matéria; não é validação do regulamento")
    if validity!="Todas":filtered=filtered[filtered["situacao_fonte"]==validity]
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
        visible=filtered.copy()
        visible["_sort_published"] = pd.to_datetime(visible["publicado"], errors="coerce", utc=True)
        visible = visible.sort_values("_sort_published", ascending=False, na_position="last").head(limit)
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
                    if item.get("situacao_fonte")=="Ativa segundo a fonte":
                        st.caption("🗓️ Ativa na notícia")
                    elif item.get("situacao_fonte")=="Encerrada pela data divulgada":
                        st.caption("⌛ Prazo encerrado")
                    st.markdown(f'<span class="seven-article-badge {style}">{safe(item["status"])}</span>',unsafe_allow_html=True)
                with review:
                    st.button("Revisar", key=f"review_{idx}", use_container_width=True,
                              on_click=goto_review, args=(item["id"],))
                with alert:
                    st.button("Gerar alerta", key=f"alert_{idx}", use_container_width=True,
                              type="primary",
                              help="Abre o alerta pronto para copiar. Confira condições e validade antes de publicar.",
                              on_click=goto_alert, args=(item["id"],))
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
            if added and not st.session_state.get("db_error"):
                automatic_review(max_items=12)
    st.divider()
    st.subheader("Análise automática de promoções e prazos — versão 3.0")
    st.caption("Analisa títulos e texto das matérias, extrai datas de término (incluindo horário quando explícito) e separa bônus de descontos. Campanhas com prazo vencido vão para Encerradas ao executar o aplicativo. A confirmação oficial continua separada.")
    st.caption(f"Analisadas na v3.0: {int((st.session_state['items']['auto_versao'].fillna('')==AUTO_VERSION).sum())} de {len(st.session_state['items'])} notícias")
    if st.button("🔎 Corrigir / analisar próximas 12 notícias", type="primary",
                 disabled=bool(st.session_state.get("db_load_failed"))):
        automatic_review(max_items=12)
    st.divider()
    if st.button("Reclassificar notícias não verificadas", disabled=bool(st.session_state.get("db_load_failed"))):
        count = 0
        for idx, item in st.session_state["items"].iterrows():
            if item["status"] == "Não verificado":
                suggested = detect_category(item["titulo"])
                if suggested != item["categoria"]:
                    st.session_state["items"].at[idx, "categoria"] = suggested
                    count += 1
        if save_items():
            st.success(f"{count} notícia(s) reclassificadas e salvas. Revisões existentes preservadas.")
        else:
            st.error("A reclassificação não pôde ser confirmada no banco. Baixe um backup CSV.")
    st.caption("Reclassificação heurística: revise as categorias; não altera notícias já revisadas.")
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
    st.caption("A mensagem é montada automaticamente e tem um botão de copiar. A publicação exige conferência do prazo e regulamento oficial.")
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
        if (st.session_state.get("show_alert_id") == str(item.get("id")) and
                item.get("auto_versao") != AUTO_VERSION and
                not st.session_state.get("db_error")):
            with st.spinner("Lendo a matéria para completar o alerta..."):
                finding = analyze_article(item["url"], item["titulo"], item["categoria"], item["publicado"])
                changes = {"auto_percentual": finding["percentual"], "auto_prazo": finding["validade_evidencia"],
                           "auto_link_candidato": finding["link"], "validade_iso": finding["validade_iso"],
                           "validade_evidencia": finding["validade_evidencia"], "validade_hora": finding["validade_hora"],
                           "origem_auto": finding["origem"], "destino_auto": finding["destino"],
                           "bonus_base": finding["bonus_base"], "bonus_maximo": finding["bonus_maximo"],
                           "auto_tipo_percentual": finding["tipo_percentual"], "auto_desconto": finding["desconto"],
                           "auto_custo_sugerido": finding["custo_sugerido"], "auto_versao": AUTO_VERSION,
                           "auto_analisado_em": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                if item.get("status") != "Confirmado no oficial":
                    for field, value in changes.items():
                        st.session_state["items"].at[chosen, field] = value
                    save_items()
                    sync_deadlines()
                    item = st.session_state["items"].loc[chosen].to_dict()
        st.link_button("Abrir matéria original", item["url"])
        if item.get("validade_iso"):
            st.caption(f"Validade identificada na notícia: {item['validade_iso']} — {expiry_state(item)}. Não equivale à conferência oficial.")
        if item.get("auto_analisado_em"):
            st.info("Sugestões automáticas (não verificadas): tipo: " + str(item.get("auto_tipo_percentual") or "não identificado") +
                    " · bônus: " + str(item.get("bonus_maximo") or item.get("bonus_base") or "não identificado") +
                    " · desconto: " + str(item.get("auto_desconto") or "não identificado") +
                    " · prazo: " + str(item.get("validade_iso") or "não identificado"))
            if item.get("auto_link_candidato"):
                st.link_button("Abrir possível regulamento oficial para conferir", item["auto_link_candidato"])
            st.caption("Verifique prazo, elegibilidade, limites e condições no site oficial antes de confirmar e gerar alerta.")
        with st.expander("Revisão manual (opcional)", expanded=False):
            with st.form("review"):
                program = st.selectbox("Programa", PROGRAMS, index=PROGRAMS.index(item["programa"]) if item["programa"] in PROGRAMS else len(PROGRAMS)-1)
                categories = CATEGORIES
                category = st.selectbox("Categoria", categories, index=categories.index(item["categoria"]) if item["categoria"] in categories else 4)
                deadline = st.text_input("Prazo verificado (data e horário)", value=item["prazo"])
                bonus = st.text_input("Condição / bônus confirmado", value=item["bonus"])
                official = st.text_input("Link OFICIAL do regulamento", value=item["link_oficial"])
                notes = st.text_area("Regras, elegibilidade e observações", value=item["observacoes"])
                st.caption("Histórico: preencha os campos abaixo apenas com condições verificadas no regulamento oficial.")
                origin = st.text_input("Programa de origem (ex.: Livelo)", value=item.get("origem_pontos", ""))
                destination = st.text_input("Programa de destino (ex.: Smiles)", value=item.get("destino_milhas", ""))
                bonus_pct = st.text_input("Bônus confirmado (ex.: 70%)", value=item.get("percentual_bonus", ""))
                cost_per_k = st.text_input("Custo confirmado do milheiro (R$, opcional)",value=item.get("custo_milheiro", ""),
                                           help="Digite somente quando esta campanha divulgar valor verificável; nunca reaproveite valores anteriores.")
                current_status = st.selectbox("Status", STATUS, index=STATUS.index(item["status"]) if item["status"] in STATUS else 0)
                save = st.form_submit_button("Salvar revisão", type="primary")
            if save:
                if st.session_state.get("db_load_failed"):
                    st.error("Banco não carregado. Não é seguro salvar esta revisão.")
                elif current_status == "Confirmado no oficial" and (not deadline.strip() or not bonus.strip() or not is_known_official(official)):
                    st.error("Para confirmar, preencha prazo, condição e URL HTTPS de um programa oficial conhecido. Confira manualmente todas as condições.")
                else:
                    for col, val in {"programa": program, "categoria": category, "prazo": deadline, "bonus": bonus, "link_oficial": official, "observacoes": notes, "status": current_status, "origem_pontos": origin.strip(), "destino_milhas": destination.strip(), "percentual_bonus": bonus_pct.strip(), "custo_milheiro":cost_per_k.strip(), "verificado_em": datetime.now(timezone.utc).isoformat(timespec="seconds") if current_status == "Confirmado no oficial" else item.get("verificado_em", "")}.items():
                        st.session_state["items"].at[chosen, col] = val
                    if save_items():
                        st.session_state["flash"] = "Revisão salva no Supabase."
                    else:
                        st.session_state["flash"] = "Revisão alterada apenas nesta sessão; faça backup CSV."
                    st.rerun()
        if st.session_state.pop("flash", None):
            st.info("Revisão processada. Confira o aviso de conexão acima.")
        item = st.session_state["items"].loc[chosen].to_dict()
        st.subheader("Gerar alerta Seven Travel para WhatsApp")
        confirmed = item["status"] == "Confirmado no oficial" and bool(item.get("verificado_em")) and is_known_official(item.get("link_oficial", ""))
        if confirmed:
            st.success("Condição registrada como conferida no regulamento. Revise antes de enviar.")
        else:
            st.warning("Rascunho automático: dados extraídos de notícia. Confira o regulamento e o prazo antes de publicar.")
        if expiry_state(item) == "Encerrada pela data divulgada" or item["status"] == "Encerrado":
            st.error("A campanha consta como encerrada. Não publique este alerta como promoção ativa.")
        # Generate from saved campaign facts; no manual edit or duplicated code block.
        draft = alert_text(item, comparable_history(item, st.session_state["items"]))
        copyable_whatsapp_alert(draft)
        st.caption("Texto copiado com negritos *prontos para WhatsApp*. Notícias sem verificação oficial permanecem como rascunhos.")


elif page == "Histórico":
    st.subheader("Histórico e comparativo de promoções")
    st.caption("Histórico por tipo e par de programas, somente com dados extraídos pela versão 3.0 ou condições verificadas. Não há percentuais presumidos.")
    history = review_history(df)
    if history.empty:
        st.info("Ainda não há campanhas com prazo explícito encerrado ou confirmadas no regulamento. A análise é executada ao abrir o sistema; campanhas sem prazo confiável não entram como encerradas.")
    else:
        history["Par"] = history.apply(lambda r:
            f"{r.get('origem_pontos') or r.get('origem_auto') or 'Origem não identificada'} → {r.get('destino_milhas') or r.get('destino_auto') or 'Destino não identificado'}",axis=1)
        history["Bônus base"] = history.apply(lambda r:r.get('bonus_base','') or r.get('percentual_bonus',''),axis=1)
        history["Bônus máximo"] = history.apply(lambda r:r.get('bonus_maximo','') or '',axis=1)
        type_filter=st.selectbox("Tipo de promoção",["Todas"]+CATEGORIES,key="historic_type")
        known_pairs=sorted(p for p in history["Par"].unique() if "não identificad" not in normalize_text(p))
        pair_filter=st.selectbox("Par de transferência",["Todos"]+known_pairs,key="historic_pair")
        if type_filter!="Todas":history=history[history["categoria"]==type_filter]
        if pair_filter!="Todos":history=history[history["Par"]==pair_filter]
        if history.empty:
            st.info("Não há campanhas para esses filtros.")
        else:
            st.metric("Campanhas no histórico",len(history))
            st.dataframe(history[["Data de referência","Par","categoria","Bônus base","Bônus máximo","custo_milheiro","Comprovação","fonte","url"]]
                .rename(columns={"categoria":"Categoria","custo_milheiro":"R$ / milheiro","fonte":"Fonte","url":"Matéria"}),
                hide_index=True,use_container_width=True,
                column_config={"Matéria":st.column_config.LinkColumn("Matéria")})
            st.download_button("Baixar histórico comparativo CSV",history.to_csv(index=False).encode("utf-8-sig"),
                               file_name="seven_historico_comparativo.csv",mime="text/csv")
            if pair_filter != "Todos":
                latest=history.iloc[0]
                st.markdown(f"**Última campanha registrada — {safe(pair_filter)}:** {format_date(latest['Data de referência'])}")
                st.caption("Compare apenas períodos e condições equivalentes. Bônus base e máximo dependem de clube, elegibilidade e limite.")
    st.divider()
    st.subheader("Comparativo preliminar de transferências bonificadas")
    st.caption("Campanhas da versão 3.0 encontradas nas notícias, por data de publicação. A presença aqui não confirma validade ou elegibilidade. Para comparar bônus máximos, confira clubes e limites.")
    verified_auto=df[(df["auto_versao"].fillna("")==AUTO_VERSION) &
                     (df["categoria"]=="Transferência") &
                     ((df["origem_auto"].fillna("")!="") & (df["destino_auto"].fillna("")!=""))].copy()
    if verified_auto.empty:
        st.info("Ainda não há pares completos extraídos. A análise prossegue na aba Coleta; pares ambíguos não são inventados.")
    else:
        verified_auto["Par"] = verified_auto["origem_auto"] + " → " + verified_auto["destino_auto"]
        pair_options=sorted(verified_auto["Par"].unique())
        selected_compare=st.selectbox("Comparar banco / programa",["Todos"]+pair_options,key="compare_automatic_pair")
        if selected_compare!="Todos":verified_auto=verified_auto[verified_auto["Par"]==selected_compare]
        verified_auto["Publicado em"] = verified_auto["publicado"].apply(format_date)
        verified_auto["Bônus base"] = verified_auto["bonus_base"].apply(lambda x: x if x else "Não identificado")
        verified_auto["Bônus máximo"] = verified_auto["bonus_maximo"].apply(lambda x: x if x else "Não identificado")
        st.dataframe(verified_auto[["Publicado em","Par","Bônus base","Bônus máximo","situacao_fonte","fonte","url"]]
                     .rename(columns={"situacao_fonte":"Validade segundo a fonte","fonte":"Fonte","url":"Matéria"}),
                     hide_index=True,use_container_width=True,
                     column_config={"Matéria":st.column_config.LinkColumn("Matéria")})
        st.caption("O custo do milheiro só pode ser considerado confirmado se o valor e as condições forem verificados para a campanha correspondente.")
    st.divider()
    st.subheader("Notícias com dados extraídos automaticamente")
    candidates=df[(df["auto_versao"].fillna("")==AUTO_VERSION) & (df["status"]!="Descartado")].copy()
    if candidates.empty:
        st.info("A análise roda na abertura do painel. Somente datas identificadas com contexto geram situação de prazo.")
    else:
        st.dataframe(candidates[["titulo","categoria","situacao_fonte","validade_iso","origem_auto","destino_auto","bonus_base","bonus_maximo","auto_desconto","auto_tipo_percentual","auto_custo_sugerido","auto_link_candidato"]]
            .rename(columns={"titulo":"Notícia","categoria":"Categoria","situacao_fonte":"Situação segundo a fonte",
                             "validade_iso":"Término (fonte)","origem_auto":"Origem sugerida","destino_auto":"Destino sugerido",
                             "bonus_base":"Bônus base informado","bonus_maximo":"Bônus máximo informado","auto_desconto":"Desconto / cashback", "auto_tipo_percentual":"Tipo de percentual", "auto_custo_sugerido":"Milheiro sugerido (não confirmado)","auto_link_candidato":"Possível regulamento"}),
            hide_index=True,use_container_width=True,
            column_config={"Possível regulamento":st.column_config.LinkColumn("Possível regulamento")})
        st.caption("Dados inferidos não são verificação oficial. Sem data confiável o status permanece 'Prazo não identificado'.")

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
            if not {"id","titulo","fonte","url","publicado","programa","categoria","prazo","bonus","link_oficial","status","observacoes"}.issubset(restored.columns):
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
