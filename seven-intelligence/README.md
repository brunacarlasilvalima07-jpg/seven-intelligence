# Seven Intelligence — MVP gratuito

Painel web para coletar notícias de milhas via RSS, revisar promoções e gerar alertas WhatsApp. **Não verifica automaticamente a validade de campanhas.**

## Publicar gratuitamente no Streamlit Community Cloud

1. Crie uma conta gratuita no [GitHub](https://github.com/) e um repositório **público** chamado `seven-intelligence` (não coloque dados privados nele).
2. Envie para a raiz do repositório os arquivos `app.py`, `requirements.txt` e a pasta `.streamlit` com `config.toml`. Você pode usar **Add file → Upload files**.
3. Entre em [share.streamlit.io](https://share.streamlit.io/), autentique com GitHub e escolha **Create app** / **Deploy**.
4. Selecione seu repositório, branch `main` e arquivo principal `app.py`. Clique em **Deploy**.
5. Abra o endereço `https://<nome-escolhido>.streamlit.app` disponibilizado pela plataforma.

O código pode ficar público no GitHub. **Não use dados pessoais, senhas ou tokens nos arquivos.** Se for usar o painel com informações sensíveis, configure o acesso privado e a autenticação antes de inserir dados; não trate o URL como controle de acesso.

## Executar no Mac (opcional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Como usar

- **Coleta:** buscar matérias nos RSS configurados ou adicionar URLs manualmente.
- **Painel:** pesquisar e filtrar notícias por programa/status.
- **Revisão e alertas:** abrir a matéria, conferir o regulamento oficial, informar prazo e condições, marcar como confirmado e baixar o alerta.
- **Backup:** exportar CSV após cada sessão; importar quando voltar.

## Limitações importantes

- O Streamlit Community Cloud pode reiniciar o app e **não oferece persistência garantida dos dados da sessão**. O backup CSV é indispensável.
- RSS de terceiros pode mudar, bloquear acesso ou não estar disponível; a coleta mostra falhas em vez de criar notícias fictícias.
- Os programas e categorias são identificados por palavras-chave; precisam de revisão.
- A URL oficial é preenchida manualmente. O app verifica apenas formato HTTPS e alguns domínios de terceiros conhecidos; **não confirma a autenticidade do regulamento nem sua vigência**.
- O texto WhatsApp é um rascunho e deve ser revisado. Sem postagem automática.
- Sem Firecrawl, banco persistente, agendamento, login próprio ou acesso seguro multiusuário nesta versão.
- Consulte termos de uso e `robots.txt` das fontes antes de ampliar o scraping.

## Próximos passos

Persistência em Supabase (plano gratuito sujeito a limites), autenticação, coleta agendada, validação assistida de regulamentos e histórico por par de transferência.
