import io
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
FIELDS = ["id", "titulo", "fonte", "url", "publicado", "programa", "categoria", "prazo", "bonus", "link_oficial", "status", "observacoes"]
STATUS = ["Não verificado", "Em análise", "Confirmado no oficial", "Encerrado", "Descartado"]


def initial_df():
    return pd.DataFrame(columns=FIELDS)

if "items" not in st.session_state:
    st.session_state["items"] = initial_df()


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
                     "prazo": "", "bonus": "", "link_oficial": "", "status": "Não verificado", "observacoes": ""})
    return rows


def add_rows(rows):
    if not rows:
        return 0
    old = st.session_state["items"]
    incoming = pd.DataFrame(rows).reindex(columns=FIELDS, fill_value="").fillna("")
    combined = pd.concat([old, incoming], ignore_index=True)
    combined = combined.drop_duplicates(subset=["id"], keep="first")
    st.session_state["items"] = combined.reset_index(drop=True)
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


# Seven Intelligence 2.0 — interface visual. A coleta e revisão permanecem manuais.
st.markdown("""
<style>
:root { --seven-gold: #c99a2e; --seven-ink: #17191d; }
.stApp { background: #faf9f6; color: var(--seven-ink); }
.block-container { max-width: 1320px; padding-top: 2rem; padding-bottom: 4rem; }
[data-testid="stSidebar"] { background: #17191d; }
[data-testid="stSidebar"] * { color: #f7f5ef; }
[data-testid="stSidebar"] [data-testid="stAlert"] { background: #34312a; }
[data-testid="stSidebar"] [data-testid="stAlert"] * { color: #f4e9c9; }
h1, h2, h3 { letter-spacing: -.035em; }
h1 { font-weight: 800 !important; }
[data-testid="stMetric"] { background: white; padding: 18px 20px; border: 1px solid #eae6dd; border-radius: 15px; }
[data-testid="stMetricLabel"] { color: #77736b; }
[data-testid="stMetricValue"] { color: #17191d; }
div[data-testid="stVerticalBlockBorderWrapper"] > div { border-color: #eae6dd; border-radius: 16px; background: #fff; }
.stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] { background: #bd8d23; border-color: #bd8d23; color: white; }
.stButton > button[kind="primary"]:hover { background: #a97c1c; border-color: #a97c1c; }
.seven-eyebrow { font-size: .76rem; letter-spacing: .22em; color: #b48725; font-weight: 700; text-transform: uppercase; }
.seven-subtitle { color: #77736b; margin-top: -.5rem; margin-bottom: 1.8rem; }
.seven-title { font-size: 2.7rem; font-weight: 800; letter-spacing: -.045em; margin: .35rem 0 .6rem; }
.seven-meta { color: #8b8579; font-size: .82rem; }
.seven-status { font-size: .8rem; font-weight: 700; color: #99701d; }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### ✈️ SEVEN")
    st.caption("INTELLIGENCE · 2.0")
    st.divider()
    if "nav" not in st.session_state:
        st.session_state["nav"] = "Painel"
    page = st.radio("Navegação", ["Painel", "Coleta", "Revisão e alertas", "Backup"], key="nav")
    st.divider()
    st.caption("Notícias coletadas não são promoções validadas. Confira sempre o regulamento oficial.")
    st.caption("Dados temporários: exporte backups regularmente.")

st.markdown('<div class="seven-eyebrow">SEVEN TRAVEL · CENTRAL DE INTELIGÊNCIA</div>', unsafe_allow_html=True)
st.markdown('<div class="seven-title">Inteligência de <span style="color:#bd8d23;font-style:italic">viagens</span></div>', unsafe_allow_html=True)
st.markdown('<div class="seven-subtitle">Monitoramento de notícias, análise de promoções e alertas prontos para revisão.</div>', unsafe_allow_html=True)

df = st.session_state["items"]

if page == "Painel":
    a, b, c, d = st.columns(4)
    a.metric("Notícias coletadas", len(df))
    b.metric("Em análise", int((df["status"] == "Em análise").sum()) if len(df) else 0)
    c.metric("Confirmadas", int((df["status"] == "Confirmado no oficial").sum()) if len(df) else 0)
    d.metric("Encerradas", int((df["status"] == "Encerrado").sum()) if len(df) else 0)
    st.write("")
    left, right = st.columns([3, 1])
    with left:
        st.subheader("Últimas oportunidades")
    with right:
        if st.button("↻ Coletar notícias", use_container_width=True):
            st.session_state["nav"] = "Coleta"
            st.rerun()
    qcol, pcol, ccol, scol = st.columns([2.2, 1, 1, 1.2])
    with qcol:
        query = st.text_input("Buscar", placeholder="Pesquisar notícias e promoções...")
    with pcol:
        program = st.selectbox("Programa", ["Todos"] + PROGRAMS)
    with ccol:
        category = st.selectbox("Categoria", ["Todas", "Transferência", "Compra de pontos", "Passagens", "Acúmulo", "Outros"])
    with scol:
        status = st.selectbox("Situação", ["Todos"] + STATUS)
    filtered = df.copy()
    if query:
        filtered = filtered[filtered["titulo"].str.contains(query, case=False, regex=False, na=False)]
    if program != "Todos":
        filtered = filtered[filtered["programa"] == program]
    if category != "Todas":
        filtered = filtered[filtered["categoria"] == category]
    if status != "Todos":
        filtered = filtered[filtered["status"] == status]
    st.caption(f"{len(filtered)} notícia(s) encontrada(s) · Fontes externas não substituem o regulamento oficial")
    if filtered.empty:
        st.info("Nenhuma notícia para esses filtros. Use Coleta para buscar notícias reais.")
    else:
        limit = st.select_slider("Exibir", options=[10, 20, 40, 80, 150], value=20, format_func=lambda n: f"{n} notícias")
        for idx, item in filtered.head(limit).iterrows():
            with st.container(border=True):
                top, tag = st.columns([4, 1.3], vertical_alignment="top")
                with top:
                    st.markdown(f"**{item['titulo']}**")
                    st.caption(f"{item['fonte']}  ·  {item['publicado'] or 'Data não informada'}")
                with tag:
                    st.markdown(f"<span class='seven-status'>{item['status']}</span>", unsafe_allow_html=True)
                    st.caption(f"{item['programa']} · {item['categoria']}")
                actions = st.columns([1, 1, 1, 3])
                with actions[0]:
                    if st.button("Revisar", key=f"review_{idx}", use_container_width=True):
                        st.session_state["selected_id"] = str(item["id"])
                        st.session_state["nav"] = "Revisão e alertas"
                        st.rerun()
                with actions[1]:
                    if item["status"] == "Confirmado no oficial":
                        if st.button("Gerar alerta", key=f"alert_{idx}", use_container_width=True):
                            st.session_state["selected_id"] = str(item["id"])
                            st.session_state["nav"] = "Revisão e alertas"
                            st.rerun()
                    else:
                        st.button("Gerar alerta", key=f"disabled_{idx}", disabled=True, help="Disponível após revisão e confirmação no oficial", use_container_width=True)
                with actions[2]:
                    st.link_button("Abrir matéria", item["url"], use_container_width=True)

elif page == "Coleta":
    st.subheader("Coleta de notícias")
    st.caption("Feeds RSS públicos, quando disponíveis. A coleta não confirma as condições das promoções.")
    with st.container(border=True):
        selected = st.multiselect("Fontes monitoradas", list(SOURCES), default=list(SOURCES))
        if st.button("↻ Buscar notícias agora", type="primary"):
            all_rows = []
            for name in selected:
                try:
                    rows = fetch_feed(name, SOURCES[name])
                    all_rows.extend(rows)
                    st.success(f"{name}: {len(rows)} notícias relevantes encontradas")
                except Exception as exc:
                    st.warning(f"{name}: não foi possível consultar o RSS ({type(exc).__name__}).")
            st.success(f"{add_rows(all_rows)} novas notícias adicionadas")
    st.subheader("Adicionar matéria manualmente")
    with st.form("manual"):
        title = st.text_input("Título da matéria")
        url = st.text_input("URL pública da matéria")
        source = st.text_input("Fonte")
        submitted = st.form_submit_button("Adicionar notícia")
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
                if current_status == "Confirmado no oficial" and (not deadline.strip() or not bonus.strip() or not is_valid_official(official)):
                    st.error("Para confirmar, preencha prazo, condição e URL HTTPS oficial (não a matéria de terceiros). Confira manualmente se a URL realmente pertence ao programa.")
                else:
                    for col, val in {"programa": program, "categoria": category, "prazo": deadline, "bonus": bonus, "link_oficial": official, "observacoes": notes, "status": current_status}.items():
                        st.session_state["items"].at[chosen, col] = val
                    st.success("Revisão salva nesta sessão")
                    st.rerun()
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
    st.warning("No Streamlit Community Cloud, os dados podem desaparecer quando o aplicativo reiniciar. Exporte o backup após cada revisão.")
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Baixar backup CSV", csv_bytes, file_name="seven_intelligence_backup.csv", mime="text/csv", type="primary")
    uploaded = st.file_uploader("Restaurar backup CSV", type=["csv"])
    if uploaded and st.button("Restaurar backup"):
        try:
            restored = pd.read_csv(uploaded, dtype=str).fillna("")
            if not set(FIELDS).issubset(restored.columns):
                st.error("CSV não contém todas as colunas necessárias.")
            else:
                st.session_state["items"] = restored[FIELDS].drop_duplicates(subset=["id"]).reset_index(drop=True)
                st.success("Backup restaurado nesta sessão")
                st.rerun()
        except Exception:
            st.error("Não foi possível ler o arquivo CSV.")
