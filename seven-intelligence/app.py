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
    st.session_state.items = initial_df()


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
    old = st.session_state.items
    incoming = pd.DataFrame(rows).reindex(columns=FIELDS, fill_value="").fillna("")
    combined = pd.concat([old, incoming], ignore_index=True)
    combined = combined.drop_duplicates(subset=["id"], keep="first")
    st.session_state.items = combined.reset_index(drop=True)
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

st.title("✈️ SEVEN INTELLIGENCE")
st.caption("Central de inteligência de promoções • MVP gratuito • dados reais via RSS quando disponíveis")
with st.sidebar:
    st.header("Navegação")
    page = st.radio("Área", ["Painel", "Coleta", "Revisão e alertas", "Backup"], label_visibility="collapsed")
    st.divider()
    st.info("Notícias coletadas NÃO são automaticamente promoções válidas. Verifique o regulamento oficial antes de confirmar.")
    st.caption("Armazenamento temporário nesta versão. Exporte backups regularmente.")

df = st.session_state["items"]
if page == "Painel":
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Notícias", len(df))
    c2.metric("Em análise", int((df.status == "Em análise").sum()) if len(df) else 0)
    c3.metric("Confirmadas", int((df.status == "Confirmado no oficial").sum()) if len(df) else 0)
    c4.metric("Encerradas", int((df.status == "Encerrado").sum()) if len(df) else 0)
    st.subheader("Oportunidades")
    query = st.text_input("Buscar notícia")
    program = st.selectbox("Programa", ["Todos"] + PROGRAMS)
    status = st.selectbox("Status", ["Todos"] + STATUS)
    filtered = df.copy()
    if query:
        filtered = filtered[filtered.titulo.str.contains(query, case=False, regex=False, na=False)]
    if program != "Todos":
        filtered = filtered[filtered.programa == program]
    if status != "Todos":
        filtered = filtered[filtered.status == status]
    if len(filtered):
        st.dataframe(filtered[["titulo", "fonte", "programa", "publicado", "prazo", "status", "url"]], hide_index=True, use_container_width=True,
                     column_config={"url": st.column_config.LinkColumn("Matéria")})
    else:
        st.info("Nenhuma notícia carregada. Vá em Coleta para buscar notícias reais.")

elif page == "Coleta":
    st.subheader("Coletar notícias por RSS")
    st.caption("A coleta depende de cada site disponibilizar um feed RSS acessível. Respeite os termos e limites das fontes.")
    selected = st.multiselect("Fontes", list(SOURCES), default=list(SOURCES))
    if st.button("Buscar notícias agora", type="primary"):
        all_rows = []
        for name in selected:
            try:
                rows = fetch_feed(name, SOURCES[name])
                all_rows.extend(rows)
                st.success(f"{name}: {len(rows)} notícias relevantes encontradas")
            except Exception as exc:
                st.warning(f"{name}: não foi possível consultar o RSS ({type(exc).__name__}).")
        st.success(f"{add_rows(all_rows)} novas notícias adicionadas")
    st.divider()
    st.subheader("Adicionar matéria por URL")
    with st.form("manual"):
        title = st.text_input("Título da matéria")
        url = st.text_input("URL pública da matéria")
        source = st.text_input("Fonte")
        submitted = st.form_submit_button("Adicionar")
    if submitted:
        if title.strip() and url.startswith("https://"):
            n = add_rows([{"id": url, "titulo": title.strip(), "fonte": source.strip(), "url": url,
                           "publicado": "", "programa": detect_program(title), "categoria": detect_category(title),
                           "prazo": "", "bonus": "", "link_oficial": "", "status": "Não verificado", "observacoes": ""}])
            st.success("Matéria adicionada" if n else "Matéria já cadastrada")
        else:
            st.error("Informe título e URL HTTPS válida.")

elif page == "Revisão e alertas":
    st.subheader("Validar promoção")
    if df.empty:
        st.info("Colete ou adicione notícias antes de revisar.")
    else:
        ids = df.index.tolist()
        chosen = st.selectbox("Selecione a notícia", ids, format_func=lambda i: f"{df.loc[i, 'titulo'][:90]} — {df.loc[i, 'fonte']}")
        item = df.loc[chosen].to_dict()
        st.link_button("Abrir matéria original", item["url"])
        with st.form("review"):
            program = st.selectbox("Programa", PROGRAMS, index=PROGRAMS.index(item["programa"]) if item["programa"] in PROGRAMS else len(PROGRAMS)-1)
            category = st.selectbox("Categoria", ["Transferência", "Compra de pontos", "Passagens", "Acúmulo", "Outros"], index=["Transferência", "Compra de pontos", "Passagens", "Acúmulo", "Outros"].index(item["categoria"]) if item["categoria"] in ["Transferência", "Compra de pontos", "Passagens", "Acúmulo", "Outros"] else 4)
            deadline = st.text_input("Prazo verificado (data e horário)", value=item["prazo"])
            bonus = st.text_input("Condição / bônus confirmado", value=item["bonus"])
            official = st.text_input("Link OFICIAL do regulamento", value=item["link_oficial"])
            notes = st.text_area("Regras, elegibilidade e observações", value=item["observacoes"])
            current_status = st.selectbox("Status", STATUS, index=STATUS.index(item["status"]) if item["status"] in STATUS else 0)
            save = st.form_submit_button("Salvar revisão", type="primary")
        if save:
            if current_status == "Confirmado no oficial" and (not deadline.strip() or not bonus.strip() or not is_valid_official(official)):
                st.error("Para confirmar, preencha prazo, condição e URL HTTPS oficial (não a matéria de terceiros).")
            else:
                for col, val in {"programa": program, "categoria": category, "prazo": deadline, "bonus": bonus, "link_oficial": official, "observacoes": notes, "status": current_status}.items():
                    st.session_state.items.at[chosen, col] = val
                st.success("Revisão salva nesta sessão")
                st.rerun()
        item = st.session_state.items.loc[chosen].to_dict()
        if item["status"] == "Confirmado no oficial":
            st.subheader("Alerta para WhatsApp")
            st.code(alert_text(item), language=None)
            st.download_button("Baixar alerta .txt", alert_text(item), file_name="alerta_seven_travel.txt", mime="text/plain")
            st.caption("Revise a mensagem antes de enviar. O sistema não publica automaticamente.")
        else:
            st.warning("O alerta só será liberado quando a promoção estiver marcada como Confirmado no oficial.")

elif page == "Backup":
    st.subheader("Exportar e importar")
    st.warning("No Streamlit Community Cloud, os dados da sessão podem desaparecer quando o aplicativo reiniciar. Exporte sempre após revisar.")
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Baixar backup CSV", csv_bytes, file_name="seven_intelligence_backup.csv", mime="text/csv")
    uploaded = st.file_uploader("Restaurar backup CSV", type=["csv"])
    if uploaded and st.button("Restaurar backup"):
        try:
            restored = pd.read_csv(uploaded, dtype=str).fillna("")
            if not set(FIELDS).issubset(restored.columns):
                st.error("CSV não contém todas as colunas necessárias.")
            else:
                st.session_state.items = restored[FIELDS].drop_duplicates(subset=["id"]).reset_index(drop=True)
                st.success("Backup restaurado nesta sessão")
                st.rerun()
        except Exception:
            st.error("Não foi possível ler o arquivo CSV.")
